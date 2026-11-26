"""Deterministic scheduling decisions over caller-supplied state and probes."""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Iterator

from .models import (
    TERMINAL_TASK_STATES,
    VALID_ID_RE,
    ClusterConfig,
    DispatchManifest,
    GpuSlotSnapshot,
    HostConfig,
    HostProbeSnapshot,
    HostRuntime,
    RunState,
    TaskAttempt,
    TaskRuntime,
    TaskSpec,
    ValidationError,
    _JsonRecord,
    host_matches_task_static,
)

_INFRA_LOG = re.compile(
    r"uncorrectable ECC|NCCL (?:error|failure)|\bbus error\b|"
    r"transport error|connection reset by peer",
    re.IGNORECASE,
)


@dataclass
class SchedulerEvent(_JsonRecord):
    ts: float
    kind: str
    task_id: str | None = None
    host_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchAssignment(_JsonRecord):
    task_id: str
    host_id: str
    gpu_ids: list[int]
    attempt: int
    out_dir: str
    attempt_dir: str


@dataclass
class PreemptAction(_JsonRecord):
    task_id: str
    attempt: int
    host_id: str
    pgid: int | None
    gpu_ids: list[int]
    reason: str


@dataclass
class PullAction(_JsonRecord):
    task_id: str
    attempt: int
    host_id: str
    out_dir: str
    mirror_root: str | None
    pull_to_local: bool
    pull_excludes: tuple[str, ...]
    pull_max_mb: int


def init_run_state(
    cluster: ClusterConfig, manifest: DispatchManifest, *,
    cluster_path: str, manifest_path: str, now: float,
) -> RunState:
    run_id = manifest.run_id
    if run_id is None:
        run_id = f"run-{int(now * 1_000_000)}"
    return RunState(
        run_id=run_id, cluster_path=cluster_path, manifest_path=manifest_path, created_at=now,
        tasks={task.id: TaskRuntime() for task in manifest.tasks},
        hosts={host_id: HostRuntime(preflight_ok=manifest.preflight is None) for host_id in cluster.hosts},
    )


def advance_dag_states(
    state: RunState, tasks_by_id: dict[str, TaskSpec], now: float,
) -> list[SchedulerEvent]:
    events = []
    pending = sorted(task_id for task_id, runtime in state.tasks.items() if runtime.state == "pending")
    successors = {task_id: [] for task_id in state.tasks}
    for task_id in pending:
        for dep in dict.fromkeys(tasks_by_id[task_id].depends_on):
            successors[dep].append(task_id)
    queue = deque(pending)
    while queue:
        task_id = queue.popleft()
        runtime = state.tasks[task_id]
        if runtime.state != "pending":
            continue
        task = tasks_by_id[task_id]
        deps = [state.tasks[dep].state for dep in task.depends_on]
        failed_dep = any(status in TERMINAL_TASK_STATES and status != "completed" for status in deps)
        all_terminal = all(status in TERMINAL_TASK_STATES for status in deps)
        if all(status == "completed" for status in deps) or (
            task.allow_partial_deps and all_terminal and "completed" in deps
        ):
            runtime.state = "ready"
        elif failed_dep and (not task.allow_partial_deps or all_terminal):
            runtime.state = "blocked_dep"
            queue.extend(successors[task_id])
        else:
            continue
        events.append(SchedulerEvent(now, f"task_{runtime.state}", task_id=task_id))
    return events


def _active_on(state: RunState, host_id: str) -> Iterator[tuple[str, TaskRuntime, TaskAttempt]]:
    for task_id in sorted(state.tasks):
        runtime = state.tasks[task_id]
        attempt = runtime.active_attempt
        if attempt is not None and attempt.host_id == host_id:
            yield task_id, runtime, attempt


def _non_retryable(task: TaskSpec, exit_code: int | None, log_tail: str) -> bool:
    policy = task.retry_policy
    return exit_code in policy.non_retryable_exit_codes or any(
        re.search(pattern, log_tail) for pattern in policy.non_retryable_log_patterns
    )


