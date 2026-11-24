"""Run parallel workers and verify their patches against pinned Git bases."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from . import profiles, verify
from .env import ensure_private_dir
from .schema_check import preflight_schema

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
  allowed_paths, protected_paths
                Arrays of Git-root-relative globs for managed-worktree edits.
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
        description="Run parallel Codex workers with Git isolation and patch verification.",
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
    parser.add_argument("--profiles-file", type=argument_path(readable_file), metavar="FILE",
                        help="forward a custom profiles file to every worker")
    parser.add_argument("--contract", type=argument_path(readable_file), metavar="FILE",
                        help="load a trusted contract instead of the pinned Git contract")
    verification = parser.add_mutually_exclusive_group()
    verification.add_argument("--verify-cmd", metavar="CMD_OR_JSON_ARGV",
                              help="run a host command against the combined patches")
    verification.add_argument("--verify-preset", metavar="NAME",
                              help="run a host command from the trusted contract")
    parser.add_argument("--verify-timeout", type=lambda value: integer(value, positive=True),
                        default=120, metavar="SECONDS", help="host verification timeout (default: 120)")
    parser.add_argument("--no-ownership-table", action="store_true",
                        help="skip ownership table injection into worker specs")
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


def prepare_tasks(options, out_dir, verify_requested):
    config = profiles.load_profiles(options.profiles_file)
    entries, parent = task_entries(options)
    default_dir = directory(options.dir)
    tasks = []
    seen = set()
    repos = {}
    locks = {}
    contracts = {}
    verification_commands = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each manifest task must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"invalid task name: {name!r}")
        # Dot paths and orchestration artifacts cannot serve as task output directories.
        output_key = name.casefold()
        if (output_key in (".", "..", "_worktrees", "parallel-summary.json")
                or (verify_requested and re.fullmatch(r"verification(?:-[0-9]+)?\.json", output_key))):
            raise ValueError(f"reserved task name: {name!r}")
        if output_key in seen:
            raise ValueError(f"duplicate task output name: {name!r}")
        seen.add(output_key)
        try:
            mode = entry.get("mode", options.mode)
            if mode not in MODES or mode not in config["valid_modes"]:
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
            if overrides["effort"] and overrides["effort"] not in config["valid_efforts"]:
                raise ValueError(f"invalid effort: {overrides['effort']!r}")
            if overrides["speed"] is not None and overrides["speed"] not in SPEEDS:
                raise ValueError("speed must be default, fast, ultrafast, or inherit")
            if overrides["speed"] is not None and overrides["speed"] not in config["valid_speeds"]:
                raise ValueError(f"invalid speed: {overrides['speed']!r}")
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
            for key in ("allowed_paths", "protected_paths"):
                verify.validate_path_globs(value_list(entry, key), key)

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
            group = (root, base_oid)
            if group not in contracts:
                contract = {}
                if root is not None or options.contract is not None:
                    contract, _ = verify.load_trusted_contract(
                        root if root is not None else target, base_oid or "",
                        contract_path=options.contract,
                    )
                contracts[group] = contract
                if verify_requested and root is not None:
                    argv, source = verify.resolve_verify_argv(
                        contract, verify_cmd=options.verify_cmd, verify_preset=options.verify_preset,
                    )
                    timeout = min(options.verify_timeout, contract.get("max_timeout_s", options.verify_timeout))
                    verification_commands[group] = argv, source, timeout
            contract = contracts[group]
            contract_allowed = contract.get("allowed_paths", [])
            allowed = entry.get("allowed_paths", contract.get("allowed_paths", []))
            protected = list(dict.fromkeys(
                [*contract.get("protected_paths", []), *entry.get("protected_paths", [])]
            ))
            if isolation != "managed-worktree" and (
                (mode == "implement" and (verify_requested or options.contract is not None
                                          or allowed or contract_allowed or protected))
                or entry.get("allowed_paths") or entry.get("protected_paths")
            ):
                raise ValueError("parallel path policies and verification require a managed Git worktree")
        except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"task {name!r}: {error_text(exc)}") from exc

        tasks.append({
            "name": name, "mode": mode, "spec": spec, "dir": target, "out_dir": out_dir / name,
            "overrides": overrides, "flags": flags, "add_dirs": add_dirs, "configs": configs,
            "images": images, "schema": task_schema,
            "allowed_paths": allowed, "contract_allowed": contract_allowed, "protected_paths": protected,
            "declares_paths": "allowed_paths" in entry or "protected_paths" in entry,
            "profiles_file": options.profiles_file,
            "contract": options.contract if mode == "implement" else None,
            "isolation": isolation, "git_root": root, "git_common_dir": common_dir, "lock": lock,
            "base_oid": base_oid, "base_dirty_warning": dirty, "worktree": None,
            "worktree_registered": False, "setup_error": "",
            "external_add_dirs": [str(path) for path in add_dirs
                                  if isolation != "managed-worktree" or not path.is_relative_to(root)],
        })
    if verify_requested and not any(task["isolation"] == "managed-worktree" for task in tasks):
        raise ValueError("combined verification requires at least one managed Git worktree")
    return tasks, verification_commands


def ownership_table(tasks, options):
    if options.no_ownership_table or len(tasks) < 2 or not any(
        task["mode"] == "implement" or task["declares_paths"] for task in tasks
    ):
        return None

    def cell(patterns, empty):
        return ", ".join(patterns).replace("|", r"\|").replace("\n", " ").replace("\r", " ") or empty

    lines = [
        "## Parallel Ownership Table (Auto-Injected)",
        "",
        "| Task | Allowed paths | Protected paths |",
        "| --- | --- | --- |",
    ]
    for task in tasks:
        allowed = cell(task["allowed_paths"], "(unspecified)")
        protected = cell(task["protected_paths"], "(none)")
        lines.append(f"| {task['name']} | {allowed} | {protected} |")
    lines.extend(["", "Edit only within your scope and never edit sibling-owned paths."])

    source_specs = {task["spec"] for task in tasks if task["spec"] is not None}
    for task in tasks:
        if task["spec"] is not None:
            path = task["out_dir"] / "effective_spec.md"
            if path.resolve() in source_specs:
                raise ValueError(f"effective spec path overlaps a source spec: {path}")
            task["effective_spec"] = path
    return ("\n\n" + "\n".join(lines) + "\n").encode("utf-8")


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
    spec = task.get("effective_spec", task["spec"])
    if spec is not None:
        cmd.extend(["--spec", str(spec)])
    if task["profiles_file"] is not None:
        cmd.extend(["--profiles-file", str(task["profiles_file"])])
    if task["contract"] is not None:
        cmd.extend(["--contract", str(task["contract"])])
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


def run_one(task, run_codex, options, table):
    task_out = task["out_dir"]
    start = time.monotonic()
    returncode = 1
    errors = []
    data = {}
    worktree = task["worktree"]
    invocation_id = uuid.uuid4().hex
    task_out_ready = False
    try:
        ensure_private_dir(task_out)
        task_out_ready = True
        stdout_log = task_out / "wrapper.stdout.log"
        stderr_log = task_out / "wrapper.stderr.log"
        summary_file = task_out / "summary.json"
        previous_summary = fingerprint(summary_file)
        with stdout_log.open("wb") as stdout, stderr_log.open("wb") as stderr:
            if "effective_spec" in task:
                atomic_write(task["effective_spec"], task["spec"].read_bytes() + table)
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
    policies = {}
    violation_status = None
    if task["worktree_registered"] and task_out_ready:
        try:
            custody = verify.export_and_verify_patch(worktree, base_oid, task_out / "changes.patch")
            final_tree_oid = custody["final_tree_oid"]
            reconstructed_tree_oid = custody["reconstructed_tree_oid"]
            custody_verified = custody["custody_verified"]
            changed_files = custody["changed_files"]
            patch_file = custody["patch_file"]
            patch_bytes = custody["patch_bytes"]
            if not custody_verified:
                errors.append(f"patch custody mismatch: reconstructed tree {reconstructed_tree_oid} "
                              f"differs from final tree {final_tree_oid}")
                returncode = 1
            policies = verify.check_path_policies(
                changed_files, allowed_paths=task["allowed_paths"], protected_paths=task["protected_paths"],
            )
            if task["contract_allowed"] and task["contract_allowed"] != task["allowed_paths"]:
                contract_policy = verify.check_path_policies(changed_files, allowed_paths=task["contract_allowed"])
                policies["allowed_violations"] = sorted(set(
                    policies["allowed_violations"] + contract_policy["allowed_violations"]
                ))
                policies["ok"] = policies["ok"] and contract_policy["ok"]
            if not policies["ok"]:
                violation_status = ("protected_path_violation" if policies["protected_violations"]
                                    else "scope_violation")
                errors.append(f"{violation_status}: "
                              f"protected={policies['protected_violations']!r}, "
                              f"outside allowed paths={policies['allowed_violations']!r}")
                returncode = 5

            if returncode == 0 and not errors and custody_verified and not options.keep_worktrees:
                remove_worktree(task["git_root"], worktree, task["lock"], registered=True)
                worktree = None
        except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
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
        "status": violation_status or (
            "error" if errors else data.get("status", "ok" if returncode == 0 else "error")
        ),
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
    if policies:
        data.update(protected_violations=policies["protected_violations"],
                    allowed_violations=policies["allowed_violations"])
    if errors:
        data["error"] = "\n".join(errors)
    return data


def check_base_reconstruction(root, base_oid, lock, patches, worktrees, errors, *, snapshot):
    created = False
    try:
        verify_parent = Path(tempfile.mkdtemp(prefix="merge_check_", dir=worktrees))
        worktree = verify_parent / "wt"
        add_worktree(root, worktree, base_oid, lock)
        created = True
        # Staging the patches retains additions that were force-staged in a worker.
        apply = ["apply", "--binary", "--index"] if snapshot else ["apply", "--binary"]
        for patch in patches:
            try:
                git(worktree, *apply, patch)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Sequential apply failed for {patch}: {error_text(exc)}")
                return False, None
        return True, verify.snapshot_worktree_tree(worktree) if snapshot else None
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        errors.append(f"Verify worktree {'snapshot' if created else 'setup'}: {error_text(exc)}")
        return False, None
    finally:
        if created:
            try:
                remove_worktree(root, worktree, lock, registered=True)
                verify_parent.rmdir()
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Verify worktree cleanup: {error_text(exc)}")


def patch_groups(tasks, results, *, include_empty):
    groups = {}
    for task, result in zip(tasks, results):
        if task["isolation"] == "managed-worktree" and (include_empty or result["patch_bytes"] > 0):
            groups.setdefault((task["git_root"], task["base_oid"]), []).append((task, result))
    return groups


def check_merges(groups, worktrees, *, snapshot=False):
    checks = []
    trees = {}
    for (root, base_oid), group in groups.items():
        lock = group[0][0]["lock"]
        patches = [result["patch_file"] for _, result in group if result["patch_bytes"] > 0]
        counts = Counter(path for _, result in group for path in set(result["changed_files"]))
        apply = ["git", "-C", str(root), "apply", *patches]
        errors = []
        target_apply_ok = True
        if patches:
            try:
                git(root, "apply", "--check", *patches)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"Working tree apply check: {error_text(exc)}")
                target_apply_ok = False
        base_reconstruct_ok, tree = check_base_reconstruction(
            root, base_oid, lock, patches, worktrees, errors, snapshot=snapshot,
        )
        trees[root, base_oid] = tree

        checks.append({
            "git_root": str(root),
            "base_oid": base_oid,
            "base_reconstruct_ok": base_reconstruct_ok,
            "target_apply_ok": target_apply_ok,
            "ok": base_reconstruct_ok and target_apply_ok,
            "overlapping_files": sorted(path for path, count in counts.items() if count >= 2),
            "patches": patches,
            "apply_command": " ".join(shlex.quote(arg) for arg in apply) if patches else "",
            "error": "\n".join(errors),
        })
    return checks, trees


def run_verifications(groups, checks, trees, failed_groups, commands, worktrees):
    runs = []
    multiple = len(groups) > 1
    for idx, ((root, base_oid), check) in enumerate(zip(groups, checks), start=1):
        key = (root, base_oid)
        if not check["base_reconstruct_ok"] or key in failed_groups:
            continue
        argv, source, timeout = commands[key]
        out_path = worktrees.parent / (f"verification-{idx}.json" if multiple else "verification.json")
        record = verify.run_host_verification(
            root, base_oid=base_oid, patches=check["patches"], expected_tree_oid=trees[key],
            verify_argv=argv, out_path=out_path, worktrees_parent=worktrees,
            lock=groups[key][0][0]["lock"], timeout_s=timeout, policy_source=source,
        )
        runs.append({**record, "git_root": str(root), "verification_file": str(out_path)})
    ok = None
    if any(not run["ok"] for run in runs):
        ok = False
    elif len(runs) == len(groups):
        ok = True
    return ok, runs if multiple else next(iter(runs), None)


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
        if check["apply_command"]:
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
    verify_requested = options.verify_cmd is not None or options.verify_preset is not None
    try:
        if not options.out_dir:
            raise ValueError("--out-dir requires a non-empty path")
        out_dir = Path(options.out_dir).resolve()
        tasks, commands = prepare_tasks(options, out_dir, verify_requested)
        table = ownership_table(tasks, options)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(error_text(exc))
    if not os.access(run_codex, os.X_OK):
        print(f"Error: missing executable run_codex.sh at {run_codex}", file=sys.stderr)
        return 127

    start = time.monotonic()
    ensure_private_dir(out_dir)
    worktrees = out_dir / "_worktrees"
    managed = [task for task in tasks if task["isolation"] == "managed-worktree"]
    if managed:
        # Multiple invocations can point their merge checks at a shared worktrees parent.
        ensure_private_dir(worktrees.resolve())
    for task in managed:
        path = worktrees / task["name"]
        try:
            add_worktree(task["git_root"], path, task["base_oid"], task["lock"])
            task["worktree_registered"] = True
            task["worktree"] = path
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            task["setup_error"] = error_text(exc)

    with ThreadPoolExecutor(max_workers=min(options.max_parallel, len(tasks))) as pool:
        futures = [pool.submit(run_one, task, run_codex, options, table) for task in tasks]
        results = [future.result() for future in futures]
    groups = patch_groups(tasks, results, include_empty=verify_requested)
    checks, trees = check_merges(groups, worktrees, snapshot=verify_requested)
    merge_ok = all(check["ok"] for check in checks)
    failed_groups = {(task["git_root"], task["base_oid"]) for task, result in zip(tasks, results)
                     if result["exit_code"] != 0}
    all_ok = merge_ok and all(not check["error"] for check in checks) and not failed_groups
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
    if table is not None:
        summary["ownership_table_injected"] = True
    exit_code = 0 if all_ok else 1
    for status in ("protected_path_violation", "scope_violation"):
        if any(result["status"] == status and result["exit_code"] == 5 for result in results):
            summary["status"], exit_code = status, 5
            break
    if verify_requested:
        ok, records = run_verifications(groups, checks, trees, failed_groups, commands, worktrees)
        summary.update(verification_ok=ok, verification=records)
        if ok is False:
            summary["status"], exit_code = "verification_failed", 6
    summary_path = out_dir / "parallel-summary.json"
    atomic_write(summary_path, (json.dumps(summary, indent=2) + "\n").encode("utf-8"))
    print_summary(summary, summary_path)
    return exit_code
