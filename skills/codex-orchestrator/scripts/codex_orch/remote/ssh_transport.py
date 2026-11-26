"""Multiplexed SSH transport and remote task lifecycle operations."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import selectors
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..env import ensure_private_dir, redact_secrets
from .models import HostConfig, TaskSpec


_SSH_ENV_KEYS = frozenset("""
    PATH HOME USER LOGNAME SHELL TERM TMPDIR TEMP TMP LANG TZ
    SSH_AUTH_SOCK SSH_AGENT_PID KRB5CCNAME
""".split())
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MUX_ERRORS = ("ControlSocket", "mux_client", "Broken pipe")
_MIB = 1024 * 1024
_CHUNK_SIZE = 64 * 1024


@dataclass
class SshResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None


def build_ssh_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    return {
        key: value for key, value in source.items()
        if key in _SSH_ENV_KEYS or key.startswith(("XDG_", "LC_"))
    }


class ControlSocketManager:
    def __init__(self, socket_dir: Path | None = None):
        directory = Path(socket_dir) if socket_dir is not None else Path(f"/tmp/co-ssh-{os.geteuid()}")
        self.sock_dir = directory.absolute()
        self.control_path()
        try:
            ensure_private_dir(self.sock_dir)
        except OSError as exc:
            raise type(exc)(redact_secrets(str(exc))) from None

    def control_path(self) -> str:
        # OpenSSH expands percent tokens within ControlPath itself.
        if "%" in os.fspath(self.sock_dir):
            raise ValueError("The SSH control socket directory must not contain percent signs.")
        if len(os.fsencode(self.sock_dir / ("0" * 40))) >= 104:
            raise ValueError("The expanded SSH control socket path must be shorter than 104 bytes.")
        return f"{self.sock_dir}/%C"

    def build_ssh_argv(
        self, host: HostConfig, *, control_persist: str = "10m", extra_args: Sequence[str] = (),
    ) -> list[str]:
        argv = [
            "ssh", "-p", str(host.ssh_port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", f"ControlPersist={control_persist}",
            "-o", f"ControlPath={self.control_path()}",
        ]
        for option in host.ssh_options:
            argv.extend(("-o", option))
        return [*argv, *extra_args, "--", host.ssh_target]


def _decode(output: bytes | str | None) -> str:
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output or ""


def _read_capture(capture: io.BufferedIOBase) -> bytes:
    capture.seek(0)
    return capture.read()


def _result(
    started: float, code: int, stdout: bytes | str | None = None, stderr: bytes | str | None = None,
    *, error: str | None = None, timed_out: bool = False,
) -> SshResult:
    diagnostic = redact_secrets(_decode(stderr))
    if code and error is None:
        error = diagnostic.strip() or f"SSH exited with status {code}."
    return SshResult(
        code == 0, code, _decode(stdout), diagnostic, time.monotonic() - started,
        timed_out=timed_out, error=redact_secrets(error) if error else None,
    )


def _timeout_result(started: float, exc: subprocess.TimeoutExpired) -> SshResult:
    return _result(
        started, 124, exc.output, exc.stderr,
        timed_out=True, error=f"SSH operation timed out after {exc.timeout:g} seconds.",
    )


def _exports(env: Mapping[str, str]) -> str:
    exports = []
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name):
            raise ValueError("Remote environment variable names must be shell identifiers.")
        exports.append(f"export {name}={shlex.quote(value)}\n")
    return "".join(exports)


def _python_command(script: str, *args: str) -> str:
    return shlex.join(("python3", "-c", script, *args))


_DONE_FUNCTION = """write_done() {
    finished_at=$(command -p date +%s) || return 1
    duration_s=$((finished_at - started_at))
    if [ "$2" = true ] || [ "$duration_s" -lt 0 ]; then
        duration_s=0
    fi
    printf '{"exit_code":%s,"started_at":%s,"finished_at":%s,"duration_s":%s,"pid":%s,"pgid":%s,"gpu_ids":%s,"skipped_existing":%s}\\n' \\
        "$1" "$started_at" "$finished_at" "$duration_s" "$$" "$$" "$gpu_ids_json" "$2" \\
        > "$ATTEMPT_DIR/done.json.tmp" &&
        command -p mv -f "$ATTEMPT_DIR/done.json.tmp" "$ATTEMPT_DIR/done.json"
}
"""

_LAUNCH = """
import base64
import os
import subprocess
import sys
import tempfile

