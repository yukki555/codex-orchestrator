#!/usr/bin/env bash
# codex-orchestrator v3.2.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -I -B - "$SCRIPT_DIR/run_codex.sh" "$@" <<'PY'
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from codex_orch.schema_check import preflight_schema

MODES = ("consult", "implement", "review", "research", "critique", "analyze")
SPEEDS = ("default", "fast", "ultrafast", "inherit")
STDOUT_MODES = ("auto", "compact", "full")
OVERRIDES = {
    "model": "--model",
    "effort": "--effort",
    "speed": "--speed",
    "timeout": "--timeout",
    "stall_timeout": "--stall-timeout",
    "stdout_mode": "--stdout-mode",
    "max_cmds": "--max-cmds",
}
BOOLEAN_FLAGS = ("fast", "ephemeral", "structured", "ignore_rules", "use_rules",
                 "ignore_user_config", "use_user_config", "no_mcp", "with_mcp", "web_search")
MANIFEST_HELP = """Manifest schema
  A JSON array of tasks or an object containing a "tasks" array.
  name          Unique task name using A-Z, a-z, 0-9, dot, underscore or hyphen.
  spec          Spec file path, optional in the effective review mode.
  dir           Working directory.
  mode          consult, implement, review, research, critique or analyze.
  model, effort, speed
                String overrides for the corresponding worker flags.
  timeout, stall_timeout
                Positive integer seconds.
  stdout_mode   auto, compact or full.
  max_cmds      Non-negative integer. Zero disables the worker command budget.
  add_dirs      Array of directory paths appended to CLI --add-dir values.
  images        Array of image paths appended to CLI --image values.
  extra_configs Array of configuration strings appended to CLI -c values.
  schema        Custom output schema path overriding CLI --schema.
  structured, ephemeral, web_search
                Booleans overriding the corresponding worker flags.
  Relative spec, dir, add_dirs, images and schema paths use the current
  directory first, then the manifest's parent directory.
  Other task fields default to CLI values.
"""


def integer(value, positive=False):
    if not re.fullmatch(r"[0-9]+", value) or (positive and int(value) == 0):
        kind = "positive" if positive else "non-negative"
        raise argparse.ArgumentTypeError(f"expected a {kind} integer")
    return int(value)


def directory(value):
    path = Path(value)
    if not value or not path.is_dir():
        raise ValueError(f"directory does not exist: {value!r}")
    return path.resolve()


def readable_file(value):
    path = Path(value)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"expected a readable file: {value!r}")
    return path.resolve()