def _finish_failure(
    task: TaskSpec, runtime: TaskRuntime, attempt: TaskAttempt, *,
    kind: str, reason: str, now: float, retryable: bool = True,
) -> SchedulerEvent:
    policy = task.retry_policy
    attempt.state = "preempted" if kind == "preempted_external" else "failed"
    attempt.failure_kind = kind
    attempt.finished_at = now

    if kind == "infra_error":
        counter, limit = "infra_retries_used", policy.max_infra_retries
    elif kind == "preempted_external":
        counter, limit = "preempt_retries_used", policy.max_preempt_retries
    else:
        counter, limit = "retries_used", policy.max_retries

    used = getattr(runtime, counter)
    if retryable and used < limit:
        setattr(runtime, counter, used + 1)
        runtime.state = "ready"
        runtime.next_eligible_at = now + (0 if kind == "preempted_external" else policy.backoff_s)
        if kind == "infra_error":
            runtime.failed_infra_hosts[attempt.host_id] = now + max(policy.backoff_s * 4, 120)
    else:
        runtime.state = "failed"

    event_kind = "task_preempted" if kind == "preempted_external" else (
        "task_requeued" if runtime.state == "ready" else "task_failed"
    )
    return SchedulerEvent(now, event_kind, task.id, attempt.host_id, {
        "attempt": attempt.attempt, "failure_kind": kind, "reason": reason,
        "exit_code": attempt.exit_code, "state": runtime.state,
    })


def _preempt(task_id: str, attempt: TaskAttempt, reason: str) -> PreemptAction:
    return PreemptAction(
        task_id, attempt.attempt, attempt.host_id, attempt.pgid, list(attempt.gpu_ids), reason,
    )


def _reconcile_attempt(
    task: TaskSpec, runtime: TaskRuntime, attempt: TaskAttempt, snapshot: HostProbeSnapshot,
    host: HostConfig, host_rt: HostRuntime, now: float, preempts: list[PreemptAction],
    pulls: list[PullAction], events: list[SchedulerEvent],
) -> None:
    status = snapshot.attempts.get(task.id)
    # An omitted observation says nothing about process liveness.
    if status is None or (status.attempt is not None and status.attempt != attempt.attempt):
        return
    payload = status.done_payload
    if payload is not None:
        attempt.exit_code = payload.get("exit_code")
    timed_out = attempt.state == "cancelling"
    if timed_out and status.pgid_alive:
        return
    if not timed_out and payload is not None and attempt.exit_code == 0 and (
        not task.done_markers or status.done_markers_present or payload.get("skipped_existing")
    ):
        attempt.state = runtime.state = "completed"
        attempt.finished_at = now
        runtime.completed_host = host.id
        runtime.completed_out_dir = attempt.out_dir
        events.append(SchedulerEvent(now, "task_completed", task.id, host.id, {"attempt": attempt.attempt}))
        if not payload.get("skipped_existing") or task.pull_to_local or host.mirror_root:
            pulls.append(PullAction(
                task.id, attempt.attempt, host.id, attempt.out_dir, host.mirror_root,
                task.pull_to_local, task.pull_excludes, task.pull_max_mb,
            ))
        if task.host_affinity.startswith("prefer_key:"):
            key = task.host_affinity[len("prefer_key:"):]
            if key not in host_rt.recent_affinity_keys:
                host_rt.recent_affinity_keys.append(key)
        return

    if timed_out:
        kind, reason = "task_error", "timeout"
    elif payload is not None:
        if attempt.exit_code == 0:
            kind, reason = "task_error", "missing_done_markers"
        elif _INFRA_LOG.search(status.log_tail):
            kind, reason = "infra_error", "infrastructure_log"
        else:
            kind, reason = "task_error", "nonzero_or_missing_exit_code"
    elif not status.pgid_alive:
        kind, reason = "infra_error", "process_vanished"
    else:
        if task.timeout_s is not None and now - attempt.started_at > task.timeout_s:
            attempt.state = "cancelling"
            preempts.append(_preempt(task.id, attempt, "timeout"))
            events.append(SchedulerEvent(now, "task_timeout", task.id, host.id, {"attempt": attempt.attempt}))
        return

    events.append(_finish_failure(
        task, runtime, attempt, kind=kind, reason=reason, now=now,
        retryable=not _non_retryable(task, attempt.exit_code, status.log_tail),
    ))


def _unreachable(
    state: RunState, host_id: str, host_rt: HostRuntime, tasks_by_id: dict[str, TaskSpec], now: float,
) -> list[SchedulerEvent]:
    events = []
    host_rt.consecutive_probe_failures += 1
    if host_rt.first_probe_failure_at is None:
        host_rt.first_probe_failure_at = now
    if host_rt.consecutive_probe_failures < 3:
        return events
    if host_rt.state != "unreachable":
        events.append(SchedulerEvent(now, "host_unreachable", host_id=host_id))
    host_rt.state = "unreachable"
    host_rt.next_probe_at = now + 30 * 2 ** min(2, host_rt.consecutive_probe_failures - 3)
    outage_since = host_rt.last_ok_at
    if outage_since is None:
        outage_since = host_rt.first_probe_failure_at
    for task_id, runtime, attempt in _active_on(state, host_id):
        task = tasks_by_id[task_id]
        if task.retry_policy.retry_on_host_lost and now - outage_since > state.host_lost_timeout_s:
            events.append(_finish_failure(
                task, runtime, attempt, kind="infra_error", reason="host_lost", now=now,
            ))
    return events


