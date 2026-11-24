"""Host-side policy, Git custody, and verification for orchestrated changes."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .env import build_child_env, redact_secrets
from .supervisor import _process_needs_cleanup, terminate_process_group

CONTRACT_FILENAME = ".codex-orchestrator.json"
ALLOWED_CONTRACT_KEYS = frozenset({
    "version", "allowed_paths", "protected_paths", "dirty_policy", "max_timeout_s",
    "verify_commands",
})

_GIT_REPO_ENV = frozenset({
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH",
})
_CREDENTIAL_KEY = re.compile(r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)")
_TAIL_BYTES = 32768
_TAIL_CHARS = 8000

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_COLLECTION_ERROR = re.compile(
    r"\b(?:ModuleNotFoundError|ImportError|SyntaxError|ERROR collecting)\b", re.IGNORECASE
)
_UNITTEST_COLLECTED = re.compile(r"(?m)^\s*Ran (\d+) tests?\b")
_UNITTEST_SUMMARY = re.compile(r"(?m)^\s*(?:OK|FAILED)(?: \(([^()\n]*)\))?\s*$")
_PYTEST_COLLECTED = re.compile(r"\bcollected (\d+) items?\b")
_PYTEST_COUNTS = re.compile(
    r"\b(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b"
)
_PYTEST_SUMMARY = re.compile(
    r"^(?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b|no tests ran\b)"
)


def _git(repo: Path, *args: str, index: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key not in _GIT_REPO_ENV}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        env=env, capture_output=True, check=True, **kwargs,
    )


def _oid(repo: Path, *args: str, index: Path | None = None) -> str:
    return _git(repo, *args, index=index).stdout.decode("ascii").strip()


def _paths(nul_delimited: bytes) -> list[str]:
    return [os.fsdecode(value) for value in nul_delimited.split(b"\0") if value]


def _changed_paths(repo: Path, base_oid: str, tree_oid: str) -> list[str]:
    names = _git(
        repo, "diff", "--no-renames", "--no-relative", "--name-only", "-z", base_oid, tree_oid, "--"
    ).stdout
    return sorted(_paths(names))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        return exc.stderr.decode("utf-8", errors="replace").strip()
    return str(exc)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temp = Path(stream.name)
    try:
        with stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _is_argv(value: object) -> bool:
    return (isinstance(value, list) and bool(value) and bool(value[0])
            and all(isinstance(arg, str) and "\0" not in arg for arg in value))


def validate_path_globs(patterns: Sequence[str], label: str) -> None:
    for pattern in patterns:
        if not pattern.strip() or pattern.startswith("/") or ".." in pattern.split("/"):
            raise ValueError(f"{label} requires a non-empty repository-relative glob, got {pattern!r}")


def _validate_contract(contract: object) -> None:
    if not isinstance(contract, dict):
        raise ValueError("repository contract must be a JSON object")
    unknown = contract.keys() - ALLOWED_CONTRACT_KEYS
    if unknown:
        raise ValueError(f"unknown repository contract keys: {', '.join(sorted(unknown))}")
    for key in ("allowed_paths", "protected_paths"):
        if key in contract:
            patterns = contract[key]
            if not isinstance(patterns, list) or any(
                not isinstance(pattern, str) or not pattern.strip() for pattern in patterns
            ):
                raise ValueError(f"{key} must be a list of non-empty strings")
            validate_path_globs(patterns, key)
    if "dirty_policy" in contract and contract["dirty_policy"] not in ("fail", "head"):
        raise ValueError("dirty_policy must be 'fail' or 'head'")
    if "max_timeout_s" in contract:
        timeout = contract["max_timeout_s"]
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("max_timeout_s must be a positive integer")
    if "verify_commands" in contract:
        commands = contract["verify_commands"]
        if not isinstance(commands, dict) or any(
            not isinstance(name, str) or not _is_argv(argv) for name, argv in commands.items()
        ):
            raise ValueError("verify_commands must map preset names to non-empty argv lists of strings")


def load_trusted_contract(
    repo_root: Path, base_oid: str, *, contract_path: str | Path | None = None
) -> tuple[dict, str]:
    source = f"git:{base_oid}:{CONTRACT_FILENAME}"
    try:
        if contract_path is not None:
            path = Path(contract_path).resolve()
            source = f"file:{path}"
            raw = path.read_bytes()
        else:
            base = _oid(repo_root, "rev-parse", "--verify", "--end-of-options", f"{base_oid}^{{object}}")
            entry = _git(repo_root, "ls-tree", "-z", base, "--", CONTRACT_FILENAME).stdout
            if not entry:
                return {}, "none"
            if entry.split(b" ", 2)[:2] not in ([b"100644", b"blob"], [b"100755", b"blob"]):
                raise ValueError("repository contract must be a regular file at the base commit")
            raw = _git(repo_root, "show", f"{base}:{CONTRACT_FILENAME}").stdout
        contract = json.loads(raw.decode("utf-8"))
        _validate_contract(contract)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"invalid repository contract at {source}: {_error_text(exc)}") from exc
    return contract, source


def match_repo_glob(rel_path: str, pattern: str) -> bool:
    path = PurePosixPath(rel_path)
    glob = PurePosixPath(pattern)
    if path.is_absolute() or glob.is_absolute() or not path.parts or not glob.parts:
        return False
    positions = {0}
    for segment in glob.parts:
        if segment == "**":
            positions = set(range(min(positions), len(path.parts) + 1))
        else:
            positions = {
                index + 1 for index in positions
                if index < len(path.parts) and fnmatchcase(path.parts[index], segment)
            }
        if not positions:
            return False
    return len(path.parts) in positions


def check_path_policies(
    changed_files: Sequence[str],
    *,
    allowed_paths: Sequence[str] = (),
    protected_paths: Sequence[str] = (),
    exact_protected_paths: Sequence[str] = (),
) -> dict:
    exact_set = {PurePosixPath(path).as_posix() for path in exact_protected_paths}
    protected = [
        path for path in changed_files
        if PurePosixPath(path).as_posix() in exact_set
        or any(match_repo_glob(path, pat) for pat in protected_paths)
    ]
    outside_scope = [
        path for path in changed_files
        if allowed_paths and not any(match_repo_glob(path, pat) for pat in allowed_paths)
    ]
    return {
        "ok": not (protected or outside_scope),
        "protected_violations": protected,
        "allowed_violations": outside_scope,
    }


def snapshot_worktree_tree(worktree: Path) -> str:
    if _git(worktree, "ls-files", "-u").stdout:
        raise ValueError("worker index contains unmerged entries")
    index_path = _git(worktree, "rev-parse", "--git-path", "index").stdout
    real_index = worktree / os.fsdecode(index_path.removesuffix(b"\n"))
    with tempfile.TemporaryDirectory() as private_dir:
        index = Path(private_dir) / "snapshot.index"
        try:
            shutil.copyfile(real_index, index)
        except FileNotFoundError:
            _git(worktree, "read-tree", "HEAD", index=index)
        else:
            _git(worktree, "add", "-A", "--", ".", index=index)
            staged_tree = _oid(worktree, "write-tree", index=index)
            # Discard the cached stat data before checking the worktree again.
            index.unlink()
            _git(worktree, "read-tree", staged_tree, index=index)
        _git(worktree, "add", "-A", "--", ".", index=index)
        return _oid(worktree, "write-tree", index=index)


def export_and_verify_patch(worktree: Path, base_ref: str, patch_path: Path) -> dict:
    final_tree = snapshot_worktree_tree(worktree)
    base_tree = _oid(worktree, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{tree}}")
    patch = _git(
        worktree, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--no-relative", "--src-prefix=a/", "--dst-prefix=b/", base_tree, final_tree, "--",
    ).stdout
    changed_files = _changed_paths(worktree, base_tree, final_tree)
    with tempfile.TemporaryDirectory() as private_dir:
        index = Path(private_dir) / "verify.index"
        _git(worktree, "read-tree", base_tree, index=index)
        if patch:
            _git(worktree, "apply", "--cached", "--binary", "-", index=index, input=patch)
        reconstructed = _oid(worktree, "write-tree", index=index)
    if reconstructed != final_tree:
        raise ValueError(f"patch custody mismatch: reconstructed tree {reconstructed} differs from {final_tree}")
    _atomic_write(patch_path, patch)
    return {
        "base_oid": base_ref,
        "final_tree_oid": final_tree,
        "reconstructed_tree_oid": reconstructed,
        "custody_verified": True,
        "changed_files": changed_files,
        "patch_bytes": len(patch),
        "patch_file": str(patch_path),
    }


def resolve_verify_argv(
    contract: dict, *, verify_cmd: str | None = None, verify_preset: str | None = None
) -> tuple[list[str], str]:
    if verify_cmd is not None and verify_preset is not None:
        raise ValueError("--verify-cmd and --verify-preset are mutually exclusive")
    if verify_preset is not None:
        argv = contract.get("verify_commands", {}).get(verify_preset)
        if not _is_argv(argv):
            raise ValueError(f"unknown or invalid verification preset: {verify_preset!r}")
        return list(argv), f"preset:{verify_preset}"
    if verify_cmd is None:
        return [], "none"
    argv = json.loads(verify_cmd) if verify_cmd.lstrip().startswith("[") else shlex.split(verify_cmd)
    if not _is_argv(argv):
        raise ValueError("--verify-cmd must specify a non-empty argv list of strings")
    return argv, "cli"


@contextmanager
def _detached_worktree(
    repo: Path, base_oid: str, parent: Path, lock: threading.Lock | None, prefix: str
) -> Iterator[Path]:
    parent.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix=prefix, dir=parent.resolve()))
    try:
        with lock if lock is not None else nullcontext():
            _git(repo, "worktree", "add", "--detach", "--", str(worktree), base_oid)
    except BaseException:
        # Git rolls back a failed add. Remove the empty mkdtemp reservation if it remains.
        if worktree.is_dir() and not any(worktree.iterdir()):
            worktree.rmdir()
        raise
    try:
        yield worktree
    finally:
        with lock if lock is not None else nullcontext():
            try:
                _git(repo, "worktree", "remove", "--force", "--", str(worktree))
            finally:
                _git(repo, "worktree", "prune")


def _verify_cwd(worktree: Path, cwd_rel: str) -> Path:
    relative = Path(cwd_rel)
    cwd = (worktree / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not cwd.is_relative_to(worktree):
        raise ValueError(f"verification cwd must be inside the worktree: {cwd_rel!r}")
    return cwd


def _read_tail(stream) -> str:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - _TAIL_BYTES))
    tail = stream.read(_TAIL_BYTES)
    if size > _TAIL_BYTES:
        # Avoid emitting the suffix of a credential cut off by the byte limit.
        tail = tail.partition(b"\n")[2]
    return redact_secrets(tail.decode("utf-8", errors="replace"))[-_TAIL_CHARS:]


def _run_verify_command(argv: list[str], worktree: Path, cwd: Path, timeout_s: int) -> tuple[int, bool, str, str]:
    env = {
        key: value for key, value in build_child_env().items()
        if not _CREDENTIAL_KEY.search(key) and redact_secrets(value) == value
    }
    env["PWD"] = str(cwd)
    env["GIT_CEILING_DIRECTORIES"] = str(worktree.parent)
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=stdout,
            stderr=stderr, start_new_session=True,
        )
        timed_out = False
        try:
            exit_code = proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
        finally:
            if _process_needs_cleanup(proc):
                terminate_process_group(proc.pid, reap=proc.poll)
            proc.wait()
        if exit_code < 0:
            exit_code = 128 - exit_code
        return exit_code, timed_out, _read_tail(stdout), _read_tail(stderr)


def run_host_verification(
    repo_root: Path,
    *,
    base_oid: str,
    patches: Sequence[str | Path],
    expected_tree_oid: str | None,
    verify_argv: Sequence[str],
    out_path: Path,
    worktrees_parent: Path,
    lock: threading.Lock | None = None,
    cwd_rel: str = ".",
    timeout_s: int = 120,
    policy_source: str = "cli",
    protected_violations: Sequence[str] = (),
    allowed_violations: Sequence[str] = (),
    test_snapshot_tree: str | None = None,
) -> dict:
    started = time.monotonic()
    result = {
        "ok": False,
        "base_oid": base_oid,
        "test_snapshot_tree": test_snapshot_tree,
        "candidate_tree_oid": expected_tree_oid,
        "reconstructed_tree_oid": None,
        "argv": list(verify_argv),
        "cwd_rel": cwd_rel,
        "policy_source": policy_source,
        "protected_violations": list(protected_violations),
        "allowed_violations": list(allowed_violations),
        "exit_code": 125,
        "timed_out": False,
        "duration_s": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    try:
        if protected_violations or allowed_violations:
            result["exit_code"] = 5
            raise ValueError("verification blocked by the repository path policy")
        if not _is_argv(result["argv"]):
            raise ValueError("verification requires a non-empty argv list of strings")
        if timeout_s <= 0:
            raise ValueError("verification timeout must be positive")
        patch_paths = [Path(patch).resolve(strict=True) for patch in patches]
        with _detached_worktree(repo_root, base_oid, worktrees_parent, lock, "verify_pow_") as worktree:
            for patch in patch_paths:
                if patch.stat().st_size:
                    # Stage patch additions so ignored but intentionally tracked files survive.
                    _git(worktree, "apply", "--binary", "--index", str(patch))
            reconstructed = snapshot_worktree_tree(worktree)
            result["reconstructed_tree_oid"] = reconstructed
            if expected_tree_oid is not None and reconstructed != expected_tree_oid:
                raise ValueError(f"verification tree mismatch: expected {expected_tree_oid}, reconstructed {reconstructed}")
            cwd = _verify_cwd(worktree, cwd_rel)
            exit_code, timed_out, stdout, stderr = _run_verify_command(result["argv"], worktree, cwd, timeout_s)
            result.update(exit_code=exit_code, timed_out=timed_out, stdout_tail=stdout, stderr_tail=stderr)
        result["ok"] = result["exit_code"] == 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        result["ok"] = False
        if result["exit_code"] == 0:
            result["exit_code"] = 125
        detail = redact_secrets(_error_text(exc))
        result["stderr_tail"] = "\n".join(filter(None, (result["stderr_tail"], detail)))[-_TAIL_CHARS:]
    result["duration_s"] = round(time.monotonic() - started, 3)
    _atomic_write(out_path, (json.dumps(result, indent=2) + "\n").encode("utf-8"))
    return result


def analyze_test_outcome(exit_code: int, stdout: str, stderr: str, *, allow_skips: bool = False) -> dict:
    output = _ANSI_CSI.sub("", f"{stdout}\n{stderr}")
    collection_error = bool(_COLLECTION_ERROR.search(output))
    collected = None
    skipped = 0
    failed = 0
    ran = _UNITTEST_COLLECTED.findall(output)
    collected_items = _PYTEST_COLLECTED.findall(output)
    if ran:
        collected = int(ran[-1])
    elif collected_items:
        collected = int(collected_items[-1])
    for summary in _UNITTEST_SUMMARY.findall(output):
        counts = {key: int(count) for key, count in re.findall(r"\b(\w+)=(\d+)\b", summary)}
        skipped = counts.get("skipped", 0)
        failed = counts.get("failures", 0)
        collection_error |= bool(counts.get("errors", 0))

    for line in output.splitlines():
        line = line.strip(" =")
        if not _PYTEST_SUMMARY.match(line):
            continue
        counts = {kind: int(count) for count, kind in _PYTEST_COUNTS.findall(line)}
        if collected is None:
            collected = sum(counts.values())
        skipped = max(skipped, counts.get("skipped", 0))
        failed = max(failed, counts.get("failed", 0))
        collection_error |= bool(counts.get("error", 0) or counts.get("errors", 0))
    collection_error |= "0" in ran or "0" in collected_items or collected == 0

    red_valid = exit_code != 0 and not collection_error and bool(collected) and failed > 0
    green_valid = (
        exit_code == 0 and not collection_error and bool(collected)
        and (allow_skips or skipped == 0)
    )
    if collection_error:
        reason = "test collection failed or no tests ran"
    elif red_valid:
        reason = "test command reported a test failure"
    elif green_valid:
        reason = "test command passed"
    elif skipped and not allow_skips:
        reason = "test command reported unexpected skips"
    elif exit_code == 0 and collected is None:
        reason = "test command did not report a test count"
    else:
        reason = "test command did not report a test failure"
    return {
        "collected": collected, "skipped": skipped, "collection_error": collection_error,
        "red_valid": red_valid, "green_valid": green_valid, "reason": reason,
    }


def _overlay_files(directory: Path) -> Iterator[Path]:
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            yield entry
        else:
            yield from _overlay_files(entry)


def _copy_test_overlay(overlay: Path, worktree: Path) -> list[str]:
    if overlay.is_symlink() or not overlay.is_dir():
        raise ValueError("test overlay must be a directory")
    relative_paths = []
    symlinks = []
    for source in _overlay_files(overlay):
        relative = source.relative_to(overlay)
        if any(part.lower() in (".git", "..") for part in relative.parts):
            raise ValueError(f"unsafe test overlay path: {relative}")
        target = worktree / relative
        for parent in target.parents:
            if parent == worktree:
                break
            if parent.is_symlink():
                raise ValueError(f"test overlay path traverses a symlink: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        if source.is_symlink():
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(source))
            symlinks.append(target)
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise ValueError(f"test overlay entry is not a regular file or symlink: {relative}")
        relative_paths.append(relative.as_posix())
    for link in symlinks:
        resolved = link.resolve()
        if not resolved.is_relative_to(worktree) or any(
            part.lower() == ".git" for part in resolved.relative_to(worktree).parts
        ):
            raise ValueError(
                f"test overlay symlink escapes the worktree or targets Git metadata: {link.relative_to(worktree)}"
            )
    if relative_paths:
        _git(
            worktree, "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
            input=b"\0".join(map(os.fsencode, relative_paths)) + b"\0",
        )
    return relative_paths


def create_test_snapshot(
    repo_root: Path,
    base_oid: str,
    *,
    test_overlay_dir: Path | None,
    test_paths: Sequence[str],
    worktrees_parent: Path,
    lock: threading.Lock | None = None,
) -> dict:
    """Pin tests from the base and any explicitly supplied overlay.

    A caller that opts into working-tree tests can pass the selected files as an overlay.
    """
    with _detached_worktree(repo_root, base_oid, worktrees_parent, lock, "verify_tdd_") as worktree:
        overlay_paths = []
        if test_overlay_dir is not None:
            overlay_paths = _copy_test_overlay(test_overlay_dir, worktree)
        if not overlay_paths:
            if not test_paths:
                raise ValueError("test snapshot requires at least one frozen file")
            base_paths = _paths(_git(worktree, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout)
            for pattern in test_paths:
                if not any(match_repo_glob(path, pattern) for path in base_paths):
                    raise ValueError(f"test path matches no files at the base commit: {pattern!r}")
        tree = snapshot_worktree_tree(worktree)
        commit = _oid(
            worktree, "-c", "user.name=Codex Orchestrator", "-c", "user.email=codex-orchestrator@localhost",
            "commit-tree", tree, "-p", base_oid, "-m", "codex-orchestrator tdd test snapshot",
        )
        protected_globs = set(test_paths)
        protected_exact = set(overlay_paths) | set(_changed_paths(worktree, "HEAD", tree))
    return {
        "base_oid": base_oid,
        "test_snapshot_tree": tree,
        "test_snapshot_commit": commit,
        "protected_test_globs": sorted(protected_globs),
        "protected_test_exact": sorted(protected_exact),
        "protected_test_paths": sorted(protected_globs | protected_exact),
    }
