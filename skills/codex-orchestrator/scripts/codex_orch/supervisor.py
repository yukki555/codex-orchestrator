from __future__ import annotations

import argparse
import json
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .env import build_child_env


_QUOTA_MARKERS = (
    "usage_limit_reached", "rate_limit_exceeded", "insufficient_quota",
    "quota exceeded", "too many requests", "status 429", "http 429",
)
_QUOTA_OVERLAP = max(map(len, _QUOTA_MARKERS)) - 1


def classify_quota_error(events_text: str, stderr_text: str) -> bool:
    return any(
        marker in stream.lower()
        for stream in (events_text, stderr_text)
        for marker in _QUOTA_MARKERS
    )


def _terminate_group(pgid, grace_s, initial_signal, reap=None):
    try:
        os.killpg(pgid, initial_signal)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace_s
    kill_deadline = deadline + 0.25
    while True:
        if reap is not None:
            reap()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # A group with only exiting processes can be visible but unsignalable.
            pass
        now = time.monotonic()
        if now >= deadline:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except PermissionError:
                if reap is not None and reap() is None:
                    raise
            # A zombie grandchild can keep its group visible until its own parent reaps it.
            if now >= kill_deadline:
                return
        time.sleep(0.02)


def terminate_process_group(pgid, grace_s=2.0):
    _terminate_group(pgid, grace_s, signal.SIGTERM)


def _drain_pipe(pipe, output, on_chunk, stop_reading, quota_found, errors):
    try:
        fd = pipe.fileno()
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        quota_tail = ""
        while True:
            if not poller.poll(100):
                if stop_reading.is_set():
                    return
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            if on_chunk is not None:
                on_chunk()
            output.write(chunk)
            output.flush()
            if not quota_found.is_set():
                # Quota markers are ASCII. Retain an overlap for markers split across reads.
                quota_text = quota_tail + chunk.decode("latin-1").lower()
                if any(marker in quota_text for marker in _QUOTA_MARKERS):
                    quota_found.set()
                quota_tail = quota_text[-_QUOTA_OVERLAP:]
    except Exception as exc:
        errors.put(exc)


def run_supervised(
    cmd: list[str], *, prompt_path: str | None, events_path: str | Path,
    stderr_path: str | Path, timeout_s: int = 0, stall_timeout_s: int = 0,
    inherit_env: bool = False,
) -> dict:
    received_signal = None
    timed_out = stall_timed_out = False
    activity_lock = threading.Lock()
    stop_reading = threading.Event()
    quota_found = threading.Event()
    errors = queue.SimpleQueue()

    def receive_signal(signum, _frame):
        nonlocal received_signal
        received_signal = signum

    def mark_activity():
        nonlocal last_activity_mono
        with activity_lock:
            last_activity_mono = time.monotonic()

    with ExitStack() as stack:
        prompt = stack.enter_context(open(prompt_path, "rb")) if prompt_path else subprocess.DEVNULL
        events = stack.enter_context(open(events_path, "wb"))
        stderr = stack.enter_context(open(stderr_path, "wb"))
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handler = signal.signal(signum, receive_signal)
                stack.callback(signal.signal, signum, previous_handler)

        start_mono = last_activity_mono = time.monotonic()
        proc = stack.enter_context(subprocess.Popen(
            cmd, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=build_child_env(inherit_all=inherit_env), start_new_session=True,
        ))
        readers = []
        try:
            for pipe, output, on_chunk in (
                (proc.stdout, events, mark_activity), (proc.stderr, stderr, None),
            ):
                reader = threading.Thread(
                    target=_drain_pipe,
                    args=(pipe, output, on_chunk, stop_reading, quota_found, errors),
                    daemon=True,
                )
                reader.start()
                readers.append(reader)

            while True:
                if received_signal is not None or not errors.empty():
                    break
                if proc.poll() is not None and not any(reader.is_alive() for reader in readers):
                    break
                with activity_lock:
                    now = time.monotonic()
                    idle_s = now - last_activity_mono
                if timeout_s > 0 and now - start_mono >= timeout_s:
                    timed_out = True
                    break
                if stall_timeout_s > 0 and idle_s >= stall_timeout_s:
                    timed_out = stall_timed_out = True
                    break
                time.sleep(0.02)
        finally:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                _terminate_group(proc.pid, 2.0, received_signal or signal.SIGTERM, proc.poll)
            stop_reading.set()
            for reader in readers:
                reader.join()
            codex_exit_code = proc.wait()
        if not errors.empty():
            raise errors.get()

    return {
        "codex_exit_code": codex_exit_code,
        "duration_s": round(time.monotonic() - start_mono, 3),
        "timed_out": timed_out,
        "stall_timed_out": stall_timed_out,
        "cancelled": received_signal is not None,
        "signal": int(received_signal) if received_signal is not None else None,
        "paused_quota": quota_found.is_set(),
        "timeout_s": timeout_s,
        "stall_timeout_s": stall_timeout_s,
    }


def _write_metadata(path: Path, metadata: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", delete=False,
    ) as temp:
        temp_path = Path(temp.name)
        try:
            json.dump(metadata, temp)
            temp.write("\n")
            temp.close()
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--stall-timeout", type=int, default=0)
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--events", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--meta-out", type=Path, required=True)
    parser.add_argument("--inherit-env", action="store_true")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("a child command is required")
    metadata = run_supervised(
        cmd, prompt_path=args.prompt_file or None, events_path=args.events,
        stderr_path=args.stderr, timeout_s=args.timeout,
        stall_timeout_s=args.stall_timeout, inherit_env=args.inherit_env,
    )
    _write_metadata(args.meta_out, metadata)
    if metadata["cancelled"]:
        return 128 + metadata["signal"]
    if metadata["stall_timed_out"]:
        return 125
    if metadata["timed_out"]:
        return 124
    code = metadata["codex_exit_code"]
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(_main())
