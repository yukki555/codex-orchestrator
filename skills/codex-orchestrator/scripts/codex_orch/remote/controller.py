"""Persist and operate a multi-host remote dispatch run."""

from __future__ import annotations

import argparse
import fcntl
import heapq
import inspect
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Sequence

from ..env import ensure_private_dir, plugin_data_dir, redact_secrets
from .gpu_probe import probe_host
from .models import (
    TERMINAL_TASK_STATES, ClusterConfig, DispatchManifest, HostConfig, HostProbeSnapshot,
    RunState, TaskRuntime, TaskSpec, ValidationError, host_matches_task_static,
    load_and_merge_additional_tasks, load_cluster_config, load_dispatch_manifest, render_task_command,
    validate_manifest_against_cluster,
)
from .scheduler import (
    PreemptAction, PullAction, SchedulerEvent, _downstream_counts, _finish_failure, advance_dag_states,
    build_summary, compute_free_gpus, init_run_state, is_run_terminal,
    mark_dispatch_launch_failed, mark_dispatch_started, plan_dispatches, reconcile_host_probe,
    mark_relay_failed, mark_task_relaying, needs_cross_host_relay,
)
from .ssh_transport import RelayError, SshTransport


_WATCHDOG_REASONS = ("timeout", "stall_timeout")


