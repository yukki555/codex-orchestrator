"""Read transcript context usage and persist per-conversation handoff state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from .env import ensure_private_dir

T = TypeVar("T")
_SUBAGENT_BOOTSTRAP_RE = re.compile(r"\[Message\]\s+timestamp=\S+\s+sender=([A-Za-z0-9_.-]+)\b")


@dataclass(frozen=True)
class TranscriptTelemetry:
    transcript_path: str
    exists: bool
    active_tokens: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    active_model: str
    recent_models: tuple[str, ...]
    checkpoint_count: int
    checkpoint_epoch: int
    last_step_index: int | None
    last_planner_step_index: int | None
    malformed_lines: int
    remote_cmd_count: int = 0
    remote_turn_streak: int = 0
    read_file_count: int = 0
    invoked_subagents: tuple[str, ...] = ()
    is_subagent: bool = False


@dataclass(frozen=True)
class ContextAssessment:
    active_tokens: int
    context_limit: int
    utilization_ratio: float
    soft_threshold: int
    hard_threshold: int
    emergency_threshold: int
    band: str
    checkpoint_count: int
    checkpoint_epoch: int
    checkpoint_fatigue: bool
    headroom_to_auto_compact: int
    active_model: str
    recent_models: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_transcript_row(line: bytes) -> dict | None:
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return row if isinstance(row, dict) else None


def _planner_tokens(row: dict) -> tuple[int, int, int]:
    usage = row.get("usage", row.get("token_usage", row.get("tokenUsage", {})))
    if not isinstance(usage, dict):
        usage = {}
    tokens = []
    for key, camel, short in (
        ("input_tokens", "inputTokens", "input"),
        ("cache_read_tokens", "cacheReadTokens", "cache_read"),
        ("output_tokens", "outputTokens", "output"),
    ):
        value = row.get(key, row.get(camel, usage.get(key, usage.get(camel, usage.get(short)))))
        tokens.append(value if type(value) is int and value >= 0 else 0)
    return tuple(tokens)


def _unquote_tool_arg(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, str):
            return parsed
    return value


_SHELL_SEPARATORS = ";&|\n"
_SHELL_PREFIXES = frozenset(("if", "then", "elif", "else", "do", "time", "!",
                             "command", "builtin", "exec", "nohup"))
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPER_VALUE_OPTIONS = {
    "env": frozenset(("-u", "-C", "-S")),
    "sudo": frozenset(("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U")),
    "timeout": frozenset(("-k", "-s", "--kill-after", "--signal")),
    "nice": frozenset(("-n",)),
    "stdbuf": frozenset(("-i", "-o", "-e")),
}
_CONTAINER_VALUE_OPTIONS = {
    "kubectl": frozenset((
        "-n", "--namespace", "--context", "--cluster", "--user", "--kubeconfig", "--server", "-s",
        "--token", "--as", "--as-group", "--as-uid", "--certificate-authority",
        "--client-certificate", "--client-key", "--request-timeout", "-v", "--v", "--cache-dir",
        "-o", "--output",
    )),
    "docker": frozenset((
        "-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert",
        "--tlscert", "--tlskey",
    )),
}


def _shell_segments(cmd: str) -> Iterator[list[str]]:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=_SHELL_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        for segment in re.split(r"[;&|\n]+", cmd):
            yield segment.split()
        return

    segment = []
    for token in tokens:
        if token and all(char in _SHELL_SEPARATORS for char in token):
            if segment:
                yield segment
                segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def _skip_options(
    words: list[str], index: int, value_options: frozenset[str], *, assignments: bool = False
) -> int:
    while index < len(words):
        word = words[index]
        if assignments and _ENV_ASSIGNMENT.match(word):
            index += 1
        elif word.startswith("-") and word not in ("-", "--"):
            index += 2 if word in value_options else 1
        else:
            break
    return index


def _is_remote_segment(words: list[str]) -> bool:
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word in _SHELL_PREFIXES or _ENV_ASSIGNMENT.match(word):
            continue

        head = os.path.basename(word)
        if head in _WRAPPER_VALUE_OPTIONS:
            index = _skip_options(words, index, _WRAPPER_VALUE_OPTIONS[head],
                                  assignments=head in ("env", "sudo"))
            if words[index:index + 1] == ["--"]:
                index += 1
            if head == "timeout":
                index += 1  # Duration
            continue

        # Orchestrator helpers and shells running scripts are outside this command allowlist.
        if head in ("ssh", "scp", "rsync", "sbatch", "squeue"):
            return True
        if head in _CONTAINER_VALUE_OPTIONS:
            index = _skip_options(words, index, _CONTAINER_VALUE_OPTIONS[head])
            return index < len(words) and words[index] == "exec"
        return False
    return False


def _is_remote_tool_call(name: str, args: Mapping[str, object]) -> bool:
    if name == "run_command":
        cmd = _unquote_tool_arg(args.get("CommandLine", args.get("command", "")))
        return any(_is_remote_segment(segment) for segment in _shell_segments(cmd))
    if name == "manage_task":
        action = _unquote_tool_arg(args.get("Action", args.get("action", ""))).strip().lower()
        return action == "status"
    return False


def _is_counted_read_file(name: str, args: Mapping[str, object]) -> bool:
    if name != "read_file":
        return False
    raw_path = _unquote_tool_arg(args.get("Path", args.get("path", args.get("AbsolutePath", "")))).strip()
    norm_path = raw_path.replace("\\", "/")
    if "/handoff/" in norm_path or norm_path.endswith("/handoff"):
        return False
    return not norm_path.startswith(("/tmp/codex-orchestrator/", "/private/tmp/codex-orchestrator/"))


def _is_subagent_bootstrap_row(row: Mapping[str, object]) -> bool:
    kind = row.get("type", row.get("step_type", row.get("stepType", row.get("event"))))
    index = row.get("step_index", row.get("stepIndex", row.get("index")))
    content = row.get("content")
    return (
        kind == "SYSTEM_MESSAGE"
        and row.get("source") == "SYSTEM"
        and (index is None or (type(index) is int and index == 0))
        and isinstance(content, str)
        and _SUBAGENT_BOOTSTRAP_RE.search(content) is not None
    )


def read_transcript_telemetry(
    transcript_path: str | Path | None, *, tail_bytes: int = 262144
) -> TranscriptTelemetry:
    path = str(transcript_path) if transcript_path is not None else ""
    exists = False
    checkpoints = malformed = 0
    step_index = planner_index = None
    planner = None
    last_model = ""
    recent_models: dict[str, None] = {}
    remote_cmd_count = remote_turn_streak = read_file_count = 0
    invoked_subagents: dict[str, None] = {}
    is_subagent = False
    seen_first_line = False

    try:
        if path:
            with Path(path).open("rb") as stream:
                exists = True
                end = stream.seek(0, os.SEEK_END)
                tail_start = max(0, end - tail_bytes)
                stream.seek(0)
                offset = 0
                # Outside the tail, decode the first non-blank line and possible
                # planner and checkpoint rows.
                while offset < end:
                    line = stream.readline(end - offset)
                    if not line:
                        break
                    in_tail = offset >= tail_start
                    offset += len(line)
                    if not line.strip():
                        continue
                    is_first_line = not seen_first_line
                    seen_first_line = True
                    if not in_tail and not is_first_line and not any(
                        marker in line for marker in (b"CHECKPOINT", b"PLANNER_RESPONSE", b"\\u")
                    ):
                        continue
                    row = _parse_transcript_row(line)
                    if row is None:
                        malformed += in_tail
                        continue
                    if is_first_line:
                        is_subagent = _is_subagent_bootstrap_row(row)
                    kind = row.get("type", row.get("step_type", row.get("stepType", row.get("event"))))
                    if kind == "CHECKPOINT":
                        checkpoints += 1
                        planner = planner_index = None
                        last_model = ""
                        recent_models.clear()
                        remote_cmd_count = remote_turn_streak = read_file_count = 0
                        invoked_subagents.clear()
                    index = row.get("step_index", row.get("stepIndex", row.get("index")))
                    if in_tail and type(index) is int:
                        step_index = index
                    if kind == "PLANNER_RESPONSE":
                        planner = row
                        planner_index = index if type(index) is int else None
                        model = row.get("model")
                        last_model = model if isinstance(model, str) else ""
                        if last_model.strip():
                            recent_models[last_model] = None
                        tool_calls = row.get("tool_calls", row.get("toolCalls"))
                        if not isinstance(tool_calls, list):
                            continue
                        turn_has_tools = turn_has_remote = False
                        for call in tool_calls:
                            if not isinstance(call, dict):
                                continue
                            turn_has_tools = True
                            call_name = call.get("name")
                            args = call.get("args", call.get("arguments", {}))
                            if not isinstance(args, dict):
                                args = {}
                            if _is_remote_tool_call(call_name, args):
                                remote_cmd_count += 1
                                turn_has_remote = True
                            elif _is_counted_read_file(call_name, args):
                                read_file_count += 1
                            elif call_name == "invoke_subagent":
                                type_name = args.get("TypeName", args.get("type_name", args.get("typeName", "")))
                                subagent = _unquote_tool_arg(type_name).strip().lower()
                                if subagent:
                                    invoked_subagents[subagent] = None
                        if turn_has_tools:
                            remote_turn_streak = remote_turn_streak + 1 if turn_has_remote else 0
    except (OSError, ValueError):
        pass

    input_tokens, cache_read_tokens, output_tokens = _planner_tokens(planner) if planner is not None else (0, 0, 0)
    return TranscriptTelemetry(
        transcript_path=path,
        exists=exists,
        active_tokens=input_tokens + cache_read_tokens,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        output_tokens=output_tokens,
        active_model=last_model,
        recent_models=tuple(recent_models),
        checkpoint_count=checkpoints,
        checkpoint_epoch=checkpoints,
        last_step_index=step_index,
        last_planner_step_index=planner_index,
        malformed_lines=malformed,
        remote_cmd_count=remote_cmd_count,
        remote_turn_streak=remote_turn_streak,
        read_file_count=read_file_count,
        invoked_subagents=tuple(invoked_subagents),
        is_subagent=is_subagent,
    )


def resolve_model_context_limit(models: Iterable[str], cfg: Mapping[str, object]) -> int:
    rules = cfg["context_limit_rules"]
    limits = []
    for model in models:
        if not model.strip():
            continue
        for rule in rules:
            if re.search(rule["pattern"], model, re.IGNORECASE):
                limits.append(int(rule["limit"]))
                break
    return min(limits, default=int(cfg["default_context_limit"]))


def resolve_model_tier_fallback(model_id: str, cfg: Mapping[str, object]) -> str:
    for rule in cfg["model_tier_fallbacks"]:
        if re.search(rule["pattern"], model_id, re.IGNORECASE):
            return rule["tier"]
    return "flash"


def evaluate_context_band(
    telemetry: TranscriptTelemetry, cfg: Mapping[str, object], *, model_hint: str = ""
) -> ContextAssessment:
    models = telemetry.recent_models
    if not models and model_hint and model_hint.lower() != "auto":
        models = (model_hint,)
    limit = resolve_model_context_limit(models, cfg)
    soft = cfg.get("soft_tokens_override") or min(int(cfg["soft_cap_tokens"]), int(limit * float(cfg["soft_ratio"])))
    hard = cfg.get("hard_tokens_override") or min(int(cfg["hard_cap_tokens"]), int(limit * float(cfg["hard_ratio"])))
    soft = max(1, int(soft))
    hard = max(soft, int(hard))
    emergency = max(hard, int(limit * float(cfg["emergency_ratio"])))
    active = telemetry.active_tokens

    band = "ok"
    for threshold, name in ((emergency, "emergency"), (hard, "hard"), (soft, "soft")):
        if active >= threshold:
            band = name
            break
    return ContextAssessment(
        active_tokens=active,
        context_limit=limit,
        utilization_ratio=active / limit,
        soft_threshold=soft,
        hard_threshold=hard,
        emergency_threshold=emergency,
        band=band,
        checkpoint_count=telemetry.checkpoint_count,
        checkpoint_epoch=telemetry.checkpoint_epoch,
        checkpoint_fatigue=telemetry.checkpoint_count >= int(cfg["checkpoint_fatigue_count"]),
        headroom_to_auto_compact=max(0, int(limit * 0.85) - active),
        active_model=telemetry.active_model,
        recent_models=telemetry.recent_models,
    )


def handoff_state_dir(environ: Mapping[str, str] | None = None, *, create: bool = True) -> Path:
    source = os.environ if environ is None else environ
    plugin_data = source.get("PLUGIN_DATA", "").strip()
    app_data = source.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
    if plugin_data:
        path = Path(plugin_data).expanduser() / "handoff_state"
    elif app_data:
        path = Path(app_data).expanduser() / "plugin_data" / "codex-orchestrator" / "handoff_state"
    else:
        path = Path("/tmp/codex-orchestrator/handoff-state")
    if create:
        return ensure_private_dir(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("Handoff state directory must be owned by the current user and private")
    return path


def state_file_path(
    conversation_id: str, *, environ: Mapping[str, str] | None = None, create: bool = True
) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_id.strip())
    if name in ("", ".", ".."):
        raise ValueError("invalid conversation ID")
    return handoff_state_dir(environ, create=create) / f"{name}.json"


def hash_user_input(text: str) -> str:
    if isinstance(text, str) and text.strip():
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return ""


def chain_position_from_tags(tags: object) -> dict[str, str | int]:
    position = {}
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str):
                continue
            key, _, value = tag.partition(":")
            if key == "chain" and re.fullmatch(r"[A-Za-z0-9_.-]+", value) and value not in (".", ".."):
                position["chain_id"] = value
            elif key == "hop" and re.fullmatch(r"[0-9]{1,18}", value) and int(value) > 0:
                position["hop_index"] = int(value)
            elif key == "handoff-mode" and value in ("auto", "propose"):
                position["chain_handoff_mode"] = value
    return position


@contextmanager
def conversation_state_lock(
    conversation_id: str, *, environ: Mapping[str, str] | None = None
) -> Iterator[None]:
    path = state_file_path(conversation_id, environ=environ)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_conversation_state(
    conversation_id: str, *, environ: Mapping[str, str] | None = None, create: bool = True
) -> dict:
    try:
        path = state_file_path(conversation_id, environ=environ, create=create)
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(state, dict):
        return {}
    for prefix in ("last_seen_user", "handed_off_turn"):
        key = f"{prefix}_input"
        if key in state:
            state.setdefault(f"{prefix}_hash", hash_user_input(state.pop(key)))
            if prefix == "handed_off_turn" and state.get("handed_off") is True:
                state.setdefault("post_handoff_turn_active", True)
    return state


def save_conversation_state(
    conversation_id: str, state: Mapping[str, object], *, environ: Mapping[str, str] | None = None
) -> Path:
    path = state_file_path(conversation_id, environ=environ)
    stream = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(dict(state), stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def update_conversation_state(
    conversation_id: str, updater: Callable[[dict], T], *, environ: Mapping[str, str] | None = None
) -> tuple[dict, T]:
    with conversation_state_lock(conversation_id, environ=environ):
        state = load_conversation_state(conversation_id, environ=environ)
        result = updater(state)
        save_conversation_state(conversation_id, state, environ=environ)
        return state, result
