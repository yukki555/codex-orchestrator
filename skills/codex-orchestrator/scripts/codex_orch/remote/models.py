"""Configuration, probe records, and persisted state for remote scheduling."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..schema_check import matches_schema, preflight_schema


class ValidationError(ValueError):
    """A configuration, state, or command template could not be validated."""


VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
VALID_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.:-]+)?$")
SAFE_SSH_OPTION_KEYS = frozenset({
    "IdentityFile", "ProxyJump", "ConnectTimeout", "ServerAliveInterval",
    "ServerAliveCountMax", "StrictHostKeyChecking", "UserKnownHostsFile",
    "Compression", "IPQoS", "ForwardAgent", "IdentitiesOnly", "TCPKeepAlive",
})
DEFAULT_PULL_EXCLUDES = ("*.pt", "*.pth", "*.ckpt", "*.npy", "*.bin", "*.safetensors")
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "blocked_dep", "cancelled"})
_BUILTINS = frozenset({
    "task_id", "run_id", "out_dir", "attempt_dir", "gpu_ids", "num_gpus",
    "host_id", "work_root",
})
_TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z0-9_.-]+)\}")
_REFERENCES = Path(__file__).parent.parent.parent.parent / "references"


def _validate_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not VALID_ID_RE.fullmatch(value):
        raise ValidationError(f"Invalid {label}: {value!r}")


def _validate_path(value: str, label: str, *, absolute: bool = False) -> None:
    if (
        not isinstance(value, str) or not value
        or value.startswith("/") != absolute
        or any(char in value for char in "\0\r\n")
        or ".." in value.split("/")
    ):
        kind = "absolute" if absolute else "relative"
        raise ValidationError(f"{label} must be a clean {kind} POSIX path: {value!r}")


def _ssh_options(options: tuple[str, ...]) -> tuple[str, ...]:
    normalized = []
    entries = iter(options)
    for option in entries:
        if option == "-o":
            option = next(entries, "")
        elif option.startswith("-o "):
            option = option[3:]
        key, separator, value = option.partition("=")
        if not separator or key not in SAFE_SSH_OPTION_KEYS:
            raise ValidationError(f"SSH option {key!r} is not allowed; expected an allowlisted Key=Value")
        if not value.strip() or not value.isprintable() or any(char in value for char in ";|&\x60$()"):
            raise ValidationError(f"SSH option {key!r} has an unsafe value")
        normalized.append(option)
    return tuple(normalized)


def _integer(value: int, label: str, minimum: int, maximum: int | None = None) -> int:
    # Draft-07 accepts JSON numbers like 2.0 for integer-valued fields.
    if not (
        type(value) is int or (type(value) is float and value.is_integer())
    ) or value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValidationError(f"{label} must be an integer in {limit}")
    return int(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class _JsonRecord:
    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        try:
            return cls(**data)
        except (TypeError, AttributeError) as exc:
            raise ValidationError(f"Invalid {cls.__name__}: {exc}") from exc


@dataclass
class HostConfig(_JsonRecord):
    id: str
    ssh_target: str
    work_root: str
    ssh_port: int = 22
    ssh_options: tuple[str, ...] = ()
    max_ssh_concurrency: int = 2
    accelerator: str = "nvidia"
    gpu_ids: tuple[int, ...] | None = None
    external_gpu_policy: str = "skip_gpu"
    external_grace_ticks: int = 3
    yield_preempt_running: bool = False
    min_free_mem_mb: int = 0
    external_mem_threshold_mb: int = 1024
    max_gpu_util_pct: int = 90
    min_disk_free_gb: float = 0
    tags: tuple[str, ...] = ()
    speed_weight: float = 1.0
    mirror_root: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    setup_cmd: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    max_concurrent_cpu_tasks: int = 1
    ppu_probe_cmd: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.id, "host ID")
        if (
            not isinstance(self.ssh_target, str)
            or not VALID_SSH_TARGET_RE.fullmatch(self.ssh_target)
            or self.ssh_target.startswith("-")
        ):
            raise ValidationError(f"Invalid SSH target: {self.ssh_target!r}")
        self.ssh_port = _integer(self.ssh_port, "ssh_port", 1, 65535)
        if isinstance(self.ssh_options, str):
            raise ValidationError("ssh_options must be an array of strings")
        self.ssh_options = _ssh_options(self.ssh_options)
        self.max_ssh_concurrency = _integer(self.max_ssh_concurrency, "max_ssh_concurrency", 1, 8)
        _validate_path(self.work_root, "work_root", absolute=True)
        if self.mirror_root is not None:
            _validate_path(self.mirror_root, "mirror_root", absolute=True)
        if self.accelerator not in ("nvidia", "ppu", "cpu"):
            raise ValidationError(f"Invalid accelerator: {self.accelerator!r}")
        if self.external_gpu_policy not in ("skip_gpu", "preempt_gpu", "yield_host", "ignore"):
            raise ValidationError(f"Invalid external GPU policy: {self.external_gpu_policy!r}")
        if self.gpu_ids is not None:
            self.gpu_ids = tuple(_integer(index, "GPU index", 0) for index in self.gpu_ids)
            if len(set(self.gpu_ids)) != len(self.gpu_ids):
                raise ValidationError(f"Host {self.id!r} has duplicate GPU indices")
        self.external_grace_ticks = _integer(self.external_grace_ticks, "external_grace_ticks", 0)
        self.max_concurrent_cpu_tasks = _integer(
            self.max_concurrent_cpu_tasks, "max_concurrent_cpu_tasks", 0,
        )
        self.tags = tuple(self.tags)
        self.env = dict(self.env)
        self.vars = dict(self.vars)

    @property
    def host_id(self) -> str:
        return self.id


@dataclass
class ClusterConfig(_JsonRecord):
    hosts: dict[str, HostConfig]
    version: str = "1"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterConfig:
        try:
            return cls(**{
                **data,
                "hosts": {
                    host_id: host if isinstance(host, HostConfig) else HostConfig.from_dict({
                        "id": host_id, **host,
                    })
                    for host_id, host in data["hosts"].items()
                },
            })
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValidationError(f"Invalid cluster inventory: {exc}") from exc

    def __post_init__(self) -> None:
        if self.version != "1":
            raise ValidationError(f"Unsupported cluster inventory version: {self.version!r}")
        for host_id, host in self.hosts.items():
            if host_id != host.id:
                raise ValidationError(f"Inventory host key {host_id!r} differs from {host.id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hosts": {
                host_id: {key: value for key, value in host.to_dict().items() if key != "id"}
                for host_id, host in self.hosts.items()
            },
        }


@dataclass
class RetryPolicy(_JsonRecord):
    max_retries: int = 0
    max_infra_retries: int = 2
    max_preempt_retries: int = 3
    backoff_s: int = 30
    retry_on_host_lost: bool = False
    non_retryable_exit_codes: tuple[int, ...] = ()
    non_retryable_log_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("max_retries", "max_infra_retries", "max_preempt_retries", "backoff_s"):
            setattr(self, name, _integer(getattr(self, name), name, 0))
        self.non_retryable_exit_codes = tuple(self.non_retryable_exit_codes)
        self.non_retryable_log_patterns = tuple(self.non_retryable_log_patterns)
        for pattern in self.non_retryable_log_patterns:
            try:
                re.compile(pattern)
            except (re.error, OverflowError, RecursionError, TypeError) as exc:
                raise ValidationError(f"Invalid non-retryable log pattern {pattern!r}: {exc}") from exc


@dataclass
class RelayInput(_JsonRecord):
    from_task: str
    rel_path: str
    max_bytes: int | None = None
    target_rel_path: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.from_task, "relay source task ID")
        _validate_path(self.rel_path, "relay rel_path")
        if self.target_rel_path is not None:
            _validate_path(self.target_rel_path, "relay target_rel_path")

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.max_bytes is None:
            del data["max_bytes"]
        return data


@dataclass
class SyncConfig(_JsonRecord):
    local_dir: str
    remote_var: str
    excludes: tuple[str, ...] = ()
    mode: str = "tar_ssh"

    def __post_init__(self) -> None:
        self.excludes = tuple(self.excludes)


@dataclass
class PreflightConfig(_JsonRecord):
    cmd: str
    timeout_s: int = 60


@dataclass
class TaskSpec(_JsonRecord):
    id: str
    cmd: str
    name: str | None = None
    priority: int = 0
    est_duration_s: int = 0
    gpus: int = 1
    requires_tags: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] | None = None
    excluded_hosts: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    allow_partial_deps: bool = False
    host_affinity: str = "auto"
    relay_inputs: tuple[RelayInput, ...] = ()
    done_markers: tuple[str, ...] = ()
    pull_to_local: bool = False
    pull_excludes: tuple[str, ...] = DEFAULT_PULL_EXCLUDES
    pull_max_mb: int = 1024
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_s: int | None = None
    stall_timeout_s: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    manifest_index: int = 0

    def __post_init__(self) -> None:
        _validate_id(self.id, "task ID")
        self.gpus = _integer(self.gpus, "gpus", 0)
        for name in ("requires_tags", "excluded_hosts", "depends_on", "done_markers", "pull_excludes"):
            setattr(self, name, tuple(getattr(self, name)))
        if self.allowed_hosts is not None:
            self.allowed_hosts = tuple(self.allowed_hosts)
        for dep in self.depends_on:
            _validate_id(dep, "dependency task ID")
        for host_id in (*self.excluded_hosts, *(self.allowed_hosts or ())):
            _validate_id(host_id, "task host ID")
        for marker in self.done_markers:
            _validate_path(marker, "done marker")
        self.env = dict(self.env)
        self.relay_inputs = tuple(
            relay if isinstance(relay, RelayInput) else RelayInput.from_dict(relay)
            for relay in self.relay_inputs
        )
        if not isinstance(self.retry_policy, RetryPolicy):
            self.retry_policy = RetryPolicy.from_dict(self.retry_policy)
        if self.host_affinity == "auto":
            self.host_affinity = "prefer_dep_host" if self.depends_on else "none"
        elif self.host_affinity.startswith("prefer_key:"):
            _validate_id(self.host_affinity[len("prefer_key:"):], "affinity key")
        elif self.host_affinity not in ("none", "prefer_dep_host", "require_dep_host"):
            raise ValidationError(f"Invalid host affinity: {self.host_affinity!r}")

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.name is None:
            del data["name"]
        data["relay_inputs"] = [relay.to_dict() for relay in self.relay_inputs]
        return data


def _merged_task(defaults: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults, **task}
    for name in ("env", "retry_policy"):
        if name in defaults and name in task:
            merged[name] = {**defaults[name], **task[name]}
    return merged


def _validate_dag(tasks: tuple[TaskSpec, ...]) -> None:
    if not tasks:
        raise ValidationError("A dispatch manifest must contain at least one task")
    successors: dict[str, list[str]] = {task.id: [] for task in tasks}
    if len(successors) != len(tasks):
        raise ValidationError("Duplicate task IDs in dispatch manifest")
    indegrees = {}
    for task in tasks:
        deps = dict.fromkeys(task.depends_on)
        indegrees[task.id] = len(deps)
        for dep in deps:
            if dep not in successors:
                raise ValidationError(f"Task {task.id!r} depends on unknown task {dep!r}")
            successors[dep].append(task.id)
        for relay in task.relay_inputs:
            if relay.from_task not in deps:
                raise ValidationError(f"Relay source {relay.from_task!r} is not a dependency of {task.id!r}")
    ready = deque(task_id for task_id, count in indegrees.items() if count == 0)
    while ready:
        for child in successors[ready.popleft()]:
            indegrees[child] -= 1
            if indegrees[child] == 0:
                ready.append(child)
    unresolved = sorted(task_id for task_id, count in indegrees.items() if count)
    if unresolved:
        raise ValidationError(f"Dependency cycle prevents scheduling tasks: {', '.join(unresolved)}")


@dataclass
class DispatchManifest(_JsonRecord):
    tasks: tuple[TaskSpec, ...]
    version: str = "1"
    run_id: str | None = None
    sync: SyncConfig | None = None
    preflight: PreflightConfig | None = None

    def __post_init__(self) -> None:
        self.tasks = tuple(
            task if task.manifest_index == index else replace(task, manifest_index=index)
            for index, task in enumerate(self.tasks)
        )
        if self.version != "1":
            raise ValidationError(f"Unsupported dispatch manifest version: {self.version!r}")
        if self.run_id is not None:
            _validate_id(self.run_id, "run ID")
        _validate_dag(self.tasks)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DispatchManifest:
        try:
            fields = dict(data)
            defaults = fields.pop("defaults", {})
            fields["tasks"] = tuple(
                TaskSpec.from_dict(_merged_task(defaults, task)) for task in fields["tasks"]
            )
            for key, model in (("sync", SyncConfig), ("preflight", PreflightConfig)):
                if fields.get(key) is not None:
                    fields[key] = model.from_dict(fields[key])
            return cls(**fields)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValidationError(f"Invalid dispatch manifest: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        data = {"version": self.version, "tasks": []}
        for task in self.tasks:
            task_data = task.to_dict()
            del task_data["manifest_index"]
            data["tasks"].append(task_data)
        for name in ("run_id", "sync", "preflight"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value if name == "run_id" else value.to_dict()
        return data


@dataclass
class GpuSlotSnapshot(_JsonRecord):
    index: int
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    utilization_pct: int = 0
    external_pids: list[int] = field(default_factory=list)
    external_busy: bool = False


@dataclass
class AttemptProbeStatus(_JsonRecord):
    pgid_alive: bool = False
    done_payload: dict[str, Any] | None = None
    done_markers_present: bool = False
    log_tail: str = ""
    attempt: int | None = None


@dataclass
class HostProbeSnapshot(_JsonRecord):
    """A host observation with attempt statuses keyed by task ID."""

    host_id: str
    ok: bool
    disk_free_gb: float | None = None
    gpus: list[GpuSlotSnapshot] = field(default_factory=list)
    attempts: dict[str, AttemptProbeStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.gpus = _gpu_snapshots(self.gpus)
        self.attempts = {
            task_id: status if isinstance(status, AttemptProbeStatus) else AttemptProbeStatus.from_dict(status)
            for task_id, status in self.attempts.items()
        }


def _gpu_snapshots(gpus: list[GpuSlotSnapshot]) -> list[GpuSlotSnapshot]:
    return [gpu if isinstance(gpu, GpuSlotSnapshot) else GpuSlotSnapshot.from_dict(gpu) for gpu in gpus]


@dataclass
class TaskAttempt(_JsonRecord):
    attempt: int
    host_id: str
    gpu_ids: list[int]
    out_dir: str
    attempt_dir: str
    started_at: float
    pgid: int | None = None
    state: str = "running"
    finished_at: float | None = None
    exit_code: int | None = None
    failure_kind: str | None = None


@dataclass
class TaskRuntime(_JsonRecord):
    state: str = "pending"
    attempts: list[TaskAttempt] = field(default_factory=list)
    next_eligible_at: float = 0
    retries_used: int = 0
    infra_retries_used: int = 0
    preempt_retries_used: int = 0
    failed_infra_hosts: dict[str, float] = field(default_factory=dict)
    completed_host: str | None = None
    completed_out_dir: str | None = None

    def __post_init__(self) -> None:
        self.attempts = [
            attempt if isinstance(attempt, TaskAttempt) else TaskAttempt.from_dict(attempt)
            for attempt in self.attempts
        ]

    @property
    def active_attempt(self) -> TaskAttempt | None:
        return self.attempts[-1] if self.state == "running" and self.attempts else None


@dataclass
class HostRuntime(_JsonRecord):
    state: str = "ready"
    preflight_ok: bool = True
    cordoned: bool = False
    cordon_reason: str | None = None
    draining_for: str | None = None
    cordoned_gpus: list[int] = field(default_factory=list)
    gpu_cooldown_until: dict[str, float] = field(default_factory=dict)
    external_gpu_ticks: dict[str, int] = field(default_factory=dict)
    external_host_ticks: int = 0
    recent_affinity_keys: list[str] = field(default_factory=list)
    consecutive_probe_failures: int = 0
    last_ok_at: float | None = None
    first_probe_failure_at: float | None = None
    next_probe_at: float = 0
    disk_full: bool = False
    gpus: list[GpuSlotSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.gpus = _gpu_snapshots(self.gpus)
        self.cordoned_gpus = sorted(set(self.cordoned_gpus))
        self.gpu_cooldown_until = {str(index): until for index, until in self.gpu_cooldown_until.items()}
        self.external_gpu_ticks = {str(index): ticks for index, ticks in self.external_gpu_ticks.items()}


@dataclass
class RunState(_JsonRecord):
    run_id: str
    cluster_path: str
    manifest_path: str
    created_at: float
    tasks: dict[str, TaskRuntime] = field(default_factory=dict)
    hosts: dict[str, HostRuntime] = field(default_factory=dict)
    host_lost_timeout_s: float = 600

    def __post_init__(self) -> None:
        _validate_id(self.run_id, "run ID")
        self.tasks = {
            key: value if isinstance(value, TaskRuntime) else TaskRuntime.from_dict(value)
            for key, value in self.tasks.items()
        }
        self.hosts = {
            key: value if isinstance(value, HostRuntime) else HostRuntime.from_dict(value)
            for key, value in self.hosts.items()
        }


def _finite_json_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValidationError(f"Invalid JSON number: {text}")
    return value


def _reject_constant(token: str) -> None:
    raise ValidationError(f"Invalid JSON constant: {token}")


def _read_validated(
    path: str | Path, schema_path: Path, *, merge_defaults: bool = False,
) -> dict[str, Any]:
    schema, error = preflight_schema(schema_path)
    if error:
        raise ValidationError(error)
    try:
        data = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_json_float,
        )
        if not isinstance(data, dict) or not matches_schema(data, schema):
            raise ValidationError(f"{path} does not match JSON Schema {schema_path.name}")
        if merge_defaults and data.get("defaults"):
            effective = {**data, "tasks": [_merged_task(data["defaults"], task) for task in data["tasks"]]}
            if not matches_schema(effective, schema):
                raise ValidationError(f"Merged tasks do not match JSON Schema {schema_path.name}")
            data = effective
            del data["defaults"]
    except (OSError, ValueError, RecursionError) as exc:
        raise ValidationError(f"Invalid configuration {path}: {exc}") from exc
    return data


def load_cluster_config(path: str | Path, schema_path: Path | None = None) -> ClusterConfig:
    schema_path = Path(schema_path) if schema_path is not None else _REFERENCES / "cluster-inventory-schema.json"
    return ClusterConfig.from_dict(_read_validated(path, schema_path))


def load_dispatch_manifest(path: str | Path, schema_path: Path | None = None) -> DispatchManifest:
    schema_path = Path(schema_path) if schema_path is not None else _REFERENCES / "dispatch-manifest-schema.json"
    return DispatchManifest.from_dict(_read_validated(path, schema_path, merge_defaults=True))


def host_matches_task_static(host: HostConfig, task: TaskSpec) -> tuple[bool, str | None]:
    if not set(task.requires_tags).issubset(host.tags):
        return False, "Host lacks required tags"
    if task.allowed_hosts is not None and host.id not in task.allowed_hosts:
        return False, "Host is outside allowed_hosts"
    if host.id in task.excluded_hosts:
        return False, "Host is in excluded_hosts"
    if task.gpus and (host.accelerator == "cpu" or
                      (host.gpu_ids is not None and len(host.gpu_ids) < task.gpus)):
        return False, "Host lacks the requested accelerator capacity"
    return True, None


def extract_template_placeholders(template: str) -> list[str]:
    return [match.group(1) for match in _TEMPLATE_TOKEN.finditer(template) if match.group(1) is not None]


def validate_manifest_against_cluster(manifest: DispatchManifest, cluster: ClusterConfig) -> list[str]:
    for task in manifest.tasks:
        hosts = [host for host in cluster.hosts.values() if host_matches_task_static(host, task)[0]]
        if not hosts:
            raise ValidationError(f"Task {task.id!r} has no statically eligible host")
        dep_keys = {f"dep.{dep}.out_dir" for dep in task.depends_on}
        for key in sorted(set(extract_template_placeholders(task.cmd)) - _BUILTINS - dep_keys):
            if key.startswith("dep.") and key.endswith(".out_dir"):
                raise ValidationError(f"Task {task.id!r} references an undeclared dependency: {key!r}")
            missing = sorted(host.id for host in hosts if key not in host.vars)
            if missing:
                raise ValidationError(f"Task {task.id!r} placeholder {key!r} is missing on hosts: {', '.join(missing)}")
    return []


def render_task_command(
    task: TaskSpec, host: HostConfig, *, run_id: str, out_dir: str, attempt_dir: str,
    gpu_ids: list[int], dep_out_dirs: dict[str, str],
) -> str:
    """Substitute literal values into a trusted shell template with caller-provided quoting."""
    values = {
        **host.vars,
        "task_id": task.id,
        "run_id": run_id,
        "out_dir": out_dir,
        "attempt_dir": attempt_dir,
        "gpu_ids": ",".join(str(gpu) for gpu in gpu_ids),
        "num_gpus": str(len(gpu_ids)),
        "host_id": host.id,
        "work_root": host.work_root,
    }

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key is None:
            return match.group()[0]
        if key.startswith("dep.") and key.endswith(".out_dir"):
            dep = key[4:-8]
            if dep in task.depends_on and dep in dep_out_dirs:
                return dep_out_dirs[dep]
        elif key in values:
            return values[key]
        raise ValidationError(f"Task {task.id!r} has unresolved placeholder {key!r}")

    return _TEMPLATE_TOKEN.sub(substitute, task.cmd)