def _external_pressure(gpu: GpuSlotSnapshot, host: HostConfig) -> bool:
    return (
        gpu.external_busy or gpu.memory_used_mb > host.external_mem_threshold_mb
        or gpu.utilization_pct > host.max_gpu_util_pct
    )


def _reconcile_external(
    state: RunState, host: HostConfig, host_rt: HostRuntime, tasks_by_id: dict[str, TaskSpec],
    managed_gpus: set[int], now: float, preempts: list[PreemptAction], events: list[SchedulerEvent],
) -> None:
    occupied = {
        gpu.index for gpu in host_rt.gpus
        if gpu.external_pids or (gpu.index not in managed_gpus and _external_pressure(gpu, host))
    }
    host_rt.external_gpu_ticks = {
        str(gpu.index): host_rt.external_gpu_ticks.get(str(gpu.index), 0) + 1 if gpu.index in occupied else 0
        for gpu in sorted(host_rt.gpus, key=lambda slot: slot.index)
    }
    host_rt.external_host_ticks = host_rt.external_host_ticks + 1 if occupied else 0

    policy = host.external_gpu_policy
    yield_preempt = False
    if policy == "yield_host":
        if not occupied and host_rt.cordon_reason == "external_yield":
            host_rt.cordoned = False
            host_rt.cordon_reason = None
            if host_rt.state == "cordoned":
                host_rt.state = "ready"
            events.append(SchedulerEvent(now, "host_resumed", host_id=host.id))
        if occupied and host_rt.external_host_ticks >= host.external_grace_ticks and not host_rt.cordoned:
            host_rt.cordoned = True
            host_rt.cordon_reason = "external_yield"
            events.append(SchedulerEvent(now, "host_yielded", host_id=host.id))
            yield_preempt = host.yield_preempt_running

    collision_gpus = {gpu.index for gpu in host_rt.gpus if gpu.external_pids}
    for task_id, runtime, attempt in _active_on(state, host.id):
        collisions = sorted(collision_gpus.intersection(attempt.gpu_ids))
        if policy == "skip_gpu" and collisions:
            events.append(SchedulerEvent(now, "external_collision", task_id, host.id, {"gpu_ids": collisions}))
        if attempt.state == "cancelling":
            continue
        if yield_preempt or (policy == "preempt_gpu" and any(
            host_rt.external_gpu_ticks[str(gpu)] >= host.external_grace_ticks for gpu in collisions
        )):
            preempts.append(_preempt(task_id, attempt, "preempted_external"))
            events.append(_finish_failure(
                tasks_by_id[task_id], runtime, attempt,
                kind="preempted_external", reason="preempted_external", now=now,
            ))


def reconcile_host_probe(
    state: RunState, cluster: ClusterConfig, tasks_by_id: dict[str, TaskSpec],
    snapshot: HostProbeSnapshot, now: float,
) -> tuple[list[PreemptAction], list[PullAction], list[SchedulerEvent]]:
    """Apply a probe. Execute returned preemptions before launching replacements."""
    host = cluster.hosts[snapshot.host_id]
    host_rt = state.hosts[host.id]
    preempts: list[PreemptAction] = []
    pulls: list[PullAction] = []
    if not snapshot.ok:
        events = _unreachable(state, host.id, host_rt, tasks_by_id, now)
        events.extend(advance_dag_states(state, tasks_by_id, now))
        return preempts, pulls, events

    events: list[SchedulerEvent] = []
    host_rt.consecutive_probe_failures = 0
    host_rt.first_probe_failure_at = None
    host_rt.last_ok_at = now
    host_rt.next_probe_at = now
    was_disk_full = host_rt.disk_full or host_rt.state == "disk_full"
    host_rt.disk_full = was_disk_full
    if snapshot.disk_free_gb is not None:
        host_rt.disk_full = snapshot.disk_free_gb < host.min_disk_free_gb
        if host_rt.disk_full and not was_disk_full:
            events.append(SchedulerEvent(now, "host_disk_full", host_id=host.id, details={
                "disk_free_gb": snapshot.disk_free_gb,
            }))
    host_rt.state = "disk_full" if host_rt.disk_full else ("cordoned" if host_rt.cordoned else "ready")
    host_rt.gpus = [GpuSlotSnapshot.from_dict(gpu.to_dict()) for gpu in snapshot.gpus]

    for task_id, runtime, attempt in _active_on(state, host.id):
        _reconcile_attempt(
            tasks_by_id[task_id], runtime, attempt, snapshot, host, host_rt, now, preempts, pulls, events,
        )
    managed_gpus = {gpu for _, _, attempt in _active_on(state, host.id) for gpu in attempt.gpu_ids}
    _reconcile_external(state, host, host_rt, tasks_by_id, managed_gpus, now, preempts, events)
    events.extend(advance_dag_states(state, tasks_by_id, now))
    return preempts, pulls, events


