"""Collect accelerator, attempt, and filesystem status in one SSH request."""

from __future__ import annotations

import base64
import json
import math
import shlex
from collections.abc import Sequence
from typing import Any

from .models import AttemptProbeStatus, GpuSlotSnapshot, HostConfig, HostProbeSnapshot
from .ssh_transport import SshTransport


PROBE_ENV_ALLOWLIST = (
    "PATH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "CUDA_HOME", "CUDA_PATH", "CUDA_SDK",
    "PPU_HOME", "PPU_PATH", "PPU_SDK", "CUDA_VISIBLE_DEVICES", "PPU_VISIBLE_DEVICES",
)


REMOTE_PROBE_SCRIPT = r"""
import base64
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

GPU_QUERY = [
    "--query-gpu=index,uuid,memory.total,memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
]
COMPUTE_QUERY = [
    "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
    "--format=csv,noheader,nounits",
]

def capture(argv):
    try:
        process = subprocess.run(
            argv, shell=False, capture_output=True, timeout=8, check=True,
            encoding="utf-8", errors="replace",
        )
        return process.stdout
    except (OSError, subprocess.SubprocessError):
        return None

def integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return None

def count(value):
    return max(0, integer(value) or 0)

def parse_processes():
    processes = {}
    for line in (capture(["ps", "-eo", "pid=,pgid="]) or "").splitlines():
        columns = line.split()
        if len(columns) == 2:
            pid, pgid = map(integer, columns)
            if pid and pgid:
                processes[pid] = pgid
    return processes

def gpu_slot(index, total=0, used=0, utilization=0, pids=(), busy=False):
    return {
        "index": index,
        "memory_total_mb": count(total),
        "memory_used_mb": count(used),
        "utilization_pct": count(utilization),
        "external_pids": sorted({pid for pid in map(integer, pids) if pid and pid > 0}),
        "external_busy": busy is True,
    }

def csv_slots(text):
    slots = {}
    for row in csv.reader(text.splitlines(), skipinitialspace=True):
        if len(row) != 5:
            continue
        index = integer(row[0])
        if index is not None and index >= 0:
            slots[row[1].strip()] = gpu_slot(index, row[2], row[3], row[4])
    return slots

def count_managed_gpu_holders(processes, lease_pgids, gpu_index, proc_root="/proc"):
    if not lease_pgids or not os.path.isdir(proc_root):
        return 0
    holders = 0
    devices = (f"/dev/nvidia{gpu_index}", f"/dev/alixpu_ppu{gpu_index}")
    for pid, pgid in processes.items():
        if pgid not in lease_pgids and pid not in lease_pgids:
            continue
        directory = f"{proc_root}/{pid}/fd"
        try:
            descriptors = os.listdir(directory)
        except OSError:
            continue
        for fd in descriptors:
            try:
                if os.readlink(f"{directory}/{fd}") in devices:
                    holders += 1
                    break
            except OSError:
                continue
    return holders

def nvidia_slots(processes, managed, leased_gpu_pgids):
    text = capture(["nvidia-smi", *GPU_QUERY])
    if not text:
        return [], "nvidia-smi returned no GPU inventory."
    slots = csv_slots(text)
    compute = capture(["nvidia-smi", *COMPUTE_QUERY])
    unmapped_by_uuid = defaultdict(list)
    for row in csv.reader((compute or "").splitlines(), skipinitialspace=True):
        if len(row) != 3 or row[0].strip() not in slots:
            continue
        uuid = row[0].strip()
        pid = integer(row[1])
        if pid in processes:
            if processes[pid] not in managed and pid not in managed:
                slots[uuid]["external_pids"].append(pid)
        else:
            unmapped_by_uuid[uuid].append((pid, count(row[2])))
    for uuid, slot in slots.items():
        unmapped_rows = unmapped_by_uuid[uuid]
        gpu_index = slot["index"]
        if gpu_index in leased_gpu_pgids:
            active_unmapped = [(pid, used) for pid, used in unmapped_rows if pid or used > 0]
            if active_unmapped:
                holders = count_managed_gpu_holders(processes, leased_gpu_pgids[gpu_index], gpu_index)
                unmapped_rows = active_unmapped[holders:]
                slot["external_busy"] = bool(unmapped_rows)
        else:
            slot["external_busy"] = any(used > 0 for _, used in unmapped_rows)
        slot["external_pids"].extend(pid for pid, _ in unmapped_rows if pid and pid > 0)
        slot["external_pids"] = sorted(set(slot["external_pids"]))
    warning = None if compute is not None else "nvidia-smi compute-app inventory is unavailable."
    return list(slots.values()), warning

def ppu_slots(request, processes, managed, leased_gpu_pgids):
    custom = request["ppu_probe_cmd"]
    if not custom:
        slots, warning = nvidia_slots(processes, managed, leased_gpu_pgids)
        if slots:
            return slots, warning
    output = capture(["/bin/sh", "-c", custom] if custom else ["ppu-smi", *GPU_QUERY])
    slots = []
    if output:
        try:
            rows = json.loads(output)
        except (ValueError, RecursionError):
            rows = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                index = integer(row.get("index"))
                if index is None or index < 0:
                    continue
                pids = row.get("external_pids")
                slots.append(gpu_slot(
                    index, row.get("memory_total_mb"), row.get("memory_used_mb"),
                    row.get("utilization_pct"), pids if isinstance(pids, list) else (),
                    row.get("external_busy"),
                ))
        else:
            slots = list(csv_slots(output).values())
    if slots:
        return slots, None
    return (
        [gpu_slot(index) for index in (request["gpu_ids"] or [])],
        "PPU probe returned no GPU inventory. Configured GPU indices were used.",
    )

def kill0_pgid(pgid):
    if type(pgid) is not int or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True

def marker_present(root, marker):
    relative = PurePosixPath(marker)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return False
    try:
        return (root / relative).is_file()
    except (OSError, ValueError):
        return False

def probe_attempt(attempt, live_pgids):
    pgid = attempt["pgid"]
    directory = Path(attempt["attempt_dir"])
    out_dir = Path(attempt["out_dir"])
    try:
        done = json.loads((directory / "done.json").read_text(encoding="utf-8"))
        json.dumps(done, allow_nan=False)
    except (OSError, ValueError, RecursionError):
        done = None
    log_tail = ""
    log_size = log_mtime = None
    log_path = directory / "task.log"
    try:
        stats = log_path.stat()
        log_size, log_mtime = stats.st_size, round(stats.st_mtime, 3)
        with log_path.open("rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 4096))
            log_tail = log.read(4096).decode("utf-8", errors="replace")
    except OSError:
        pass
    return {
        "attempt": attempt["attempt"],
        "pgid_alive": bool(pgid and (pgid in live_pgids or kill0_pgid(pgid))),
        "done_payload": done if isinstance(done, dict) else None,
        "done_markers_present": all(marker_present(out_dir, marker) for marker in attempt["done_markers"]),
        "log_tail": log_tail,
        "log_size": log_size,
        "log_mtime": log_mtime,
    }

def disk_free(work_root):
    directory = Path(work_root)
    for candidate in (directory, *directory.parents):
        try:
            if candidate.is_dir():
                stats = os.statvfs(candidate)
                return round(stats.f_bavail * stats.f_frsize / (1024 ** 3), 3)
        except OSError:
            continue
    return None

def probe(request):
    processes = parse_processes()
    live_pgids = set(processes.values())
    gpus, warning = [], None
    accelerator = request["accelerator"]
    if accelerator in ("nvidia", "ppu"):
        managed = set(request["managed_pgids"])
        leased_gpu_pgids = defaultdict(set)
        for attempt in request["attempts"]:
            pgid = attempt["pgid"]
            if type(pgid) is int and pgid > 1 and (pgid in live_pgids or kill0_pgid(pgid)):
                for gpu_index in attempt.get("gpu_ids", []):
                    leased_gpu_pgids[gpu_index].add(pgid)
        if accelerator == "nvidia":
            gpus, warning = nvidia_slots(processes, managed, leased_gpu_pgids)
        else:
            gpus, warning = ppu_slots(request, processes, managed, leased_gpu_pgids)
    allowed_indices = request["gpu_ids"]
    result = {
        "gpus": sorted(
            (gpu for gpu in gpus if allowed_indices is None or gpu["index"] in allowed_indices),
            key=lambda gpu: gpu["index"],
        ),
        "attempts": {
            attempt["task_id"]: probe_attempt(attempt, live_pgids)
            for attempt in request["attempts"]
        },
        "disk_free_gb": disk_free(request["work_root"]),
        "existing_done_tasks": sorted({
            entry["task_id"] for entry in request.get("marker_checks") or []
            if entry["done_markers"] and all(
                marker_present(Path(entry["out_dir"]), marker) for marker in entry["done_markers"]
            )
        }),
    }
    if warning:
        result["probe_warning"] = warning
    return result

def main(request):
    env = request.get("env") or {}
    previous = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        result = probe(request)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))

if __name__ == "__main__":
    raw = base64.b64decode(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin.buffer.read()
    main(json.loads(raw))
"""