out_dir, attempt_dir = sys.argv[1:]
os.makedirs(out_dir, exist_ok=True)
os.makedirs(attempt_dir, exist_ok=True)
for name, mode in (("env.sh", 0o600), ("run.sh", 0o700)):
    content = base64.b64decode(sys.stdin.buffer.readline().rstrip(b"\\n"), validate=True)
    fd, temporary = tempfile.mkstemp(dir=attempt_dir)
    try:
        with os.fdopen(fd, "wb") as script:
            os.fchmod(script.fileno(), mode)
            script.write(content)
        os.replace(temporary, os.path.join(attempt_dir, name))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
process = subprocess.Popen(
    ["/bin/sh", os.path.join(attempt_dir, "run.sh")],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    shell=False, start_new_session=True,
)
print(process.pid)
"""

_LAUNCH_FALLBACK = """
set -e
command -v setsid >/dev/null
command -v nohup >/dev/null
umask 077
mkdir -p -- "$out_dir" "$attempt_dir"
IFS= read -r env_data
IFS= read -r run_data
write_script() {
    temporary=$(mktemp "$attempt_dir/.script.XXXXXXXX")
    if { printf '%s' "$1" | base64 -d > "$temporary" 2>/dev/null; } ||
       { printf '%s' "$1" | base64 -D > "$temporary"; }; then
        chmod "$2" "$temporary" && mv -f "$temporary" "$attempt_dir/$3" && return 0
    fi
    rm -f "$temporary"
    return 1
}
write_script "$env_data" 600 env.sh
write_script "$run_data" 700 run.sh
nohup setsid /bin/sh "$attempt_dir/run.sh" </dev/null >/dev/null 2>&1 &
printf '%s\\n' "$!"
"""

_KILL_GROUP = """
import os
import signal
import sys
import time