def compute_free_gpus(
    host_cfg: HostConfig, host_rt: HostRuntime, leased_gpus: set[int], now: float,
) -> list[int]:
    if host_cfg.accelerator == "cpu":
        return []
    excluded = leased_gpus.union(host_rt.cordoned_gpus)
    allowed = None if host_cfg.gpu_ids is None else set(host_cfg.gpu_ids)
    free = set()
    for gpu in host_rt.gpus:
        if gpu.index in excluded or (allowed is not None and gpu.index not in allowed):
            continue
        if host_rt.gpu_cooldown_until.get(str(gpu.index), now) > now:
            continue
        if host_cfg.external_gpu_policy != "ignore" and (
            gpu.external_pids or _external_pressure(gpu, host_cfg)
            or gpu.memory_total_mb - gpu.memory_used_mb < host_cfg.min_free_mem_mb
        ):
            continue
        free.add(gpu.index)
    return sorted(free)


def _downstream_counts(tasks_by_id: dict[str, TaskSpec]) -> dict[str, int]:
    descendants: dict[str, set[str]] = {task_id: set() for task_id in tasks_by_id}
    direct_dependents = Counter(dep for task in tasks_by_id.values() for dep in set(task.depends_on))
    queue = deque(task_id for task_id in tasks_by_id if not direct_dependents[task_id])
    while queue:
        task_id = queue.popleft()
        for dep in set(tasks_by_id[task_id].depends_on):
            descendants[dep].add(task_id)
            descendants[dep].update(descendants[task_id])
            direct_dependents[dep] -= 1
            if not direct_dependents[dep]:
                queue.append(dep)
    return {task_id: len(children) for task_id, children in descendants.items()}


def _primary_dep_host(state: RunState, task: TaskSpec) -> str | None:
    """Choose the host with the most completed dependencies, breaking ties by host ID."""
    hosts = Counter(
        state.tasks[dep].completed_host for dep in dict.fromkeys(task.depends_on)
        if state.tasks[dep].state == "completed" and state.tasks[dep].completed_host is not None
    )
    return min(hosts, key=lambda host: (-hosts[host], host), default=None)


def _new_assignment(task: TaskSpec, runtime: TaskRuntime, host: HostConfig, gpu_ids: list[int],
                    run_id: str) -> DispatchAssignment:
    next_attempt = len(runtime.attempts) + 1
    root = f"{host.work_root.rstrip('/')}/{run_id}"
    return DispatchAssignment(
        task.id, host.id, gpu_ids, next_attempt, f"{root}/outputs/{task.id}",
        f"{root}/tasks/{task.id}/attempt-{next_attempt:03d}",
    )