def argument_path(validate):
    def parse(value):
        try:
            return validate(value)
        except (ValueError, OSError, RuntimeError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def usage():
    parser = argparse.ArgumentParser(
        prog="run_codex_parallel.sh",
        description="Run parallel Codex workers with v3.2.0 isolation and patch verification.",
        epilog=MANIFEST_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--out-dir", required=True, metavar="DIR",
                        help="output directory for per-task artifacts and parallel-summary.json")
    parser.add_argument("--mode", choices=MODES, default="consult",
                        help="default worker mode (default: consult)")
    parser.add_argument("--max-parallel", type=lambda value: integer(value, positive=True),
                        default=4, metavar="N", help="maximum concurrent workers (default: 4)")
    parser.add_argument("--dir", default=".", metavar="DIR",
                        help="default working directory (default: current directory)")
    parser.add_argument("--model", help="default worker model")
    parser.add_argument("--effort", help="default worker reasoning effort")
    parser.add_argument("--speed", choices=SPEEDS, help="default worker speed tier")
    parser.add_argument("--fast", action="store_true", help="enable the worker fast preset")
    parser.add_argument("--ephemeral", action="store_true", help="use ephemeral worker sessions")
    parser.add_argument("--web-search", action="store_true", help="enable web search for workers")
    parser.add_argument("--image", type=argument_path(readable_file), action="append", default=[],
                        metavar="FILE", help="attach an image to each worker prompt (repeatable)")
    parser.add_argument("--timeout", type=lambda value: integer(value, positive=True),
                        metavar="SECONDS", help="worker timeout in seconds")
    parser.add_argument("--stall-timeout", type=lambda value: integer(value, positive=True),
                        metavar="SECONDS", help="worker inactivity timeout in seconds")
    parser.add_argument("--stdout-mode", choices=STDOUT_MODES, default="auto",
                        help="worker output preview mode (default: auto)")
    parser.add_argument("--max-cmds", type=integer, metavar="N",
                        help="worker command budget (0 disables it)")
    parser.add_argument("-c", "--config", action="append", default=[], metavar="K=V",
                        help="append a worker configuration (repeatable)")
    parser.add_argument("--add-dir", type=argument_path(directory), action="append", default=[],
                        metavar="DIR", help="add a writable directory for implement (repeatable)")

    rules = parser.add_mutually_exclusive_group()
    rules.add_argument("--ignore-rules", action="store_true", help="skip user rules")
    rules.add_argument("--use-rules", action="store_true", help="load user rules")
    config = parser.add_mutually_exclusive_group()
    config.add_argument("--ignore-user-config", action="store_true", help="skip user config")
    config.add_argument("--use-user-config", action="store_true", help="load user config")
    mcp = parser.add_mutually_exclusive_group()
    mcp.add_argument("--no-mcp", action="store_true", help="disable MCP for workers")
    mcp.add_argument("--with-mcp", action="store_true", help="enable MCP for workers")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--structured", action="store_true", help="use the bundled output schema")
    output.add_argument("--schema", type=argument_path(readable_file), metavar="FILE",
                        help="use a custom worker output schema")
    isolation = parser.add_mutually_exclusive_group()
    isolation.add_argument("--worktree", action="store_true",
                           help="require a managed Git worktree for every task")
    isolation.add_argument("--shared-dir", action="store_true",
                           help="run directly in each task directory (implement defaults to "
                                "managed worktrees in Git repositories with a valid HEAD)")
    parser.add_argument("--keep-worktrees", action="store_true",
                        help="preserve successful worktrees as well as failed worktrees")
    parser.add_argument("--dirty-policy", choices=("fail", "head"), default="fail",
                        help="refuse dirty checkouts or explicitly use clean HEAD for managed worktrees")
    tasks = parser.add_mutually_exclusive_group(required=True)
    tasks.add_argument("--task", action="append", metavar="NAME:SPEC[:DIR]",
                       help="define a task (repeatable, empty SPEC allowed in review)")
    tasks.add_argument("--manifest", metavar="FILE.json", help="load task definitions from JSON")
    return parser


def git(path, *args, check=True, **kwargs):
    return subprocess.run(["git", "-C", str(path), *args], check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def git_path(output):
    return Path(os.fsdecode(output[:-1] if output.endswith(b"\n") else output))


def repo_for(path):
    root = git(path, "rev-parse", "--show-toplevel", check=False)
    if root.returncode == 0 and git(path, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        return git_path(root.stdout).resolve()
    return None


def error_text(exc):
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        return os.fsdecode(exc.stderr).strip()
    return str(exc)


def manifest_path(value, parent, validate):
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty path string")
    path = Path(value)
    if parent is not None and not path.is_absolute() and not path.exists():
        path = parent / path
    return validate(str(path))


def value_list(entry, key):
    values = entry.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be an array of strings")
    return values


def task_entries(options):
    if options.manifest is not None:
        manifest = readable_file(options.manifest)
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(entries, dict):
            entries = entries.get("tasks")
        if not isinstance(entries, list) or not entries:
            raise ValueError("manifest must contain a non-empty task array")
        return entries, manifest.parent

    entries = []
    for raw in options.task:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise ValueError(f"invalid --task {raw!r}, expected NAME:SPEC[:DIR]")
        name, spec = parts[:2]
        entry = {"name": name}
        if spec:
            entry["spec"] = spec
        if len(parts) == 3 and parts[2]:
            entry["dir"] = parts[2]
        entries.append(entry)
    return entries, None


def prepare_tasks(options, out_dir):
    entries, parent = task_entries(options)
    default_dir = directory(options.dir)
    tasks = []
    seen = set()
    repos = {}
    locks = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each manifest task must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"invalid task name: {name!r}")
        # Dot paths and orchestration artifacts cannot serve as task output directories.
        output_key = name.casefold()
        if output_key in (".", "..", "_worktrees", "parallel-summary.json"):
            raise ValueError(f"reserved task name: {name!r}")
        if output_key in seen:
            raise ValueError(f"duplicate task output name: {name!r}")
        seen.add(output_key)
        try:
            mode = entry.get("mode", options.mode)
            if mode not in MODES:
                raise ValueError(f"mode must be one of {', '.join(MODES)}")
            if "spec" in entry:
                spec = manifest_path(entry["spec"], parent, readable_file)
            elif mode == "review":
                spec = None
            else:
                raise ValueError(f"a spec file is required in {mode} mode")
            target = (manifest_path(entry["dir"], parent, directory)
                      if "dir" in entry else default_dir)
            overrides = {key: entry.get(key, getattr(options, key)) for key in OVERRIDES}
            for key in ("model", "effort", "speed"):
                if key in entry and not isinstance(entry[key], str):
                    raise ValueError(f"{key} must be a string")
            if overrides["speed"] is not None and overrides["speed"] not in SPEEDS:
                raise ValueError("speed must be default, fast, ultrafast, or inherit")
            if overrides["stdout_mode"] not in STDOUT_MODES:
                raise ValueError("stdout_mode must be auto, compact, or full")
            for key, minimum in (("timeout", 1), ("stall_timeout", 1), ("max_cmds", 0)):
                if key in entry and (type(entry[key]) is not int or entry[key] < minimum):
                    raise ValueError(f"{key} must be an integer >= {minimum}")
            flags = {key: getattr(options, key) for key in BOOLEAN_FLAGS}
            for key in ("structured", "ephemeral", "web_search"):
                value = entry.get(key, flags[key])
                if type(value) is not bool:
                    raise ValueError(f"{key} must be a boolean")
                flags[key] = value
            task_schema = (manifest_path(entry["schema"], parent, readable_file)
                           if "schema" in entry else options.schema)
            if flags["structured"] and task_schema is not None:
                raise ValueError("--structured and --schema are mutually exclusive")
            if task_schema is not None:
                _, schema_error = preflight_schema(task_schema)
                if schema_error:
                    raise ValueError(schema_error)
            images = options.image + [
                manifest_path(path, parent, readable_file) for path in value_list(entry, "images")
            ]
            add_dirs = options.add_dir + [
                manifest_path(path, parent, directory) for path in value_list(entry, "add_dirs")
            ]
            if add_dirs and mode != "implement":
                raise ValueError("--add-dir is supported only in implement mode")
            configs = options.config + value_list(entry, "extra_configs")

            root = repo_for(target)
            if options.worktree and root is None:
                raise ValueError("--worktree requires a Git work tree with a valid HEAD")
            if options.shared_dir or (mode != "implement" and not options.worktree):
                isolation = "shared-dir"
            elif root is None:
                isolation = "shared-dir-non-git"
            else:
                isolation = "managed-worktree"
            if root is not None and root not in repos:
                base_oid = git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
                dirty = bool(git(root, "status", "--porcelain", "--untracked-files=normal").stdout)
                common_dir = (root / git_path(git(root, "rev-parse", "--git-common-dir").stdout)).resolve()
                repos[root] = (base_oid, dirty, common_dir, locks.setdefault(common_dir, Lock()))
            base_oid, dirty, common_dir, lock = repos.get(root, (None, False, None, None))
            if isolation == "managed-worktree" and dirty and options.dirty_policy == "fail":
                raise ValueError(f"Git checkout has uncommitted changes at {root}. "
                                 "Commit, stash, or remove local changes, or explicitly pass "
                                 "--dirty-policy head to start from clean HEAD.")
        except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"task {name!r}: {error_text(exc)}") from exc

        tasks.append({
            "name": name, "mode": mode, "spec": spec, "dir": target, "out_dir": out_dir / name,
            "overrides": overrides, "flags": flags, "add_dirs": add_dirs, "configs": configs,
            "images": images, "schema": task_schema,
            "isolation": isolation, "git_root": root, "git_common_dir": common_dir, "lock": lock,
            "base_oid": base_oid, "base_dirty_warning": dirty, "worktree": None,
            "worktree_registered": False, "setup_error": "",
            "external_add_dirs": [str(path) for path in add_dirs
                                  if isolation != "managed-worktree" or not path.is_relative_to(root)],
        })
    return tasks


def add_worktree(root, path, base_oid, lock):
    with lock:
        if path.exists() or path.is_symlink():
            raise ValueError(f"worktree path already exists: {path}")
        git(root, "worktree", "add", "--detach", str(path), base_oid)


def remove_worktree(root, path, lock, *, registered=True):
    if not registered:
        return
    with lock:
        git(root, "worktree", "remove", "--force", str(path))
        git(root, "worktree", "prune")


def translate(path, root, worktree):
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path
    mapped = worktree / relative
    try:
        mapped.resolve().relative_to(worktree.resolve())
    except ValueError as exc:
        raise ValueError(f"directory resolves outside the worktree: {mapped}") from exc
    # A directory in the original checkout may contain only untracked files.
    mapped.mkdir(parents=True, exist_ok=True)
    return mapped


def worker_command(run_codex, task, invocation_id):
    target = task["dir"]
    add_dirs = task["add_dirs"]
    if task["worktree"] is not None:
        root, worktree = task["git_root"], task["worktree"]
        target = translate(target, root, worktree)
        add_dirs = [translate(path, root, worktree) for path in add_dirs]
    cmd = [run_codex, task["mode"], "--dir", str(target), "--out-dir", str(task["out_dir"]),
           "--invocation-id", invocation_id]
    if task["spec"] is not None:
        cmd.extend(["--spec", str(task["spec"])])
    for key, flag in OVERRIDES.items():
        value = task["overrides"][key]
        if value is not None:
            cmd.extend([flag, str(value)])
    cmd.extend("--" + key.replace("_", "-") for key, enabled in task["flags"].items() if enabled)
    if task["schema"] is not None:
        cmd.extend(["--schema", str(task["schema"])])
    for path in task["images"]:
        cmd.extend(["--image", str(path)])
    for path in add_dirs:
        cmd.extend(["--add-dir", str(path)])
    for config in task["configs"]:
        cmd.extend(["-c", config])
    return cmd


def atomic_write(path, content):
    temp = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temp_path = Path(temp.name)
    try:
        with temp:
            temp.write(content)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def fingerprint(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_worker_summary(path, previous, invocation_id):
    if not path.is_file():
        raise ValueError("worker summary.json is missing or is not a file")
    if fingerprint(path) == previous:
        raise ValueError("worker summary.json was not refreshed")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"invalid worker summary.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("worker summary.json must be a JSON object")
    if data.get("invocation_id") != invocation_id:
        raise ValueError("worker summary.json invocation_id does not match this invocation")
    return data


def run_one(task, run_codex, options):
    task_out = task["out_dir"]
    start = time.monotonic()
    returncode = 1
    errors = []
    data = {}
    worktree = task["worktree"]
    invocation_id = uuid.uuid4().hex
    try:
        task_out.mkdir(parents=True, exist_ok=True)
        stdout_log = task_out / "wrapper.stdout.log"
        stderr_log = task_out / "wrapper.stderr.log"
        summary_file = task_out / "summary.json"
        previous_summary = fingerprint(summary_file)
        with stdout_log.open("wb") as stdout, stderr_log.open("wb") as stderr:
            if task["setup_error"]:
                raise ValueError(task["setup_error"])
            cmd = worker_command(run_codex, task, invocation_id)
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            returncode = result.returncode
        data = read_worker_summary(summary_file, previous_summary, invocation_id)
        if data.get("status") != "ok" or data.get("exit_code") != 0:
            raise ValueError(f"worker summary.json reported status={data.get('status')!r}, "
                             f"exit_code={data.get('exit_code')!r}")
        if returncode != 0:
            raise ValueError(f"worker exited with code {returncode} despite a successful summary.json")
        requires_structured = task["flags"]["structured"] or task["schema"] is not None
        if requires_structured and data.get("structured_valid") is not True:
            raise ValueError("worker summary.json must report structured_valid=true for structured output")
    except (ValueError, OSError, RuntimeError) as exc:
        errors.append(error_text(exc))

    wall = round(time.monotonic() - start, 3)
    changed_files = (data.get("modified_files") or []) if worktree is None else []
    patch_file = None
    patch_bytes = 0
    base_oid = task["base_oid"]
    final_tree_oid = reconstructed_tree_oid = None
    custody_verified = False
    if task["worktree_registered"]:
        try:
            index_env = os.environ.copy()
            index_env.pop("GIT_INDEX_FILE", None)
            real_index = worktree / git_path(
                git(worktree, "rev-parse", "--git-path", "index", env=index_env).stdout
            )
            if git(worktree, "ls-files", "-u", env=index_env).stdout:
                raise ValueError("worker index contains unmerged entries")
            with tempfile.TemporaryDirectory() as private_dir:
                tmp = Path(private_dir)
                final_index = tmp / "final.index"
                env_final = {**index_env, "GIT_INDEX_FILE": str(final_index)}
                try:
                    shutil.copyfile(real_index, final_index)
                    git(worktree, "add", "-A", "--", ".", env=env_final)
                    staged_tree = git(worktree, "write-tree", env=env_final).stdout.decode("ascii").strip()
                    final_index.unlink()
                    git(worktree, "read-tree", staged_tree, env=env_final)
                except FileNotFoundError:
                    worker_head = git(worktree, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
                    git(worktree, "read-tree", worker_head, env=env_final)
                git(worktree, "add", "-A", "--", ".", env=env_final)
                final_tree_oid = git(worktree, "write-tree", env=env_final).stdout.decode("ascii").strip()
                patch = git(worktree, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                            base_oid, final_tree_oid, "--").stdout
                names = git(worktree, "diff", "--name-only", "-z", base_oid, final_tree_oid, "--").stdout
                changed_files = sorted(os.fsdecode(name) for name in names.split(b"\0") if name)
                path = task_out / "changes.patch"
                atomic_write(path, patch)
                patch_file = str(path)
                patch_bytes = len(patch)

                env_verify = {**os.environ, "GIT_INDEX_FILE": str(tmp / "verify.index")}
                git(worktree, "read-tree", base_oid, env=env_verify)
                if patch_bytes > 0:
                    git(worktree, "apply", "--cached", "--binary", patch_file, env=env_verify)
                reconstructed_tree_oid = git(worktree, "write-tree", env=env_verify).stdout.decode("ascii").strip()
                custody_verified = reconstructed_tree_oid == final_tree_oid
                if not custody_verified:
                    errors.append(f"patch custody mismatch: reconstructed tree {reconstructed_tree_oid} "
                                  f"differs from final tree {final_tree_oid}")
                    returncode = 1

            if returncode == 0 and not errors and custody_verified and not options.keep_worktrees:
                remove_worktree(task["git_root"], worktree, task["lock"], registered=True)
                worktree = None
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"Worktree patch custody or cleanup: {error_text(exc)}")

    if errors and returncode == 0:
        returncode = 1
    data.update({
        "task": task["name"],
        "isolation": task["isolation"],
        "mode": task["mode"],
        "invocation_id": invocation_id,
        "model": data.get("model", task["overrides"]["model"]),
        "effort": data.get("effort", task["overrides"]["effort"]),
        "speed": data.get("speed", task["overrides"]["speed"]),
        "status": "error" if errors else data.get("status", "ok" if returncode == 0 else "error"),
        "exit_code": returncode,
        "duration_s": data.get("duration_s", wall),
        "wrapper_wall_s": wall,
        "commands": data.get("commands", 0),
        "cmd_output_kb": data.get("cmd_output_kb", 0.0),
        "tokens": data.get("tokens", {"input": 0, "cached": 0, "output": 0, "reasoning": 0}),
        "stderr_errors": data.get("stderr_errors", 0),
        "thread_id": data.get("thread_id", "unknown"),
        "last_message": data.get("last_message", str(task_out / "last_message.md")),
        "git_root": str(task["git_root"]) if task["git_root"] is not None else None,
        "git_common_dir": str(task["git_common_dir"]) if task["git_common_dir"] is not None else None,
        "base_oid": base_oid,
        "final_tree_oid": final_tree_oid,
        "reconstructed_tree_oid": reconstructed_tree_oid,
        "custody_verified": custody_verified,
        "external_add_dirs": task["external_add_dirs"],
        "changed_files": changed_files,
        "patch_file": patch_file,
        "patch_bytes": patch_bytes,
        "worktree_dir": str(worktree) if worktree is not None else None,
        "base_dirty_warning": task["base_dirty_warning"],
    })
    if errors:
        data["error"] = "\n".join(errors)
    return data


def check_base_reconstruction(root, base_oid, lock, patches, worktrees, errors):
    created = False
    try:
        verify_parent = Path(tempfile.mkdtemp(prefix="merge_check_", dir=worktrees))
        verify = verify_parent / "wt"
        add_worktree(root, verify, base_oid, lock)
        created = True
        for patch in patches:
            try:
                git(verify, "apply", "--binary", patch)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Sequential apply failed for {patch}: {error_text(exc)}")
                return False
        return True
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"Verify worktree setup: {error_text(exc)}")
        return False
    finally:
        if created:
            try:
                remove_worktree(root, verify, lock, registered=True)
                verify_parent.rmdir()
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Verify worktree cleanup: {error_text(exc)}")


def check_merges(tasks, results, worktrees):
    groups = {}
    for task, result in zip(tasks, results):
        if task["isolation"] == "managed-worktree" and result["patch_bytes"] > 0:
            groups.setdefault((task["git_root"], task["base_oid"]), []).append((task, result))

    checks = []
    for (root, base_oid), group in groups.items():
        lock = group[0][0]["lock"]
        patches = [result["patch_file"] for _, result in group]
        counts = Counter(path for _, result in group for path in set(result["changed_files"]))
        apply = ["git", "-C", str(root), "apply", *patches]
        errors = []
        try:
            git(root, "apply", "--check", *patches)
            target_apply_ok = True
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"Working tree apply check: {error_text(exc)}")
            target_apply_ok = False
        base_reconstruct_ok = check_base_reconstruction(root, base_oid, lock, patches, worktrees, errors)

        checks.append({
            "git_root": str(root),
            "base_oid": base_oid,
            "base_reconstruct_ok": base_reconstruct_ok,
            "target_apply_ok": target_apply_ok,
            "ok": base_reconstruct_ok and target_apply_ok,
            "overlapping_files": sorted(path for path, count in counts.items() if count >= 2),
            "patches": patches,
            "apply_command": " ".join(shlex.quote(arg) for arg in apply),
            "error": "\n".join(errors),
        })
    return checks


