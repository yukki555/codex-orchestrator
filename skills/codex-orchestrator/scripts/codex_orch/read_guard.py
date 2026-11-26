"""Keep large whole-file tool results out of the main conversation."""

import json
import os
import re
import sys

from .context_telemetry import (
    _is_subagent_bootstrap_row,
    _parse_transcript_row,
    _payload_marks_child,
    _read_line_arg,
    _read_path,
)

SKIP_READ_GUARD_RE = re.compile(
    r"全文读取|完整读取|直接读全文|跳过读文件护栏|read\s+full|skip\s+read\s+guard|"
    r"no\s+read\s+guard|allow\s+full\s+read",
    re.IGNORECASE,
)
_BINARY_EXTENSIONS = frozenset("""
    .png .jpg .jpeg .gif .webp .bmp .ico .svg .pdf
    .mp3 .wav .m4a .ogg .flac .mp4 .mov .webm .mkv .avi
    .zip .tar .gz .tgz .bz2 .xz .7z .pt .pth .ckpt .safetensors
    .npz .npy .pkl .parquet .so .dylib .dll .exe .wasm .bin
""".split())
_DISABLED_VALUES = {"0", "off", "false", "no"}
_LINE_COUNT_MAX_BYTES = 2 * 1024 * 1024


def _positive_env_int(environ, name, default):
    try:
        value = int(environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _is_child(payload):
    cid = payload.get("conversationId") or payload.get("conversation_id") or ""
    if _payload_marks_child(payload, cid):
        return True
    transcript = payload.get("transcriptPath") or payload.get("transcript_path")
    if isinstance(transcript, str) and os.path.isfile(transcript):
        with open(transcript, "rb") as stream:
            for line in stream:
                if line.strip():
                    return _is_subagent_bootstrap_row(_parse_transcript_row(line) or {})
    return False


def evaluate_read_guard(payload, *, environ=None):
    allow = {"decision": "allow"}
    source = os.environ if environ is None else environ
    try:
        if source.get("CODEX_READ_GUARD", "").strip().lower() in _DISABLED_VALUES:
            return allow
        if not isinstance(payload, dict):
            return allow
        call = payload.get("toolCall")
        if isinstance(call, dict):
            name, args = call.get("name"), call.get("args")
        else:
            name, args = payload.get("tool_name"), payload.get("tool_input")
        if not isinstance(name, str) or name.lower() not in {"read_file", "view_file"}:
            return allow
        if not isinstance(args, dict):
            return allow

        user_input = payload.get("lastUserInput") or payload.get("last_user_input")
        if isinstance(user_input, str) and SKIP_READ_GUARD_RE.search(user_input):
            return allow
        if _is_child(payload):
            return allow
        path = _read_path(args)
        if "/handoff/" in path or path.endswith("/handoff"):
            return allow
        if os.path.splitext(path)[1].lower() in _BINARY_EXTENSIONS:
            return allow
        if (_read_line_arg(args, "ByteOffset", "byte_offset") or 0) > 0:
            return allow

        max_span = _positive_env_int(source, "CODEX_READ_GUARD_MAX_SPAN_LINES", 450)
        start = _read_line_arg(args, "StartLine", "start_line", "offset")
        start = 1 if start is None else start
        end = _read_line_arg(args, "EndLine", "end_line")
        limit = _read_line_arg(args, "limit")
        if 0 < (limit or 0) <= max_span or (end is not None and 0 < end - start + 1 <= max_span):
            return allow

        max_bytes = _positive_env_int(source, "CODEX_READ_GUARD_MAX_BYTES", 20480)
        if not os.path.isfile(path):
            return allow
        size = os.path.getsize(path)
        if size <= max_bytes:
            return allow

        line_count = ""
        with open(path, "rb") as stream:
            if size <= _LINE_COUNT_MAX_BYTES:
                try:
                    line_count = f"，{sum(1 for _ in stream):,} 行"
                except OSError:
                    pass
        reason = (
            f"`{os.path.basename(path)}` 大小为 {size / 1024:.1f} KB{line_count}。"
            f"请用 StartLine/EndLine 限定范围（单次 <= {max_span} 行），"
            "或用 rg 或简短的 python3 命令提取所需片段或统计摘要。"
            '若需系统理解多个文件或大模块，请直接委派内置 research 子代理（invoke_subagent(TypeName="research")）。'
            "确需全文时，可设置 CODEX_READ_GUARD=off，或由用户明确要求“全文读取”。"
        )
        return {"decision": "deny", "reason": reason}
    except Exception:
        return allow


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = None
    print(json.dumps(evaluate_read_guard(payload)))
    return 0