def plan_dispatches(
    state: RunState, cluster: ClusterConfig, tasks_by_id: dict[str, TaskSpec],
    run_id: str, now: float,
) -> tuple[list[DispatchAssignment], list[SchedulerEvent]]:
    """Propose assignments for one tick. The caller records each launch outcome."""
    if not isinstance(run_id, str) or not VALID_ID_RE.fullmatch(run_id):
        raise ValidationError(f"Invalid run ID: {run_id!r}")
    events = advance_dag_states(state, tasks_by_id, now)
    downstream = _downstream_counts(tasks_by_id)
    ready = sorted(
        (tasks_by_id[task_id] for task_id, rt in state.tasks.items()
         if rt.state == "ready" and rt.next_eligible_at <= now),
        key=lambda task: (
            -task.priority, -downstream[task.id], -task.gpus, -task.est_duration_s,
            task.manifest_index, task.id,
        ),
    )
    leases: dict[str, set[int]] = {host_id: set() for host_id in cluster.hosts}
    cpu_tasks: Counter[str] = Counter()
    for runtime in state.tasks.values():
        attempt = runtime.active_attempt
        if attempt is not None:
            if attempt.gpu_ids:
                leases[attempt.host_id].update(attempt.gpu_ids)
            else:
                cpu_tasks[attempt.host_id] += 1

    assignments = []
    for task in ready:
        runtime = state.tasks[task.id]
        dep_host = _primary_dep_host(state, task)
        affinity_key = task.host_affinity.partition(":")[2]
        candidates = []
        for host_id, host in cluster.hosts.items():
            host_rt = state.hosts[host_id]
            if (
                host_rt.state != "ready" or host_rt.cordoned or not host_rt.preflight_ok
                or host_rt.draining_for not in (None, task.id)
                or not host_matches_task_static(host, task)[0]
                or (task.host_affinity == "require_dep_host" and host_id != dep_host)
            ):
                continue
            free_gpus = []
            if task.gpus:
                free_gpus = compute_free_gpus(host, host_rt, leases[host_id], now)
                if len(free_gpus) < task.gpus:
                    continue
            elif cpu_tasks[host_id] >= host.max_concurrent_cpu_tasks:
                continue

            cooling = runtime.failed_infra_hosts.get(host_id, now) > now
            score = 10.0 * host.speed_weight - (500.0 if cooling else 0.0)
            if task.host_affinity == "prefer_dep_host" and host_id == dep_host:
                score += 100.0
            if affinity_key and affinity_key in host_rt.recent_affinity_keys:
                score += 50.0
            candidates.append((cooling, -score, host_id, free_gpus))
        if not candidates:
            continue
        _, _, host_id, free_gpus = min(candidates)
        gpu_ids = free_gpus[:task.gpus]
        if gpu_ids:
            leases[host_id].update(gpu_ids)
        else:
            cpu_tasks[host_id] += 1
        assignments.append(_new_assignment(task, runtime, cluster.hosts[host_id], gpu_ids, run_id))
    return assignments, events


def _record_attempt(state: RunState, assignment: DispatchAssignment, now: float) -> TaskAttempt:
    runtime = state.tasks[assignment.task_id]
    if (
        runtime.state != "ready" or runtime.next_eligible_at > now
        or assignment.attempt != len(runtime.attempts) + 1
    ):
        raise ValidationError(f"Stale dispatch assignment for task {assignment.task_id!r}")
    attempt = TaskAttempt(
        assignment.attempt, assignment.host_id, list(assignment.gpu_ids),
        assignment.out_dir, assignment.attempt_dir, now,
    )
    runtime.attempts.append(attempt)
    return attempt


def mark_dispatch_started(
    state: RunState, assignment: DispatchAssignment, now: float, *, pgid: int | None = None,
) -> SchedulerEvent:
    attempt = _record_attempt(state, assignment, now)
    attempt.pgid = pgid
    state.tasks[assignment.task_id].state = "running"
    return SchedulerEvent(
        now, "task_started", assignment.task_id, assignment.host_id,
        {"attempt": assignment.attempt, "gpu_ids": list(assignment.gpu_ids), "pgid": pgid},
    )


def mark_dispatch_launch_failed(
    state: RunState, assignment: DispatchAssignment, task: TaskSpec, now: float, *,
    reason: str = "launch_failed",
) -> SchedulerEvent:
    if task.id != assignment.task_id:
        raise ValidationError(f"Launch failure assignment is for a different task: {assignment.task_id!r}")
    attempt = _record_attempt(state, assignment, now)
    return _finish_failure(
        task, state.tasks[task.id], attempt, kind="infra_error", reason=reason, now=now,
        retryable=not _non_retryable(task, None, reason),
    )


def build_summary(state: RunState) -> dict[str, Any]:
    counts = Counter(runtime.state for runtime in state.tasks.values())
    return {
        "run_id": state.run_id,
        "created_at": state.created_at,
        "terminal": is_run_terminal(state),
        "task_counts": dict(sorted(counts.items())),
        "tasks": {task_id: runtime.to_dict() for task_id, runtime in sorted(state.tasks.items())},
        "hosts": {host_id: runtime.to_dict() for host_id, runtime in sorted(state.hosts.items())},
    }


def is_run_terminal(state: RunState) -> bool:
    return all(runtime.state in TERMINAL_TASK_STATES for runtime in state.tasks.values())