pgid = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
try:
    os.killpg(pgid, signal.SIGTERM)
    while True:
        os.killpg(pgid, 0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.killpg(pgid, signal.SIGKILL)
            break
        time.sleep(min(0.5, remaining))
except ProcessLookupError:
    pass
"""

# Use the same rules for both output copy operations. Match relative paths and basenames.
_REMOTE_COPY = """
import fnmatch
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile

mode, source, target, rules_json = sys.argv[1:]
rules = json.loads(rules_json)
source = os.path.realpath(source)

def excluded(name):
    if name.startswith("./"):
        name = name[2:]
    return any(fnmatch.fnmatchcase(value, rule)
               for rule in rules for value in (name, os.path.basename(name)))

class BoundedOutput:
    def __init__(self, limit):
        self.remaining = limit

    def write(self, data):
        if len(data) > self.remaining:
            raise ValueError("Tar stream exceeds the byte limit.")
        self.remaining -= len(data)
        return sys.stdout.buffer.write(data)

if not os.path.isdir(source):
    raise ValueError("Remote output directory does not exist.")
if mode == "pull":
    def archive_filter(member):
        if member.name != "." and excluded(member.name):
            return None
        if not (member.isdir() or member.isfile()):
            raise ValueError("Remote output contains an unsupported tar member type.")
        return member

    with tarfile.open(fileobj=BoundedOutput(int(target)), mode="w|", dereference=False) as archive:
        archive.add(source, arcname=".", filter=archive_filter)
else:
    target = os.path.abspath(target)
    resolved_target = os.path.realpath(target)
    if os.path.commonpath((source, resolved_target)) in (source, resolved_target):
        raise ValueError("Remote output and mirror directories overlap.")
    if os.path.islink(target):
        raise ValueError("Remote mirror directory must not be a symlink.")
    os.makedirs(target, exist_ok=True)
    for directory, dirs, files in os.walk(source):
        relative = os.path.relpath(directory, source)
        destination = os.path.join(target, relative)
        dirs[:] = sorted(name for name in dirs if not excluded(os.path.join(relative, name)))
        for name in dirs:
            src = os.path.join(directory, name)
            dst = os.path.join(destination, name)
            if os.path.islink(src) or os.path.islink(dst):
                raise ValueError("Remote mirror encountered a symlink directory.")
            os.makedirs(dst, exist_ok=True)
        for name in sorted(files):
            if excluded(os.path.join(relative, name)):
                continue
            src = os.path.join(directory, name)
            dst = os.path.join(destination, name)
            if not stat.S_ISREG(os.lstat(src).st_mode) or (
                os.path.lexists(dst) and not stat.S_ISREG(os.lstat(dst).st_mode)
            ):
                raise ValueError("Remote mirror encountered an unsupported file type.")
            # Replace the directory entry rather than writing through existing hard links.
            fd, temporary = tempfile.mkstemp(dir=destination)
            os.close(fd)
            try:
                shutil.copy2(src, temporary)
                os.replace(temporary, dst)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
"""


def _extract_tar(archive_file: io.BufferedIOBase, dest_dir: Path, limit: int) -> None:
    root = dest_dir.resolve()
    with tarfile.open(fileobj=archive_file, mode="r:") as archive:
        entries = {}
        total_size = 0
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise ValueError("Unsafe tar member path or type.")
            target = root.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root) or any(
                part.is_symlink() for part in (target, *target.parents) if part.is_relative_to(root)
            ):
                raise ValueError("Tar member resolves through an unsafe destination path.")
            if member.isfile():
                if target == root or member.size < 0:
                    raise ValueError("Invalid tar file member.")
                total_size += member.size
                if total_size > limit:
                    raise ValueError("Extracted files exceed the byte limit.")
            if target in entries and not (member.isdir() and entries[target].isdir()):
                raise ValueError("Duplicate tar member path.")
            entries[target] = member

        for target, member in entries.items():
            if any(parent in entries and not entries[parent].isdir() for parent in target.parents):
                raise ValueError("A tar member would be extracted below a file.")
            if target.exists() and (
                (member.isdir() and not target.is_dir())
                or (member.isfile() and not target.is_file())
            ):
                raise ValueError("A tar member conflicts with a destination entry.")

        root.mkdir(parents=True, exist_ok=True)
        for target, member in entries.items():
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                try:
                    with archive.extractfile(member) as source:
                        shutil.copyfileobj(source, temporary, length=_CHUNK_SIZE)
                    temporary.close()
                    os.replace(temporary.name, target)
                finally:
                    Path(temporary.name).unlink(missing_ok=True)


def _byte_limit(max_mb: int) -> int:
    if type(max_mb) is not int or max_mb < 0:
        raise ValueError("The tar size limit must be a nonnegative integer in MiB.")
    return max_mb * _MIB


def safe_extract_tar_bytes(data: bytes, dest_dir: Path, max_mb: int = 1024) -> None:
    """Extract an uncompressed tar after validating its paths, types, and byte budget."""
    limit = _byte_limit(max_mb)
    if len(data) > limit:
        raise ValueError("Tar stream exceeds the byte limit.")
    _extract_tar(io.BytesIO(data), dest_dir, limit)


class SshTransport:
    def __init__(
        self, *, socket_dir: Path | None = None, control_persist: str = "10m",
        runner: Callable | None = None,
    ):
        self._sockets = ControlSocketManager(socket_dir)
        self._control_persist = control_persist
        self._runner = subprocess.run if runner is None else runner
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}

    def _slot(self, host: HostConfig) -> threading.Semaphore:
        with self._lock:
            if host.id not in self._semaphores:
                self._semaphores[host.id] = threading.Semaphore(host.max_ssh_concurrency)
            return self._semaphores[host.id]

    def _argv(self, host: HostConfig, *extra_args: str) -> list[str]:
        return self._sockets.build_ssh_argv(
            host, control_persist=self._control_persist, extra_args=extra_args,
        )

    def _close_host(self, host: HostConfig) -> None:
        try:
            self._runner(
                self._argv(host, "-O", "exit"), shell=False, capture_output=True,
                timeout=5.0, env=build_ssh_env(),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    def run_remote(
        self, host: HostConfig, command: str, *, stdin_data: bytes | None = None,
        timeout_s: float = 30.0, retry_mux: bool = True,
    ) -> SshResult:
        with self._slot(host):
            started = time.monotonic()
            argv = [*self._argv(host), command]
            options = dict(shell=False, capture_output=True, timeout=timeout_s, env=build_ssh_env())
            if stdin_data is None:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["input"] = stdin_data

            try:
                result = self._runner(argv, **options)
                if retry_mux and result.returncode == 255 and any(
                    error in _decode(result.stderr) for error in _MUX_ERRORS
                ):
                    self._close_host(host)
                    result = self._runner(argv, **options)
            except subprocess.TimeoutExpired as exc:
                return _timeout_result(started, exc)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return _result(started, 1, error=str(exc))
            return _result(started, result.returncode, result.stdout, result.stderr)

    def close_host(self, host: HostConfig) -> None:
        with self._slot(host):
            self._close_host(host)

    def close_all(self, hosts: Iterable[HostConfig]) -> None:
        for host in hosts:
            self.close_host(host)

    def build_remote_wrapper(
        self, *, host: HostConfig, task: TaskSpec, run_id: str, out_dir: str,
        attempt_dir: str, gpu_ids: Sequence[int], rendered_cmd: str,
    ) -> tuple[str, str]:
        env_sh = _exports({**host.env, **task.env})
        gpu_list = ",".join(str(gpu) for gpu in gpu_ids)
        dispatch_env = {
            "DISPATCH_RUN_ID": run_id, "DISPATCH_TASK_ID": task.id,
            "DISPATCH_OUT_DIR": out_dir, "DISPATCH_ATTEMPT_DIR": attempt_dir,
            "DISPATCH_HOST_ID": host.id,
        }
        if host.accelerator == "nvidia" or (host.accelerator == "ppu" and gpu_ids):
            dispatch_env["CUDA_VISIBLE_DEVICES"] = gpu_list
        if host.accelerator == "ppu" and gpu_ids:
            dispatch_env["PPU_VISIBLE_DEVICES"] = gpu_list

        env_file = shlex.quote(attempt_dir + "/env.sh")
        run_sh = (
            "#!/bin/sh\nset -u\n"
            f"if [ -f {env_file} ]; then\n    . {env_file} || exit \"$?\"\nfi\n"
            f"OUT_DIR={shlex.quote(out_dir)}\n"
            f"ATTEMPT_DIR={shlex.quote(attempt_dir)}\n"
            f"gpu_ids_json={shlex.quote(json.dumps(list(gpu_ids)))}\n"
            'started_at=$(command -p date +%s) || exit 1\n'
            + _exports(dispatch_env)
            + _DONE_FUNCTION
        )
        if task.done_markers:
            checks = " && ".join(f'[ -f "$OUT_DIR"/{shlex.quote(marker)} ]' for marker in task.done_markers)
            run_sh += f'if {checks}; then\n    write_done 0 true\n    exit "$?"\nfi\n'

        run_sh += """trap 'rc=$?; trap - EXIT; write_done "$rc" false; exit "$rc"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
(
"""
        if host.setup_cmd:
            run_sh += f"set -e\n{host.setup_cmd}\nset +e\n"
        run_sh += f':\n{rendered_cmd}\n) > "$ATTEMPT_DIR/task.log" 2>&1\n'
        return env_sh, run_sh

    def launch_detached(
        self, host: HostConfig, *, task: TaskSpec, run_id: str, out_dir: str,
        attempt_dir: str, gpu_ids: Sequence[int], rendered_cmd: str, timeout_s: float = 30.0,
    ) -> tuple[bool, int | None, str | None]:
        try:
            env_sh, run_sh = self.build_remote_wrapper(
                host=host, task=task, run_id=run_id, out_dir=out_dir,
                attempt_dir=attempt_dir, gpu_ids=gpu_ids, rendered_cmd=rendered_cmd,
            )
        except ValueError as exc:
            return False, None, redact_secrets(str(exc))
        payload = b"".join(base64.b64encode(script.encode("utf-8")) + b"\n" for script in (env_sh, run_sh))
        command = (
            "if command -v python3 >/dev/null 2>&1; then\n"
            f"    {_python_command(_LAUNCH, out_dir, attempt_dir)}\n"
            "else\n"
            f"out_dir={shlex.quote(out_dir)}\nattempt_dir={shlex.quote(attempt_dir)}\n"
            f"{_LAUNCH_FALLBACK}\nfi"
        )
        # Reissuing a launch after a connection failure can spawn a duplicate task.
        result = self.run_remote(host, command, stdin_data=payload, timeout_s=timeout_s, retry_mux=False)
        if not result.ok:
            return False, None, redact_secrets(result.error or result.stderr or "SSH launch failed.")
        try:
            pgid = int(result.stdout.strip())
            if pgid > 1:
                return True, pgid, None
        except ValueError:
            pass
        return False, None, "The remote launcher did not return a valid process group ID."

    def kill_pgid(
        self, host: HostConfig, pgid: int, *, grace_s: float = 5.0, timeout_s: float = 20.0,
    ) -> SshResult:
        if not isinstance(pgid, int) or pgid <= 1:
            raise ValueError("The process group ID must be an integer greater than one.")
        if not math.isfinite(grace_s) or grace_s < 0:
            raise ValueError("The termination grace period must be finite and nonnegative.")
        return self.run_remote(
            host, _python_command(_KILL_GROUP, str(pgid), str(grace_s)), timeout_s=timeout_s,
            retry_mux=False,
        )

    def run_preflight(self, host: HostConfig, rendered_cmd: str, *, timeout_s: float = 120.0) -> SshResult:
        started = time.monotonic()
        try:
            command = _exports(host.env)
        except ValueError as exc:
            return _result(started, 1, error=str(exc))
        if host.setup_cmd:
            command += f"set -e\n{host.setup_cmd}\nset +e\n"
        return self.run_remote(
            host, "/bin/sh -s", stdin_data=(command + rendered_cmd + "\n").encode("utf-8"),
            timeout_s=timeout_s,
        )

    def sync_directory_tar(
        self, host: HostConfig, *, local_dir: Path, remote_dir: str,
        excludes: Sequence[str] = (), timeout_s: float = 300.0,
    ) -> SshResult:
        target = shlex.quote(remote_dir)
        remote_command = f"mkdir -p -- {target} && tar -xf - -C {target}"
        env = build_ssh_env()
        tar_env = {**env, "COPYFILE_DISABLE": "1"}
        with self._slot(host), ExitStack() as captures:
            tar_errors = captures.enter_context(tempfile.TemporaryFile())
            ssh_output = captures.enter_context(tempfile.TemporaryFile())
            ssh_errors = captures.enter_context(tempfile.TemporaryFile())
            started = time.monotonic()
            try:
                tar_options = []
                support = subprocess.run(
                    ["tar", "--no-xattrs", "-cf", os.devnull, "-T", os.devnull],
                    shell=False, capture_output=True, timeout=timeout_s, env=tar_env,
                )
                if support.returncode == 0:
                    tar_options.append("--no-xattrs")
                tar_argv = [
                    "tar", *tar_options, *(f"--exclude={pattern}" for pattern in excludes),
                    "-cf", "-", "-C", os.fspath(local_dir), ".",
                ]
                with subprocess.Popen(
                    tar_argv, shell=False, stdout=subprocess.PIPE, stderr=tar_errors, env=tar_env,
                ) as tar:
                    try:
                        with subprocess.Popen(
                            [*self._argv(host), remote_command], shell=False, stdin=tar.stdout,
                            stdout=ssh_output, stderr=ssh_errors, env=env,
                        ) as ssh:
                            tar.stdout.close()
                            try:
                                ssh.wait(timeout=self._remaining(started, timeout_s))
                            finally:
                                if ssh.poll() is None:
                                    ssh.kill()
                        tar.wait(timeout=self._remaining(started, timeout_s))
                    finally:
                        if tar.poll() is None:
                            tar.kill()
                stdout, ssh_stderr, tar_stderr = map(_read_capture, (ssh_output, ssh_errors, tar_errors))
                return _result(started, ssh.returncode or tar.returncode, stdout, ssh_stderr + tar_stderr)
            except subprocess.TimeoutExpired:
                return _timeout_result(started, subprocess.TimeoutExpired(
                    "tar over ssh", timeout_s, _read_capture(ssh_output),
                    _read_capture(ssh_errors) + _read_capture(tar_errors),
                ))
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return _result(started, 1, error=str(exc))

    @staticmethod
    def _remaining(started: float, timeout_s: float) -> float:
        return max(0.0, timeout_s - (time.monotonic() - started))

    def mirror_remote_outputs(
        self, host: HostConfig, *, out_dir: str, mirror_dir: str,
        excludes: Sequence[str] = (), timeout_s: float = 120.0,
    ) -> SshResult:
        command = _python_command(_REMOTE_COPY, "mirror", out_dir, mirror_dir, json.dumps(list(excludes)))
        return self.run_remote(host, command, timeout_s=timeout_s, retry_mux=False)

    def pull_outputs_tar(
        self, host: HostConfig, *, remote_out_dir: str, local_dest_dir: Path,
        excludes: Sequence[str] = (), max_mb: int = 1024, timeout_s: float = 180.0,
    ) -> SshResult:
        limit = _byte_limit(max_mb)
        command = _python_command(_REMOTE_COPY, "pull", remote_out_dir, str(limit), json.dumps(list(excludes)))
        started = time.monotonic()
        with tempfile.TemporaryFile() as archive, tempfile.TemporaryFile() as errors:
            try:
                with self._slot(host), selectors.DefaultSelector() as ready, subprocess.Popen(
                    [*self._argv(host), command], shell=False, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=errors, env=build_ssh_env(),
                ) as ssh:
                    try:
                        ready.register(ssh.stdout, selectors.EVENT_READ)
                        remaining = limit
                        while True:
                            wait_s = self._remaining(started, timeout_s)
                            if not wait_s or not ready.select(wait_s):
                                raise subprocess.TimeoutExpired("ssh", timeout_s)
                            data = os.read(ssh.stdout.fileno(), min(_CHUNK_SIZE, remaining + 1))
                            if not data:
                                break
                            if len(data) > remaining:
                                raise ValueError("Tar stream exceeds the byte limit.")
                            archive.write(data)
                            remaining -= len(data)
                        code = ssh.wait(timeout=self._remaining(started, timeout_s))
                    finally:
                        if ssh.poll() is None:
                            ssh.kill()
                stderr = _read_capture(errors)
                if code == 0:
                    archive.seek(0)
                    _extract_tar(archive, local_dest_dir, limit)
                return _result(started, code, stderr=stderr)
            except subprocess.TimeoutExpired:
                return _timeout_result(
                    started, subprocess.TimeoutExpired("ssh", timeout_s, stderr=_read_capture(errors)),
                )
            except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError, RuntimeError) as exc:
                return _result(started, 1, error=str(exc))