def resolve_cluster_path(explicit: str | None) -> Path:
    configured = explicit if explicit is not None else os.environ.get("CODEX_ORCH_CLUSTER_CONFIG")
    try:
        path = (Path(configured).expanduser() if configured is not None else
                Path.home() / ".config" / "codex-orchestrator" / "clusters.json")
        if path.is_file():
            return path
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"Cannot resolve the cluster configuration: {exc}") from exc
    raise ValidationError(f"Cluster configuration is not a file: {path}")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {redact_secrets(key): _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_text(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, data: Any) -> None:
    _atomic_text(path, json.dumps(data, sort_keys=True, allow_nan=False) + "\n")


@contextmanager
def _locked(run_dir: Path, name: str = "state.lock"):
    fd = os.open(run_dir / name, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _append_events(run_dir: Path, events: Sequence[SchedulerEvent]) -> None:
    fd = os.open(run_dir / "events.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(_event_records(events))
        handle.flush()
        os.fsync(handle.fileno())


def _event_records(events: Sequence[SchedulerEvent]) -> str:
    return "".join(json.dumps(_redact(event.to_dict()), allow_nan=False) + "\n" for event in events)


def _update_dispatch_registry(run_dir: Path, summary: dict[str, Any]) -> None:
    if os.environ.get("CODEX_ORCH_DISABLE_DISPATCH_REGISTRY") == "1":
        return
    try:
        directory = ensure_private_dir(plugin_data_dir())
        path = directory / "dispatch_runs.json"
        with _locked(directory, "dispatch_runs.lock"):
            try:
                data = _read_json(path)
            except (FileNotFoundError, ValueError):
                data = {}
            entries = data.get("runs", {}) if isinstance(data, dict) else {}
            now = time.time()
            runs = {}
            if isinstance(entries, dict):
                for key, entry in entries.items():
                    if not isinstance(entry, dict):
                        continue
                    directory_name = entry.get("run_dir")
                    updated_at = entry.get("updated_at")
                    if (not isinstance(directory_name, str) or not Path(directory_name).is_dir()
                            or type(updated_at) not in (int, float) or not math.isfinite(updated_at)
                            or (entry.get("terminal") is True and updated_at < now - 7 * 86400)):
                        continue
                    runs[key] = entry
            resolved = str(run_dir.resolve())
            runs[resolved] = {key: summary[key] for key in (
                "run_id", "controller_pid", "terminal", "task_counts",
            )}
            runs[resolved].update(run_dir=resolved, updated_at=now)
            recent = sorted(runs, key=lambda key: runs[key]["updated_at"], reverse=True)[:32]
            _write_json(path, {"version": "1", "runs": {key: runs[key] for key in recent}})
    except (OSError, RuntimeError, ValueError, OverflowError):
        pass


def _pid(run_dir: Path) -> int | None:
    try:
        pid = int((run_dir / "controller.pid").read_text(encoding="ascii"))
        return pid if pid > 1 else None
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _existing_run(run_dir: Path) -> Path:
    run_dir = Path(run_dir).expanduser().absolute()
    if run_dir.is_symlink() or not (run_dir / "state.json").is_file():
        raise ValidationError(f"No initialized run at {run_dir}")
    return run_dir


def _load_run(run_dir: Path) -> tuple[ClusterConfig, DispatchManifest, RunState]:
    return (
        ClusterConfig.from_dict(_read_json(run_dir / "cluster.snapshot.json")),
        DispatchManifest.from_dict(_read_json(run_dir / "manifest.snapshot.json")),
        _load_state(run_dir),
    )


def _load_state(run_dir: Path) -> RunState:
    return RunState.from_dict(_read_json(run_dir / "state.json"))


def _task_order(manifest: DispatchManifest) -> list[str]:
    tasks = {task.id: task for task in manifest.tasks}
    downstream = _downstream_counts(tasks)
    waiting = {task.id: len(set(task.depends_on)) for task in manifest.tasks}
    successors: dict[str, list[str]] = {task_id: [] for task_id in tasks}
    ready = []

    def offer(task: TaskSpec) -> None:
        heapq.heappush(ready, (-task.priority, -downstream[task.id], -task.gpus,
                              -task.est_duration_s, task.manifest_index, task.id))

    for task in manifest.tasks:
        for dep in dict.fromkeys(task.depends_on):
            successors[dep].append(task.id)
        if not waiting[task.id]:
            offer(task)
    order = []
    while ready:
        *_, task_id = heapq.heappop(ready)
        order.append(task_id)
        for successor in successors[task_id]:
            waiting[successor] -= 1
            if waiting[successor] == 0:
                offer(tasks[successor])
    return order


def _attempts_on(state: RunState, host_id: str):
    for task_id, runtime in state.tasks.items():
        attempt = runtime.active_attempt
        if attempt is not None and attempt.host_id == host_id:
            yield task_id, runtime, attempt


def _preflight_command(manifest: DispatchManifest, host: HostConfig, run_id: str) -> str:
    return render_task_command(
        TaskSpec("preflight", manifest.preflight.cmd, gpus=0), host, run_id=run_id,
        out_dir=host.work_root, attempt_dir=host.work_root, gpu_ids=[], dep_out_dirs={},
    )


def _configuration(cluster_path: Path, manifest_path: Path):
    cluster = load_cluster_config(Path(cluster_path).expanduser())
    manifest = load_dispatch_manifest(Path(manifest_path).expanduser())
    warnings = validate_manifest_against_cluster(manifest, cluster)
    if manifest.preflight is not None:
        if manifest.preflight.timeout_s <= 0:
            raise ValidationError("The preflight timeout must be positive.")
        for host in cluster.hosts.values():
            _preflight_command(manifest, host, manifest.run_id or "validation")
    return cluster, manifest, warnings


def _validation_receipt(cluster, manifest, warnings):
    return {"ok": True, "run_id": manifest.run_id, "hosts": len(cluster.hosts),
            "tasks": len(manifest.tasks), "warnings": warnings}


def _poll_interval(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValidationError("The poll interval must be finite and positive.")


def _error(result) -> str:
    return redact_secrets((result.error or result.stderr or f"Exit status {result.exit_code}")[:2048])


class RemoteController:
    def __init__(
        self, *, transport: SshTransport | None = None, clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None, probe_fn: Callable | None = None,
    ):
        self._transport = transport
        self.clock = time.time if clock is None else clock
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.probe_fn = probe_host if probe_fn is None else probe_fn
        self._interrupted = False

    @property
    def transport(self) -> SshTransport:
        if self._transport is None:
            self._transport = SshTransport()
        return self._transport

    def validate(self, cluster_path: Path, manifest_path: Path) -> dict[str, Any]:
        return _validation_receipt(*_configuration(cluster_path, manifest_path))

    def plan(self, cluster_path: Path, manifest_path: Path, *, live_probe: bool = False) -> dict[str, Any]:
        cluster, manifest, warnings = _configuration(cluster_path, manifest_path)
        result = _validation_receipt(cluster, manifest, warnings)
        result["eligible_hosts"] = {
            task.id: sorted(host.id for host in cluster.hosts.values()
                            if host_matches_task_static(host, task)[0])
            for task in manifest.tasks
        }
        result["execution_order"] = _task_order(manifest)
        if live_probe:
            snapshots = self._probe(cluster, {host_id: [] for host_id in cluster.hosts})
            result["host_gpu_summary"] = {snapshot.host_id: snapshot.to_dict() for snapshot in snapshots}
        return result

    def _probe(self, cluster, attempts, *, marker_checks=None) -> list[HostProbeSnapshot]:
        if not attempts:
            return []
        transport = self.transport
        accepts_checks = False
        if marker_checks is not None:
            try:
                inspect.signature(self.probe_fn).bind(transport, None, [], marker_checks=[])
            except (TypeError, ValueError):
                pass
            else:
                accepts_checks = True
        with ThreadPoolExecutor(max_workers=min(16, len(attempts))) as pool:
            futures = [
                pool.submit(self.probe_fn, transport, cluster.hosts[host_id], active,
                            **({"marker_checks": marker_checks[host_id]} if accepts_checks else {}))
                for host_id, active in sorted(attempts.items())
            ]
            return [future.result() for future in futures]

    def _summary(self, run_dir, cluster, state, *, probes=(), pulled=None) -> dict[str, Any]:
        summary = build_summary(state)
        try:
            previous = _read_json(run_dir / "summary.json")
        except FileNotFoundError:
            previous = {}
        pulled_tasks = previous.get("pulled_tasks", {})
        if pulled is not None:
            pulled_tasks.update(pulled)
        observed = previous.get("host_gpu_summary", {})
        for snapshot in probes:
            host = observed.setdefault(snapshot.host_id, {})
            host["ok"] = snapshot.ok
            if snapshot.ok:
                host["disk_free_gb"] = snapshot.disk_free_gb
        hosts = {}
        for host_id, runtime in state.hosts.items():
            leased = {gpu for _, _, attempt in _attempts_on(state, host_id) for gpu in attempt.gpu_ids}
            hosts[host_id] = {
                **observed.get(host_id, {}), "state": runtime.state, "preflight_ok": runtime.preflight_ok,
                "draining_for": runtime.draining_for,
                "gpus": [gpu.to_dict() for gpu in runtime.gpus], "leased_gpu_ids": sorted(leased),
                "free_gpu_ids": (
                    compute_free_gpus(cluster.hosts[host_id], runtime, leased, self.clock())
                    if runtime.state == "ready" and not runtime.consecutive_probe_failures else []
                ),
            }
        pid = _pid(run_dir)
        summary.update(updated_at=self.clock(), controller_pid=pid, controller_alive=_alive(pid),
                       pulled_tasks=pulled_tasks, host_gpu_summary=hosts, run_dir=str(run_dir))
        _write_json(run_dir / "summary.json", summary)
        _update_dispatch_registry(run_dir, summary)
        return summary

    def _persist(self, run_dir, cluster, state, events, *, probes=()) -> dict[str, Any]:
        _write_json(run_dir / "state.json", state.to_dict())
        _append_events(run_dir, events)
        return self._summary(run_dir, cluster, state, probes=probes)

    def _prepare_run(self, run_dir, cluster_path, manifest_path, sync):
        cluster, manifest, _ = _configuration(cluster_path, manifest_path)
        local_dir = None
        if sync and manifest.sync is not None:
            local_dir = Path(manifest.sync.local_dir).expanduser()
            if not local_dir.is_absolute():
                local_dir = Path(manifest_path).expanduser().absolute().parent / local_dir
            if not local_dir.is_dir():
                raise ValidationError(f"Synchronization source is not a directory: {local_dir}")
        directory = ensure_private_dir(Path(run_dir).expanduser().absolute())
        return directory, cluster, manifest, local_dir

    def _initialize(self, run_dir, cluster, manifest, cluster_path, manifest_path, local_dir, skip_preflight):
        with _locked(run_dir):
            if _alive(_pid(run_dir)):
                raise ValidationError("A controller is already running.")
            if (run_dir / "state.json").exists():
                raise ValidationError(f"Run already initialized at {run_dir}. Use --resume.")
        now = self.clock()
        state = init_run_state(cluster, manifest, cluster_path=str(cluster_path),
                               manifest_path=str(manifest_path), now=now)
        events = [SchedulerEvent(now, "run_initialized")]
        for host_id, host in sorted(cluster.hosts.items()):
            error = None
            if local_dir is not None:
                remote_dir = host.vars.get(manifest.sync.remote_var)
                if not remote_dir or not remote_dir.startswith("/") or ".." in Path(remote_dir).parts:
                    error = f"Invalid synchronization destination variable {manifest.sync.remote_var!r}"
                else:
                    result = self.transport.sync_directory_tar(
                        host, local_dir=local_dir, remote_dir=remote_dir, excludes=manifest.sync.excludes,
                    )
                    if not result.ok:
                        error = _error(result)
            if error is None and manifest.preflight is not None and not skip_preflight:
                result = self.transport.run_preflight(
                    host, _preflight_command(manifest, host, state.run_id),
                    timeout_s=manifest.preflight.timeout_s,
                )
                if not result.ok:
                    error = _error(result)
            state.hosts[host_id].preflight_ok = error is None
            if error is not None:
                state.hosts[host_id].state = "preflight_failed"
                events.append(SchedulerEvent(self.clock(), "host_preflight_failed", host_id=host_id,
                                             details={"error": error}))
        events.extend(advance_dag_states(state, {task.id: task for task in manifest.tasks}, self.clock()))
        with _locked(run_dir):
            (run_dir / "stop.flag").unlink(missing_ok=True)
            (run_dir / "controller.pid").unlink(missing_ok=True)
            _write_json(run_dir / "cluster.snapshot.json", cluster.to_dict())
            _write_json(run_dir / "manifest.snapshot.json", manifest.to_dict())
            return self._persist(run_dir, cluster, state, events)

    def initialize_run(
        self, run_dir: Path, cluster_path: Path, manifest_path: Path, *,
        sync: bool = False, skip_preflight: bool = False,
    ) -> dict[str, Any]:
        run_dir, cluster, manifest, local_dir = self._prepare_run(run_dir, cluster_path, manifest_path, sync)
        with _locked(run_dir, "dispatch.lock"):
            return self._initialize(run_dir, cluster, manifest, cluster_path, manifest_path, local_dir,
                                    skip_preflight)

    def _kill(self, cluster, actions):
        events, failed = [], set()
        for action in actions:
            if type(action.pgid) is not int or action.pgid <= 1:
                continue
            result = self.transport.kill_pgid(cluster.hosts[action.host_id], action.pgid)
            if not result.ok:
                failed.add(action.task_id)
            events.append(SchedulerEvent(
                self.clock(), "remote_killed" if result.ok else "remote_kill_failed",
                action.task_id, action.host_id, {"pgid": action.pgid, "reason": action.reason,
                                                **({} if result.ok else {"error": _error(result)})},
            ))
        return events, failed

    def _run_tick(self, run_dir, *, force_probe):
        with _locked(run_dir):
            cluster, manifest, state = _load_run(run_dir)
            tasks = {task.id: task for task in manifest.tasks}
            now = self.clock()
            # With dispatch.lock held, a persisted relay can only belong to an interrupted tick.
            recovered = []
            for task_id, runtime in state.tasks.items():
                if runtime.state == "relaying":
                    runtime.state = "ready"
                    recovered.append(SchedulerEvent(now, "relay_interrupted", task_id))
            if recovered:
                self._persist(run_dir, cluster, state, recovered)
            requests = {
                host_id: [
                    {"task_id": task_id, "attempt": attempt.attempt, "pgid": attempt.pgid,
                     "gpu_ids": list(attempt.gpu_ids),
                     "attempt_dir": attempt.attempt_dir, "out_dir": attempt.out_dir,
                     "done_markers": list(tasks[task_id].done_markers)}
                    for task_id, _, attempt in _attempts_on(state, host_id)
                ]
                for host_id, runtime in state.hosts.items()
                if runtime.preflight_ok and (force_probe or runtime.next_probe_at <= now)
            }
            marker_checks = {}
            for host_id in requests:
                host = cluster.hosts[host_id]
                marker_checks[host_id] = [
                    {"task_id": task.id, "done_markers": list(task.done_markers),
                     "out_dir": f"{host.work_root.rstrip('/')}/{state.run_id}/outputs/{task.id}"}
                    for task in manifest.tasks
                    if state.tasks[task.id].state == "ready" and task.done_markers
                    and not state.tasks[task.id].attempts and host_matches_task_static(host, task)[0]
                ]

        snapshots = self._probe(cluster, requests, marker_checks=marker_checks)
        pulls, preempts, events = [], [], []
        with _locked(run_dir):
            state = _load_state(run_dir)
            before = {task_id: deepcopy(runtime) for task_id, runtime in state.tasks.items()
                      if runtime.active_attempt is not None}
            pending_dag = [task_id for task_id, runtime in state.tasks.items() if runtime.state == "pending"]
            now = self.clock()
            for snapshot in snapshots:
                kills, transfers, changes = reconcile_host_probe(state, cluster, tasks, snapshot, now)
                for event in changes:
                    if event.kind == "task_completed" and not event.details.get("prechecked"):
                        payload = snapshot.attempts[event.task_id].done_payload
                        event.details["skipped_existing"] = bool(payload.get("skipped_existing"))
                        state.tasks[event.task_id].attempts[-1].failure_kind = None
                if snapshot.ok:
                    planned = {action.task_id for action in kills}
                    for task_id, runtime, attempt in _attempts_on(state, snapshot.host_id):
                        if attempt.failure_kind == "preempted_external" and task_id not in planned:
                            kills.append(PreemptAction(task_id, attempt.attempt, snapshot.host_id,
                                                       attempt.pgid, attempt.gpu_ids, "preempted_external"))
                            changes.append(_finish_failure(tasks[task_id], runtime, attempt,
                                                           kind="preempted_external",
                                                           reason="preempted_external", now=now))
                preempts.extend(kills)
                pulls.extend(transfers)
                events.extend(changes)
            pending = {}
            for action in preempts:
                if action.reason in _WATCHDOG_REASONS:
                    continue
                # Keep the lease until SSH confirms the kill. Persist the intended transition
                # only after that confirmation so a failed kill cannot duplicate a task.
                pending[action.task_id] = state.tasks[action.task_id]
                state.tasks[action.task_id] = before[action.task_id]
                state.tasks[action.task_id].active_attempt.failure_kind = "preempted_external"
            deferred = {event.task_id: event for event in events
                        if event.kind == "task_preempted" and event.task_id in pending}
            events = [event for event in events if deferred.get(event.task_id) is not event]
            if pending:
                # Recalculate DAG decisions against confirmed transitions only.
                for task_id in pending_dag:
                    state.tasks[task_id].state = "pending"
                events = [event for event in events if event.kind not in ("task_ready", "task_blocked_dep")]
                events.extend(advance_dag_states(state, tasks, now))
            self._persist(run_dir, cluster, state, events, probes=snapshots)

        kill_events, failed_kills = self._kill(cluster, preempts)
        with _locked(run_dir):
            state = _load_state(run_dir)
            now = self.clock()
            for action in preempts:
                if action.task_id in failed_kills:
                    if action.reason in _WATCHDOG_REASONS:
                        state.tasks[action.task_id].active_attempt.state = "running"
                elif action.reason not in _WATCHDOG_REASONS:
                    state.tasks[action.task_id] = pending[action.task_id]
                    kill_events.append(deferred[action.task_id])
            # The scheduler records an outage after three failures. Only dispatch to
            # hosts whose most recent observation succeeded, including during that grace period.
            unavailable = [host for host in state.hosts.values() if host.consecutive_probe_failures]
            saved = [host.preflight_ok for host in unavailable]
            for host in unavailable:
                host.preflight_ok = False
            assignments, changes = plan_dispatches(state, cluster, tasks, state.run_id, now)
            for host, preflight_ok in zip(unavailable, saved):
                host.preflight_ok = preflight_ok
            self._persist(run_dir, cluster, state, kill_events + changes)

        dispatched = []
        for assignment in assignments:
            if self._interrupted or (run_dir / "stop.flag").exists():
                break
            host = cluster.hosts[assignment.host_id]
            task = tasks[assignment.task_id]
            run_root = f"{host.work_root.rstrip('/')}/{state.run_id}"
            inputs_dir = f"{run_root}/inputs/{task.id}"
            dep_dirs = {dep: state.tasks[dep].completed_out_dir for dep in task.depends_on
                        if state.tasks[dep].completed_out_dir is not None}
            cross_relays = needs_cross_host_relay(state, task, host.id)
            if cross_relays:
                with _locked(run_dir):
                    state = _load_state(run_dir)
                    changes = mark_task_relaying(state, assignment, self.clock())
                    self._persist(run_dir, cluster, state, changes)
                try:
                    for relay in cross_relays:
                        dep = state.tasks[relay.from_task]
                        src_path = f"{dep.completed_out_dir.rstrip('/')}/{relay.rel_path}"
                        cache_dir = f"{run_root}/relay_cache/{relay.from_task}"
                        dst_path = f"{cache_dir}/{relay.rel_path}"
                        self.transport.relay_file(
                            cluster.hosts[dep.completed_host], src_path, host, dst_path,
                            local_cache_path=run_dir / "relay_cache" / relay.from_task / relay.rel_path,
                            max_bytes=relay.max_bytes,
                        )
                        dep_dirs[relay.from_task] = cache_dir
                        if relay.target_rel_path and not self.transport.stage_local_input(
                            host, dst_path, f"{inputs_dir}/{relay.target_rel_path}",
                        ):
                            raise RelayError("Could not stage the relayed input.",
                                             reason="relay_stage_failed", retryable=True)
                except Exception as exc:
                    reason = exc.reason if isinstance(exc, RelayError) else "relay_transport_error"
                    retryable = exc.retryable if isinstance(exc, RelayError) else True
                    with _locked(run_dir):
                        state = _load_state(run_dir)
                        changes = mark_relay_failed(
                            state, manifest, assignment, reason, self.clock(), retryable=retryable,
                        )
                        self._persist(run_dir, cluster, state, changes)
                    continue
                with _locked(run_dir):
                    _append_events(run_dir, [SchedulerEvent(
                        self.clock(), "relay_completed", task.id, host.id,
                        {"attempt": assignment.attempt, "files": len(cross_relays)},
                    )])
            options = dict(run_id=state.run_id, out_dir=assignment.out_dir,
                           attempt_dir=assignment.attempt_dir, gpu_ids=assignment.gpu_ids)
            try:
                for relay in task.relay_inputs:
                    dep = state.tasks[relay.from_task]
                    if dep.completed_host == host.id and relay.target_rel_path:
                        src_path = f"{dep.completed_out_dir.rstrip('/')}/{relay.rel_path}"
                        if not self.transport.stage_local_input(
                            host, src_path, f"{inputs_dir}/{relay.target_rel_path}",
                        ):
                            raise ValidationError("Could not stage the same-host input.")
                command = render_task_command(
                    task, host, dep_out_dirs=dep_dirs, inputs_dir=inputs_dir, **options,
                )
            except Exception as exc:
                ok, pgid, reason = False, None, str(exc)
            else:
                if self._interrupted or (run_dir / "stop.flag").exists():
                    if cross_relays:
                        with _locked(run_dir):
                            state = _load_state(run_dir)
                            state.tasks[task.id].state = "ready"
                            self._persist(run_dir, cluster, state, [
                                SchedulerEvent(self.clock(), "relay_interrupted", task.id, host.id),
                            ])
                    break
                ok, pgid, reason = self.transport.launch_detached(
                    host, task=task, rendered_cmd=command, **options,
                )
            with _locked(run_dir):
                state = _load_state(run_dir)
                now = self.clock()
                if ok:
                    event = mark_dispatch_started(state, assignment, now, pgid=pgid)
                    dispatched.append({**assignment.to_dict(), "pgid": pgid})
                else:
                    event = mark_dispatch_launch_failed(
                        state, assignment, task, now, reason=(reason or "launch_failed")[:2048],
                    )
                self._persist(run_dir, cluster, state,
                              [event, *advance_dag_states(state, tasks, now)])

        return {
            "run_id": state.run_id, "terminal": is_run_terminal(state),
            "task_counts": build_summary(state)["task_counts"], "dispatched": dispatched,
            "preempted": [action.to_dict() for action in preempts if action.task_id not in failed_kills],
        }, cluster, pulls

    def run_tick(
        self, run_dir: Path, *, force_probe: bool = False, sync_pulls: bool = True,
        pull_executor: ThreadPoolExecutor | None = None,
    ) -> dict[str, Any]:
        run_dir = _existing_run(run_dir)
        with _locked(run_dir, "dispatch.lock"):
            return self._tick_receipt(run_dir, force_probe, sync_pulls, pull_executor)

    def _tick_receipt(self, run_dir, force_probe, sync_pulls, pull_executor):
        receipt, cluster, pulls = self._run_tick(run_dir, force_probe=force_probe)
        receipt["pulled"] = []
        for action in pulls:
            if not action.mirror_root and not action.pull_to_local:
                continue
            if sync_pulls or pull_executor is None:
                receipt["pulled"].append(self._pull_action(run_dir, cluster, receipt["run_id"], action))
            else:
                pull_executor.submit(self._pull_action, run_dir, cluster, receipt["run_id"], action)
        return {**receipt, "host_gpu_summary": self.status(run_dir)["host_gpu_summary"]}

    def _pull_action(self, run_dir, cluster, run_id, action, dest=None):
        host = cluster.hosts[action.host_id]
        errors = {}
        if action.mirror_root:
            mirror_dir = f"{action.mirror_root.rstrip('/')}/{run_id}/{action.task_id}"
            result = self.transport.mirror_remote_outputs(
                host, out_dir=action.out_dir, mirror_dir=mirror_dir, excludes=action.pull_excludes,
            )
            if not result.ok:
                errors["mirror"] = _error(result)
        if action.pull_to_local:
            local = dest if dest is not None else run_dir / "results" / action.task_id
            result = self.transport.pull_outputs_tar(
                host, remote_out_dir=action.out_dir, local_dest_dir=local, excludes=action.pull_excludes,
                max_mb=action.pull_max_mb,
            )
            if not result.ok:
                errors["local"] = _error(result)
        record = {"task_id": action.task_id, "ok": not errors, "attempt": action.attempt, "errors": errors}
        with _locked(run_dir):
            state = _load_state(run_dir)
            kind = "outputs_pulled" if record["ok"] else "outputs_pull_failed"
            _append_events(run_dir, [SchedulerEvent(self.clock(), kind, action.task_id, host.id, record)])
            self._summary(run_dir, cluster, state, pulled={action.task_id: record})
        return record

    def status(self, run_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
        run_dir = _existing_run(run_dir)
        if refresh:
            self.run_tick(run_dir, force_probe=True)
        with _locked(run_dir):
            summary = _read_json(run_dir / "summary.json")
            pid = _pid(run_dir)
            return {**summary, "controller_pid": pid, "controller_alive": _alive(pid)}

    def run_loop(
        self, run_dir: Path, *, poll_interval_s: float = 20.0, max_ticks: int | None = None,
    ) -> int:
        return self._run_loop(run_dir, poll_interval_s, max_ticks, detached=False)

    def _run_loop(self, run_dir, poll_interval_s, max_ticks, *, detached):
        _poll_interval(poll_interval_s)
        if max_ticks is not None and max_ticks < 0:
            raise ValidationError("The maximum tick count must be nonnegative.")
        run_dir = _existing_run(run_dir)
        received = None
        registered = False
        previous = {}
        pid = os.getpid()

        def on_signal(sig, _frame):
            nonlocal received
            received = sig
            self._interrupted = True

        def stopping():
            return received is not None or (run_dir / "stop.flag").exists()

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, on_signal)
            with _locked(run_dir, "dispatch.lock"), _locked(run_dir):
                cluster, _, state = _load_run(run_dir)
                owner = _pid(run_dir)
                if _alive(owner) and not (detached and owner in (pid, os.getppid())):
                    raise ValidationError("A controller is already running.")
                _atomic_text(run_dir / "controller.pid", f"{pid}\n")
                registered = True
                self._summary(run_dir, cluster, state)
            with ThreadPoolExecutor(max_workers=4) as pool:
                ticks = 0
                terminal = is_run_terminal(state)
                while not terminal and not stopping() and (max_ticks is None or ticks < max_ticks):
                    if self.run_tick(run_dir, sync_pulls=False, pull_executor=pool)["terminal"]:
                        break
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    remaining = poll_interval_s
                    while remaining > 0 and not stopping():
                        duration = min(remaining, 0.5)
                        self.sleeper(duration)
                        remaining -= duration
        finally:
            try:
                if registered:
                    try:
                        self.transport.close_all(cluster.hosts.values())
                    finally:
                        with _locked(run_dir):
                            if _pid(run_dir) == pid:
                                (run_dir / "controller.pid").unlink()
                            self._summary(run_dir, cluster, _load_state(run_dir))
            finally:
                self._interrupted = False
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 128 + received if received is not None else 0

    def start_detached(
        self, run_dir: Path, cluster_path: Path | None, manifest_path: Path | None, *,
        poll_interval_s: float = 20.0, caffeinate: bool = False, sync: bool = False,
        resume: bool = False,
    ) -> dict[str, Any]:
        return self._start(run_dir, cluster_path, manifest_path, poll_interval_s=poll_interval_s,
                           caffeinate=caffeinate, sync=sync, resume=resume, skip_preflight=False)

    def _start(self, run_dir, cluster_path, manifest_path, *, poll_interval_s, caffeinate,
               sync, resume, skip_preflight):
        _poll_interval(poll_interval_s)
        if resume:
            run_dir = _existing_run(run_dir)
        else:
            if manifest_path is None:
                raise ValidationError("--manifest is required for a fresh start.")
            cluster_path = cluster_path if cluster_path is not None else resolve_cluster_path(None)
            run_dir, cluster, manifest, local_dir = self._prepare_run(run_dir, cluster_path, manifest_path, sync)
        with _locked(run_dir, "dispatch.lock"):
            if resume:
                with _locked(run_dir):
                    if _alive(_pid(run_dir)):
                        raise ValidationError("A controller is already running.")
                    _load_run(run_dir)
                    (run_dir / "stop.flag").unlink(missing_ok=True)
                    (run_dir / "controller.pid").unlink(missing_ok=True)
            else:
                self._initialize(run_dir, cluster, manifest, cluster_path, manifest_path,
                                 local_dir, skip_preflight)
            tick = self._tick_receipt(run_dir, True, True, None)
            hosts = tick["host_gpu_summary"]
            with _locked(run_dir):
                state = _load_state(run_dir)
            progressed = any(runtime.state == "completed" or any(attempt.pgid for attempt in runtime.attempts)
                             for runtime in state.tasks.values())
            healthy = any(host.get("ok") and host["preflight_ok"] and host["state"] == "ready"
                          for host in hosts.values())
            pid = None
            if not healthy and not progressed and not tick["terminal"]:
                return {**tick, "ok": False, "exit_code": 6, "run_dir": str(run_dir), "controller_pid": pid}
            if not tick["terminal"]:
                with _locked(run_dir):
                    if not (run_dir / "stop.flag").exists():
                        pid = self._spawn(run_dir, poll_interval_s, caffeinate)
                        _atomic_text(run_dir / "controller.pid", f"{pid}\n")
                        cluster, _, state = _load_run(run_dir)
                        self._summary(run_dir, cluster, state)
            return {"ok": True, "run_id": tick["run_id"], "run_dir": str(run_dir),
                    "controller_pid": pid, "dispatched": tick["dispatched"],
                    "task_counts": tick["task_counts"], "host_gpu_summary": hosts}

    def _spawn(self, run_dir, interval, caffeinate):
        scripts = Path(__file__).resolve().parents[2]
        entry = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from codex_orch.remote.controller import _detached_main; "
            "sys.exit(_detached_main(sys.argv[2], float(sys.argv[3])))"
        )
        command = [sys.executable, "-I", "-B", "-c", entry, str(scripts), str(run_dir), str(interval)]
        if caffeinate and shutil.which("caffeinate"):
            command = ["caffeinate", "-i", *command]
        fd = os.open(run_dir / "controller.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            os.fchmod(log.fileno(), 0o600)
            process = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
        return process.pid

    def _cordon(self, run_dir, host_id, gpu, cordoned, preempt, reason):
        run_dir = _existing_run(run_dir)
        receipt = {"action": "cordon" if cordoned else "uncordon", "host_id": host_id}
        with _locked(run_dir, "dispatch.lock"):
            with _locked(run_dir):
                cluster, manifest, state = _load_run(run_dir)
                if host_id not in state.hosts:
                    raise ValidationError(f"Unknown host: {host_id}")
                if gpu is not None and (type(gpu) is not int or gpu < 0):
                    raise ValidationError("The GPU index must be a nonnegative integer.")
                runtime = state.hosts[host_id]
                if gpu is None:
                    runtime.cordoned = cordoned
                    runtime.cordon_reason = reason if cordoned else None
                    if runtime.state in ("ready", "cordoned"):
                        runtime.state = "cordoned" if cordoned else "ready"
                else:
                    indices = set(runtime.cordoned_gpus)
                    (indices.add if cordoned else indices.discard)(gpu)
                    runtime.cordoned_gpus = sorted(indices)
                event = SchedulerEvent(self.clock(), "host_cordoned" if cordoned else "host_uncordoned",
                                       host_id=host_id, details={"gpu": gpu, "reason": reason})
                summary = self._persist(run_dir, cluster, state, [event])
                affected = {
                    task_id: attempt for task_id, _, attempt in _attempts_on(state, host_id)
                    if preempt and (gpu is None or gpu in attempt.gpu_ids)
                }
            if not affected:
                return {**summary, **receipt, "ok": True}
            actions = [PreemptAction(task_id, a.attempt, host_id, a.pgid, a.gpu_ids, reason)
                       for task_id, a in affected.items()]
            events, failed = self._kill(cluster, actions)
            with _locked(run_dir):
                state = _load_state(run_dir)
                now = self.clock()
                for task_id in affected.keys() - failed:
                    task = state.tasks[task_id]
                    attempt = task.active_attempt
                    attempt.state = "preempted"
                    attempt.failure_kind = "preempted_external"
                    attempt.finished_at = now
                    task.state = "ready"
                    task.next_eligible_at = now
                    events.append(SchedulerEvent(now, "task_preempted", task_id, host_id,
                                                 {"reason": reason, "attempt": attempt.attempt}))
                summary = self._persist(run_dir, cluster, state, events)
            return {**summary, **receipt, "ok": not failed, "failed_kills": sorted(failed)}

    def cordon(
        self, run_dir: Path, *, host_id: str, gpu: int | None = None, preempt: bool = False,
        reason: str = "operator_cordon",
    ) -> dict[str, Any]:
        return self._cordon(run_dir, host_id, gpu, True, preempt, reason)

    def uncordon(self, run_dir: Path, *, host_id: str, gpu: int | None = None) -> dict[str, Any]:
        return self._cordon(run_dir, host_id, gpu, False, False, None)

    def requeue(
        self, run_dir: Path, *, task_id: str | None = None, failed_only: bool = False,
        clear_infra_cooldown: bool = False,
    ) -> dict[str, Any]:
        if task_id is None and not failed_only:
            raise ValidationError("Specify a task or --failed.")
        run_dir = _existing_run(run_dir)
        with _locked(run_dir, "dispatch.lock"), _locked(run_dir):
            cluster, manifest, state = _load_run(run_dir)
            eligible = {"failed", "blocked_dep"} if failed_only else {"failed", "blocked_dep", "cancelled"}
            if task_id is not None and (task_id not in state.tasks or state.tasks[task_id].state not in eligible):
                raise ValidationError(f"Task is unknown or cannot be requeued: {task_id}")
            now = self.clock()
            events = []
            requeued = []
            for tid, runtime in state.tasks.items():
                if runtime.state not in eligible or (task_id is not None and tid != task_id):
                    continue
                runtime.state = "pending"
                runtime.next_eligible_at = now
                runtime.retries_used = runtime.infra_retries_used = runtime.preempt_retries_used = 0
                if clear_infra_cooldown:
                    runtime.failed_infra_hosts.clear()
                requeued.append(tid)
                events.append(SchedulerEvent(now, "task_requeued", tid, details={"reason": "operator_requeue"}))
            events.extend(advance_dag_states(state, {task.id: task for task in manifest.tasks}, now))
            summary = self._persist(run_dir, cluster, state, events)
            return {**summary, "ok": True, "action": "requeue", "requeued_tasks": sorted(requeued)}

    def add_tasks(self, run_dir: Path, tasks_path: Path) -> dict[str, Any]:
        run_dir = _existing_run(run_dir)
        with _locked(run_dir, "dispatch.lock"), _locked(run_dir):
            cluster, manifest, state = _load_run(run_dir)
            combined, new_tasks = load_and_merge_additional_tasks(manifest, tasks_path, cluster)
            added = [task.id for task in new_tasks]
            for task_id in added:
                state.tasks[task_id] = TaskRuntime()
            now = self.clock()
            events = advance_dag_states(state, {task.id: task for task in combined.tasks}, now)
            events.append(SchedulerEvent(now, "tasks_added", details={"task_ids": added}))
            event_path = run_dir / "events.jsonl"
            event_log = event_path.read_text(encoding="utf-8") if event_path.exists() else ""
            _write_json(run_dir / "manifest.snapshot.json", combined.to_dict())
            _write_json(run_dir / "state.json", state.to_dict())
            _atomic_text(event_path, event_log + _event_records(events))
            summary = self._summary(run_dir, cluster, state)
            return {**summary, "ok": True, "action": "add_tasks", "added_tasks": added}

    def cancel(
        self, run_dir: Path, *, task_id: str | None = None, cancel_all: bool = False,
    ) -> dict[str, Any]:
        if (task_id is not None) == cancel_all:
            raise ValidationError("Specify a task or --all.")
        run_dir = _existing_run(run_dir)
        with _locked(run_dir, "dispatch.lock"):
            with _locked(run_dir):
                cluster, manifest, state = _load_run(run_dir)
                if task_id is not None and task_id not in state.tasks:
                    raise ValidationError(f"Unknown task: {task_id}")
                selected = {
                    tid: runtime.active_attempt for tid, runtime in state.tasks.items()
                    if runtime.state not in TERMINAL_TASK_STATES and (cancel_all or tid == task_id)
                }
            kills = [PreemptAction(tid, a.attempt, a.host_id, a.pgid, a.gpu_ids, "operator_cancel")
                     for tid, a in selected.items() if a is not None]
            events, failed = self._kill(cluster, kills)
            with _locked(run_dir):
                state = _load_state(run_dir)
                now = self.clock()
                for tid in sorted(selected.keys() - failed):
                    runtime = state.tasks[tid]
                    attempt = runtime.active_attempt
                    if attempt is not None:
                        attempt.state = "cancelled"
                        attempt.finished_at = now
                    runtime.state = "cancelled"
                    events.append(SchedulerEvent(now, "task_cancelled", tid))
                events.extend(advance_dag_states(state, {task.id: task for task in manifest.tasks}, now))
                summary = self._persist(run_dir, cluster, state, events)
            return {**summary, "ok": not failed, "failed_kills": sorted(failed), "action": "cancel",
                    "cancelled_tasks": sorted(selected.keys() - failed)}

    def pull(
        self, run_dir: Path, *, task_id: str | None = None, dest: Path | None = None,
    ) -> dict[str, Any]:
        run_dir = _existing_run(run_dir)
        with _locked(run_dir):
            cluster, manifest, state = _load_run(run_dir)
            if task_id is not None and (task_id not in state.tasks or state.tasks[task_id].state != "completed"):
                raise ValidationError(f"Task is unknown or has not completed: {task_id}")
            actions = []
            for task in manifest.tasks:
                runtime = state.tasks[task.id]
                if runtime.state != "completed" or (task_id is not None and task.id != task_id):
                    continue
                actions.append(PullAction(
                    task.id, runtime.attempts[-1].attempt, runtime.completed_host,
                    runtime.completed_out_dir, cluster.hosts[runtime.completed_host].mirror_root,
                    True, task.pull_excludes, task.pull_max_mb,
                ))
        if dest is not None:
            dest = Path(dest).expanduser()
        pulled = [
            self._pull_action(run_dir, cluster, state.run_id, action,
                              dest / action.task_id if dest is not None and task_id is None else dest)
            for action in actions
        ]
        return {"run_id": state.run_id, "ok": all(record["ok"] for record in pulled), "pulled": pulled}

    def stop(self, run_dir: Path, *, kill_remote: bool = False) -> dict[str, Any]:
        run_dir = _existing_run(run_dir)
        with _locked(run_dir):
            cluster, _, state = _load_run(run_dir)
            _atomic_text(run_dir / "stop.flag", "stop\n")
            pid = _pid(run_dir)
            if _alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self._persist(run_dir, cluster, state, [SchedulerEvent(self.clock(), "stop_requested")])
        for _ in range(10):
            if not _alive(pid) or _pid(run_dir) != pid:
                break
            self.sleeper(0.5)
        failed = set()
        cancelled = []
        if kill_remote:
            with _locked(run_dir, "dispatch.lock"):
                with _locked(run_dir):
                    cluster, manifest, state = _load_run(run_dir)
                    actions = [PreemptAction(tid, a.attempt, host_id, a.pgid, a.gpu_ids, "operator_stop")
                               for host_id in state.hosts for tid, _, a in _attempts_on(state, host_id)]
                events, failed = self._kill(cluster, actions)
                with _locked(run_dir):
                    state = _load_state(run_dir)
                    now = self.clock()
                    for action in actions:
                        if action.task_id in failed:
                            continue
                        runtime = state.tasks[action.task_id]
                        attempt = runtime.active_attempt
                        if attempt is None or attempt.attempt != action.attempt:
                            continue
                        if type(action.pgid) is not int or action.pgid <= 1:
                            failed.add(action.task_id)
                            continue
                        attempt.state = runtime.state = "cancelled"
                        attempt.finished_at = now
                        attempt.cancel_reason = "stop_kill_remote"
                        cancelled.append(action.task_id)
                        events.append(SchedulerEvent(
                            now, "task_cancelled", action.task_id, attempt.host_id,
                            {"attempt": attempt.attempt, "reason": "stop_kill_remote"},
                        ))
                    events.extend(advance_dag_states(state, {task.id: task for task in manifest.tasks}, now))
                    summary = self._persist(run_dir, cluster, state, events)
            return {**summary, "ok": not failed, "failed_kills": sorted(failed),
                    "cancelled_tasks": sorted(cancelled)}
        summary = self.status(run_dir)
        _update_dispatch_registry(run_dir, summary)
        return {**summary, "ok": not failed and not summary["controller_alive"],
                "failed_kills": sorted(failed), "cancelled_tasks": []}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(redact_secrets(message))


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="remote_dispatch.sh")
    parser.add_argument("--format", choices=("json", "table"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "start", "run", "tick", "status", "cordon", "uncordon",
                 "requeue", "cancel", "add-tasks", "pull", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--format", choices=("json", "table"), default=argparse.SUPPRESS)
        if name in ("validate", "plan", "start"):
            command.add_argument("--cluster")
            command.add_argument("--manifest", type=Path, required=name != "start")
        if name not in ("validate", "plan"):
            command.add_argument("--run-dir", type=Path, required=True)
        if name in ("start", "run"):
            command.add_argument("--poll-interval-s", "--poll-interval", type=float, default=20.0)
        if name == "plan":
            command.add_argument("--live-probe", action="store_true")
        elif name == "start":
            for flag in ("caffeinate", "sync", "resume", "skip-preflight"):
                command.add_argument(f"--{flag}", action="store_true")
        elif name == "run":
            command.add_argument("--max-ticks", type=int)
        elif name == "tick":
            command.add_argument("--force-probe", action="store_true")
        elif name == "status":
            command.add_argument("--refresh", action="store_true")
        elif name in ("cordon", "uncordon"):
            command.add_argument("--host", "--host-id", dest="host_id", required=True)
            command.add_argument("--gpu", type=int)
            if name == "cordon":
                command.add_argument("--preempt", action="store_true")
                command.add_argument("--reason", default="operator_cordon")
        elif name in ("requeue", "cancel"):
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--task", "--task-id", dest="task_id")
            if name == "requeue":
                target.add_argument("--failed", dest="failed_only", action="store_true")
                command.add_argument("--clear-infra-cooldown", action="store_true")
            else:
                target.add_argument("--all", dest="cancel_all", action="store_true")
        elif name == "pull":
            command.add_argument("--task", "--task-id", dest="task_id")
            command.add_argument("--dest", type=Path)
        elif name == "stop":
            command.add_argument("--kill-remote", action="store_true")
        elif name == "add-tasks":
            command.add_argument("--tasks", dest="tasks_path", type=Path, required=True)
    return parser


def _table(title, columns, rows) -> None:
    print(f"\n{title}")
    cells = [columns, *[tuple("-" if item is None else str(item) for item in row) for row in rows]]
    widths = [max(map(len, column)) for column in zip(*cells)]
    for row in cells:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _emit(result: dict[str, Any], output_format: str) -> None:
    result = _redact(result)
    limit = 1024 * 1024
    encoded = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) > limit:
        result = {key: result[key] for key in ("ok", "run_id", "task_counts", "exit_code") if key in result}
        result["truncated"] = True
        encoded = json.dumps(result, separators=(",", ":"))
    if output_format == "json":
        print(encoded)
        return

    hosts = result.get("host_gpu_summary")
    live_plan = hosts is not None and "execution_order" in result
    status = hosts is not None and not live_plan and isinstance(result.get("tasks"), dict)
    sections = {"host_gpu_summary", "execution_order"} if live_plan else set()
    if status:
        sections.update(("hosts", "host_gpu_summary", "tasks", "pulled_tasks"))
    print(f"{'FIELD':<24} VALUE")
    for key, value in result.items():
        if key not in sections:
            print(f"{key:<24} {json.dumps(value, ensure_ascii=True)}")
    if live_plan:
        _table("HOSTS / GPUS", ("HOST", "OK", "DISK_GB", "GPU", "MEM_MB", "UTIL%", "EXT_BUSY"), [
            (host_id, host.get("ok"), host.get("disk_free_gb"), gpu.get("index"),
             f"{gpu['memory_used_mb']}/{gpu['memory_total_mb']}" if gpu else None,
             gpu.get("utilization_pct"), gpu.get("external_busy"))
            for host_id, host in sorted(hosts.items()) for gpu in (host.get("gpus") or [{}])
        ])
        _table("EXECUTION_ORDER", ("TASK_ID",), [(tid,) for tid in result["execution_order"]])
    if status:
        _table("HOSTS", ("HOST", "STATE", "FREE", "LEASED", "DISK_GB", "DRAINING_FOR"), [
            (host_id, host["state"], ",".join(map(str, host["free_gpu_ids"])) or "-",
             ",".join(map(str, host["leased_gpu_ids"])) or "-", host.get("disk_free_gb"),
             host.get("draining_for"))
            for host_id, host in sorted(hosts.items())
        ])
        rows = []
        for task_id, task in sorted(result["tasks"].items()):
            attempts = task["attempts"]
            last = attempts[-1] if attempts else {}
            rows.append((task_id, task["state"], task.get("completed_host") or last.get("host_id"),
                         ",".join(map(str, last.get("gpu_ids", []))) or "-", len(attempts),
                         last.get("exit_code"), last.get("failure_kind") or last.get("cancel_reason")))
        _table("TASKS", ("TASK_ID", "STATE", "HOST", "GPUS", "ATTEMPTS", "EXIT", "FAILURE"), rows)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = vars(_parser().parse_args(argv))
    except SystemExit as exc:
        return int(exc.code)
    command = args.pop("command")
    output_format = args.pop("format")
    controller = RemoteController()
    try:
        if command in ("validate", "plan", "start"):
            cluster = args.pop("cluster")
            args["cluster_path"] = None if args.get("resume") else resolve_cluster_path(cluster)
            args["manifest_path"] = args.pop("manifest")
        if command == "run":
            return controller.run_loop(**args)
        method = {"start": controller._start, "tick": controller.run_tick}.get(command)
        result = (method or getattr(controller, command.replace("-", "_")))(**args)
        _emit(result, output_format)
        return result.get("exit_code", 0 if result.get("ok", True) else 1)
    except ValidationError as exc:
        _emit({"ok": False, "error": str(exc)[:2048], "exit_code": 2}, output_format)
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)[:2048], "exit_code": 1}, output_format)
        return 1


def _detached_main(run_dir: str, interval: float) -> int:
    try:
        return RemoteController()._run_loop(Path(run_dir), interval, None, detached=True)
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)[:2048], "exit_code": 1}, "json")
        return 1


if __name__ == "__main__":
    sys.exit(main())