def build_probe_request(
    host: HostConfig, active_attempts: Sequence[dict[str, Any]], *,
    marker_checks: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    attempt_keys = ("task_id", "attempt", "pgid", "attempt_dir", "out_dir", "done_markers")
    attempts = [
        {**{key: attempt[key] for key in attempt_keys}, "gpu_ids": list(attempt.get("gpu_ids", []))}
        for attempt in active_attempts
    ]
    return {
        "accelerator": host.accelerator,
        "gpu_ids": list(host.gpu_ids) if host.gpu_ids is not None else None,
        "work_root": host.work_root,
        "ppu_probe_cmd": host.ppu_probe_cmd,
        "env": {k: v for k, v in host.env.items() if k in PROBE_ENV_ALLOWLIST},
        "managed_pgids": sorted({
            attempt["pgid"] for attempt in attempts
            if type(attempt["pgid"]) is int and attempt["pgid"] > 1
        }),
        "attempts": attempts,
        "marker_checks": list(marker_checks),
    }


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"Nonfinite number in probe response: {token}")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        _reject_nonfinite(token)
    return value


def parse_probe_response(host: HostConfig, raw_stdout: str) -> HostProbeSnapshot:
    failed = HostProbeSnapshot(host_id=host.id, ok=False)
    try:
        payload = json.loads(raw_stdout, parse_constant=_reject_nonfinite, parse_float=_finite_float)
        if not isinstance(payload, dict):
            return failed
        raw_gpus = payload.get("gpus", [])
        raw_attempts = payload.get("attempts", {})
        existing_done_tasks = payload.get("existing_done_tasks", [])
        disk_free_gb = payload.get("disk_free_gb")
        if not isinstance(raw_gpus, list) or not isinstance(raw_attempts, dict):
            return failed
        if not isinstance(existing_done_tasks, list) or any(
            not isinstance(task_id, str) for task_id in existing_done_tasks
        ):
            return failed
        if disk_free_gb is not None and (type(disk_free_gb) not in (int, float) or disk_free_gb < 0):
            return failed

        gpus = [GpuSlotSnapshot.from_dict(gpu) for gpu in raw_gpus]
        for gpu in gpus:
            if (
                any(type(value) is not int or value < 0 for value in (
                    gpu.index, gpu.memory_total_mb, gpu.memory_used_mb, gpu.utilization_pct,
                ))
                or not isinstance(gpu.external_pids, list)
                or any(type(pid) is not int or pid <= 0 for pid in gpu.external_pids)
                or type(gpu.external_busy) is not bool
            ):
                return failed
        attempts = {
            task_id: AttemptProbeStatus.from_dict(status) for task_id, status in raw_attempts.items()
        }
        for status in attempts.values():
            if (
                type(status.pgid_alive) is not bool
                or type(status.done_markers_present) is not bool
                or not isinstance(status.log_tail, str)
                or (status.done_payload is not None and not isinstance(status.done_payload, dict))
                or (status.attempt is not None and type(status.attempt) is not int)
                or (status.log_size is not None and (type(status.log_size) is not int or status.log_size < 0))
                or (status.log_mtime is not None and (
                    type(status.log_mtime) not in (int, float) or status.log_mtime < 0
                ))
            ):
                return failed
        return HostProbeSnapshot(
            host_id=host.id, ok=True, disk_free_gb=disk_free_gb, gpus=gpus, attempts=attempts,
            existing_done_tasks=existing_done_tasks,
        )
    except (TypeError, ValueError, RecursionError):
        return failed


def probe_host(
    transport: SshTransport, host: HostConfig, active_attempts: Sequence[dict[str, Any]], *,
    timeout_s: float = 25.0, marker_checks: Sequence[dict[str, Any]] = (),
) -> HostProbeSnapshot:
    request = json.dumps(
        build_probe_request(host, active_attempts, marker_checks=marker_checks), separators=(",", ":"),
    )
    encoded = base64.b64encode(request.encode("utf-8")).decode("ascii")
    command = shlex.join(("python3", "-c", REMOTE_PROBE_SCRIPT, encoded))
    result = transport.run_remote(host, command, timeout_s=timeout_s)
    if not result.ok:
        return HostProbeSnapshot(host_id=host.id, ok=False)
    return parse_probe_response(host, result.stdout)