def print_summary(summary, path):
    print(f"status: {summary['status']}")
    print(f"tasks: {summary['task_count']} (max_parallel={summary['max_parallel']})")
    print(f"wall_duration_s: {summary['wall_duration_s']} "
          f"(sum_workers_s={summary['sum_worker_duration_s']})")
    print(f"parallel_summary: {path}")
    print(f"merge_ok: {str(summary['merge_ok']).lower()}")
    for check in summary["merge_check"]:
        status = "ok" if check["ok"] else "error"
        print(f"merge_check: {status} {check['git_root']} "
              f"({len(check['patches'])} patches, {len(check['overlapping_files'])} overlapping files)")
        print(check["apply_command"])
        if check["error"]:
            print(check["error"])

    print("\n| task | isolation | status | exit_code | duration_s | speed | commands | changed_files | patch_or_msg |")
    print("|---|---|---|---:|---:|---|---:|---:|---|")
    for result in summary["tasks"]:
        values = [result[key] for key in ("task", "isolation", "status", "exit_code",
                                          "duration_s", "speed", "commands")]
        values.extend([len(result["changed_files"]), result["patch_file"] or result["last_message"]])
        cells = [str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
                 for value in values]
        print("| " + " | ".join(cells) + " |")


def main():
    run_codex = sys.argv[1]
    parser = usage()
    options = parser.parse_args(sys.argv[2:])
    try:
        if not options.out_dir:
            raise ValueError("--out-dir requires a non-empty path")
        out_dir = Path(options.out_dir).resolve()
        tasks = prepare_tasks(options, out_dir)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(error_text(exc))
    if not os.access(run_codex, os.X_OK):
        print(f"Error: missing executable run_codex.sh at {run_codex}", file=sys.stderr)
        return 127

    start = time.monotonic()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    worktrees = out_dir / "_worktrees"
    managed = [task for task in tasks if task["isolation"] == "managed-worktree"]
    if managed:
        worktrees.mkdir(exist_ok=True)
    for task in managed:
        path = worktrees / task["name"]
        try:
            add_worktree(task["git_root"], path, task["base_oid"], task["lock"])
            task["worktree_registered"] = True
            task["worktree"] = path
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            task["setup_error"] = error_text(exc)

    with ThreadPoolExecutor(max_workers=min(options.max_parallel, len(tasks))) as pool:
        futures = [pool.submit(run_one, task, run_codex, options) for task in tasks]
        results = [future.result() for future in futures]
    checks = check_merges(tasks, results, worktrees)
    merge_ok = all(check["ok"] for check in checks)
    all_ok = (merge_ok and all(not check["error"] for check in checks)
              and all(result["exit_code"] == 0 for result in results))
    summary = {
        "status": "ok" if all_ok else "error",
        "mode": options.mode,
        "max_parallel": options.max_parallel,
        "task_count": len(results),
        "wall_duration_s": round(time.monotonic() - start, 3),
        "sum_worker_duration_s": round(sum(float(result["duration_s"] or 0) for result in results), 3),
        "merge_ok": merge_ok,
        "merge_check": checks,
        "out_dir": str(out_dir),
        "tasks": results,
    }
    summary_path = out_dir / "parallel-summary.json"
    atomic_write(summary_path, (json.dumps(summary, indent=2) + "\n").encode("utf-8"))
    print_summary(summary, summary_path)
    return 0 if all_ok else 1


sys.exit(main())
PY
