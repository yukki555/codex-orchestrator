#!/usr/bin/env bash
# codex-orchestrator v2.1.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 - "$SCRIPT_DIR/run_codex.sh" "$@" <<'PY'
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

MODES = ("consult", "implement", "review")
SPEEDS = ("default", "fast", "ultrafast", "inherit")
OVERRIDES = {
    "model": "--model",
    "effort": "--effort",
    "speed": "--speed",
    "timeout": "--timeout",
    "max_cmds": "--max-cmds",
}
BOOLEAN_FLAGS = ("fast", "ephemeral", "structured", "ignore_rules", "use_rules",
                 "ignore_user_config", "use_user_config", "no_mcp", "with_mcp")
MANIFEST_HELP = """Manifest schema
  A JSON array of tasks or an object containing a "tasks" array.
  name          Unique task name using A-Z, a-z, 0-9, dot, underscore or hyphen.
  spec          Spec file path, optional in the effective review mode.
  dir           Working directory.
  mode          consult, implement or review.
  model, effort, speed
                String overrides for the corresponding worker flags.
  timeout       Positive integer seconds.
  max_cmds      Non-negative integer. Zero disables the worker command budget.
  add_dirs      Array of directory paths appended to CLI --add-dir values.
  extra_configs Array of configuration strings appended to CLI -c values.
  structured, ephemeral
                Booleans overriding the corresponding worker flags.
  Relative spec, dir and add_dirs paths use the current directory first,
  then the manifest's parent directory. Other task fields default to CLI values.
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
        description="Run parallel Codex workers with v2.1.0 isolation and patch preflight.",
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
    parser.add_argument("--timeout", type=lambda value: integer(value, positive=True),
                        metavar="SECONDS", help="worker timeout in seconds")
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
                raise ValueError("mode must be consult, implement, or review")
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
            for key, minimum in (("timeout", 1), ("max_cmds", 0)):
                if key in entry and (type(entry[key]) is not int or entry[key] < minimum):
                    raise ValueError(f"{key} must be an integer >= {minimum}")
            flags = {key: getattr(options, key) for key in BOOLEAN_FLAGS}
            for key in ("structured", "ephemeral"):
                value = entry.get(key, flags[key])
                if type(value) is not bool:
                    raise ValueError(f"{key} must be a boolean")
                flags[key] = value
            if flags["structured"] and options.schema:
                raise ValueError("--structured and --schema are mutually exclusive")
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
                dirty = bool(git(root, "status", "--porcelain").stdout)
                common_dir = (root / git_path(git(root, "rev-parse", "--git-common-dir").stdout)).resolve()
                repos[root] = (dirty, locks.setdefault(common_dir, Lock()))
            dirty, lock = repos.get(root, (False, None))
        except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"task {name!r}: {error_text(exc)}") from exc

        tasks.append({
            "name": name, "mode": mode, "spec": spec, "dir": target, "out_dir": out_dir / name,
            "overrides": overrides, "flags": flags, "add_dirs": add_dirs, "configs": configs,
            "isolation": isolation, "git_root": root, "lock": lock,
            "base_dirty_warning": dirty, "worktree": None, "setup_error": "",
        })
    return tasks


def add_worktree(root, path, lock):
    with lock:
        if path.exists() or path.is_symlink():
            raise ValueError(f"worktree path already exists: {path}")
        git(root, "worktree", "add", "--detach", str(path), "HEAD")


def remove_worktree(root, path, lock):
    with lock:
        try:
            git(root, "worktree", "remove", "--force", str(path))
        except subprocess.CalledProcessError:
            if path.exists():
                shutil.rmtree(path)
            git(root, "worktree", "prune")


def translate(path, root, worktree):
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path
    mapped = worktree / relative
    try:
        mapped.resolve().relative_to(worktree)
    except ValueError as exc:
        raise ValueError(f"directory resolves outside the worktree: {mapped}") from exc
    # A directory in the original checkout may contain only untracked files.
    mapped.mkdir(parents=True, exist_ok=True)
    return mapped


def worker_command(run_codex, task, schema):
    target = task["dir"]
    add_dirs = task["add_dirs"]
    if task["worktree"] is not None:
        root, worktree = task["git_root"], task["worktree"]
        target = translate(target, root, worktree)
        add_dirs = [translate(path, root, worktree) for path in add_dirs]
    cmd = [run_codex, task["mode"], "--dir", str(target), "--out-dir", str(task["out_dir"])]
    if task["spec"] is not None:
        cmd.extend(["--spec", str(task["spec"])])
    for key, flag in OVERRIDES.items():
        value = task["overrides"][key]
        if value is not None:
            cmd.extend([flag, str(value)])
    cmd.extend("--" + key.replace("_", "-") for key, enabled in task["flags"].items() if enabled)
    if schema:
        cmd.extend(["--schema", str(schema)])
    for path in add_dirs:
        cmd.extend(["--add-dir", str(path)])
    for config in task["configs"]:
        cmd.extend(["-c", config])
    return cmd


def changed_paths(worktree):
    # Porcelain -z emits literal paths and puts the destination first for renames/copies.
    records = iter(git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
                   .stdout.split(b"\0"))
    paths = set()
    for record in records:
        if not record:
            continue
        paths.add(os.fsdecode(record[3:]))
        if b"R" in record[:2] or b"C" in record[:2]:
            next(records, None)
    return sorted(paths)


def fingerprint(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def run_one(task, run_codex, options):
    task_out = task["out_dir"]
    start = time.monotonic()
    returncode = 1
    errors = []
    data = {}
    worktree = task["worktree"]
    try:
        task_out.mkdir(parents=True, exist_ok=True)
        stdout_log = task_out / "wrapper.stdout.log"
        stderr_log = task_out / "wrapper.stderr.log"
        summary_file = task_out / "summary.json"
        previous_summary = fingerprint(summary_file)
        with stdout_log.open("wb") as stdout, stderr_log.open("wb") as stderr:
            if task["setup_error"]:
                raise ValueError(task["setup_error"])
            cmd = worker_command(run_codex, task, options.schema)
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            returncode = result.returncode
        if fingerprint(summary_file) != previous_summary and summary_file.is_file():
            data = json.loads(summary_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
                raise ValueError("worker summary must be a JSON object")
    except (ValueError, OSError, RuntimeError) as exc:
        errors.append(error_text(exc))

    wall = round(time.monotonic() - start, 3)
    changed_files = (data.get("modified_files") or []) if worktree is None else []
    patch_file = None
    patch_bytes = 0
    if worktree is not None:
        try:
            git(worktree, "add", "-N", "--", ".")
            changed_files = changed_paths(worktree)
            patch = git(worktree, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")
            path = task_out / "changes.patch"
            path.write_bytes(patch.stdout)
            patch_bytes = os.path.getsize(path)
            patch_file = str(path)
            if returncode == 0 and not errors and not options.keep_worktrees:
                remove_worktree(task["git_root"], worktree, task["lock"])
                worktree = None
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            errors.append(error_text(exc))

    if errors and returncode == 0:
        returncode = 1
    data.update({
        "task": task["name"],
        "isolation": task["isolation"],
        "mode": task["mode"],
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
        "changed_files": changed_files,
        "patch_file": patch_file,
        "patch_bytes": patch_bytes,
        "worktree_dir": str(worktree) if worktree is not None else None,
        "base_dirty_warning": task["base_dirty_warning"],
    })
    if errors:
        data["error"] = "\n".join(errors)
    return data


def check_merges(tasks, results, worktrees):
    groups = {}
    for task, result in zip(tasks, results):
        if task["isolation"] == "managed-worktree" and result["patch_bytes"] > 0:
            groups.setdefault(task["git_root"], []).append((task, result))

    checks = []
    occupied = {task["name"].casefold() for task in tasks}
    index = 0
    for root, group in groups.items():
        lock = group[0][0]["lock"]
        patches = [result["patch_file"] for _, result in group]
        counts = Counter(path for _, result in group for path in set(result["changed_files"]))
        apply = ["git", "-C", str(root), "apply", *patches]
        errors = []
        try:
            git(root, "apply", "--check", *patches)
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"Working tree apply check: {error_text(exc)}")

        if len(patches) >= 2:
            while True:
                verify = worktrees / f"_merge_check_{index}"
                index += 1
                if verify.name.casefold() not in occupied and not verify.exists() and not verify.is_symlink():
                    break
            created = False
            try:
                add_worktree(root, verify, lock)
                created = True
                for patch in patches:
                    try:
                        git(verify, "apply", patch)
                    except (OSError, subprocess.CalledProcessError) as exc:
                        errors.append(f"Sequential apply failed for {patch}: {error_text(exc)}")
                        break
            except (ValueError, OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Verify worktree setup: {error_text(exc)}")
            finally:
                if created or verify.exists():
                    try:
                        remove_worktree(root, verify, lock)
                    except (OSError, subprocess.CalledProcessError) as exc:
                        errors.append(f"Verify worktree cleanup: {error_text(exc)}")

        checks.append({
            "git_root": str(root),
            "ok": not errors,
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
    out_dir.mkdir(parents=True, exist_ok=True)
    worktrees = out_dir / "_worktrees"
    managed = [task for task in tasks if task["isolation"] == "managed-worktree"]
    if managed:
        worktrees.mkdir(exist_ok=True)
    for task in managed:
        path = worktrees / task["name"]
        try:
            add_worktree(task["git_root"], path, task["lock"])
            task["worktree"] = path
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            task["setup_error"] = error_text(exc)

    with ThreadPoolExecutor(max_workers=min(options.max_parallel, len(tasks))) as pool:
        futures = [pool.submit(run_one, task, run_codex, options) for task in tasks]
        results = [future.result() for future in futures]
    checks = check_merges(tasks, results, worktrees)
    merge_ok = all(check["ok"] for check in checks)
    all_ok = merge_ok and all(result["exit_code"] == 0 for result in results)
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
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_summary(summary, summary_path)
    return 0 if all_ok else 1


sys.exit(main())
PY
