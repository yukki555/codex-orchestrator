#!/usr/bin/env python3
"""Gemini CLI and Antigravity delegate guard for codex-orchestrator.

Prevents Gemini from bypassing Codex on non-trivial local code writes/edits
while allowing Docs/Configs, /tmp specs, <=10-line micro-fixes, and explicit
user opt-outs. Always fails open on parse or unexpected errors.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

CODE_EXTS = {
    ".py", ".pyi", ".sh", ".bash", ".zsh",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".cu", ".cuh", ".rb", ".php", ".scala",
    ".sql", ".lua", ".r",
}
CODE_BASENAMES = {"Makefile", "Dockerfile"}
MAX_DIRECT_EDIT_LINES = 10
SKIP_CODEX_RE = re.compile(
    r"(?:不用\s*codex|别用\s*codex|不要用\s*codex|跳过\s*codex|不走\s*codex|"
    r"skip\s+codex|without\s+codex|no\s+codex|direct\s+edit|你直接改|你直接写)",
    re.IGNORECASE,
)
SKIP_STATE_DIR = Path("/tmp/codex-orchestrator")
SKIP_TTL_S = 3600


def allow() -> None:
    print(json.dumps({"decision": "allow"}))


def deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))


def is_safe_temp_or_config_path(path_str: str) -> bool:
    norm = os.path.normpath(os.path.expanduser(path_str))
    safe_prefixes = ("/tmp/", "/private/tmp/", "/var/folders/")
    if norm.startswith(safe_prefixes) or path_str.startswith(safe_prefixes):
        return True
    if "/.gemini/" in norm or "/.gemini/" in path_str:
        return True
    return False


def is_code_file(path_str: str) -> bool:
    p = Path(path_str)
    if p.name in CODE_BASENAMES:
        return True
    return p.suffix.lower() in CODE_EXTS


def count_changed_lines(old_text: str, new_text: str) -> int:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    sm = SequenceMatcher(None, old_lines, new_lines)
    return sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", session_id).strip("_")[:80]


def _skip_state_path(session_id: str) -> Path | None:
    safe_id = _safe_session_id(session_id)
    if safe_id:
        return SKIP_STATE_DIR / f".gemini_cli_skip_{safe_id}"
    return None


def _update_skip_state(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    state_path = _skip_state_path(session_id)
    if state_path is None:
        return
    if SKIP_CODEX_RE.search(prompt):
        SKIP_STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path.write_text("1\n", encoding="utf-8")
    else:
        state_path.unlink(missing_ok=True)


def _has_active_skip_state(session_id: str) -> bool:
    state_path = _skip_state_path(session_id)
    if state_path is None:
        return False
    try:
        modified_at = state_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - modified_at <= SKIP_TTL_S


def _gemini_target_path(args: dict) -> str:
    return args.get("file_path") or args.get("path") or args.get("Path") or args.get("TargetFile") or ""


def evaluate_tool_action(tool_name: str, args: dict, source_flavor: str) -> str | None:
    """Return a denial reason when the tool action exceeds the guard limits."""
    gemini_cli = source_flavor == "gemini_cli"
    if gemini_cli:
        target_path = _gemini_target_path(args)
    else:
        target_path = str(
            args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        ).strip()
    if not target_path or is_safe_temp_or_config_path(target_path) or not is_code_file(target_path):
        return None

    spec = "Write a task Spec under /tmp/codex-orchestrator/<task-id>/spec.md"
    if gemini_cli:
        delegate = "delegate to `run_codex.sh`, `run_codex_parallel.sh`, or `@codex-worker`."
    else:
        delegate = "delegate implementation to the `codex-worker` subagent or `run_codex.sh implement`."

    if tool_name in ("write_file", "write_to_file"):
        append = (gemini_cli and args.get("append")) or args.get("Append")
        if append:
            content = str(
                (gemini_cli and args.get("content"))
                or args.get("Content") or args.get("CodeContent") or ""
            )
            line_count = len(content.splitlines())
            if line_count <= MAX_DIRECT_EDIT_LINES:
                return None
            return (
                f"codex-delegate-guard: Appending {line_count} lines to code file "
                f"'{target_path}' exceeds the <= {MAX_DIRECT_EDIT_LINES}-line direct-edit limit. "
                f"{spec} and {delegate}"
            )
        if not gemini_cli:
            delegate = "delegate to the `codex-worker` subagent or `run_codex.sh implement`."
        return (
            f"codex-delegate-guard: Creating or overwriting code file '{target_path}' must be "
            f"delegated to Codex. {spec} and {delegate}"
        )

    if tool_name == "replace" and gemini_cli:
        changed = count_changed_lines(
            str(args.get("old_string") or ""), str(args.get("new_string") or "")
        )
    elif tool_name == "multi_replace_file_content":
        changed = 0
        chunks = args.get("ReplacementChunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                if isinstance(chunk, dict):
                    changed += count_changed_lines(
                        str(chunk.get("TargetContent") or ""),
                        str(chunk.get("ReplacementContent") or ""),
                    )
    elif tool_name in ("edit_file", "replace_file_content"):
        if not gemini_cli:
            old_text = args.get("Target") or args.get("TargetContent")
            new_text = args.get("Replacement") or args.get("ReplacementContent")
        elif tool_name == "edit_file":
            old_text, new_text = args.get("Target"), args.get("Replacement")
        else:
            old_text, new_text = args.get("TargetContent"), args.get("ReplacementContent")
        changed = count_changed_lines(str(old_text or ""), str(new_text or ""))
    else:
        return None

    if changed > MAX_DIRECT_EDIT_LINES:
        return (
            f"codex-delegate-guard: Editing {changed} lines in code file '{target_path}' "
            f"exceeds the <= {MAX_DIRECT_EDIT_LINES}-line direct-edit limit. "
            f"{spec} and {delegate}"
        )
    return None


def _evaluate_gemini_tool(payload: dict) -> str | None:
    session_id = str(payload.get("session_id") or "")
    if _has_active_skip_state(session_id):
        return None

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    args = tool_input if isinstance(tool_input, dict) else {}
    target_path = _gemini_target_path(args)
    cwd = str(payload.get("cwd") or "")
    if target_path and not os.path.isabs(target_path) and cwd:
        args = {**args, "file_path": os.path.normpath(os.path.join(cwd, target_path))}
    return evaluate_tool_action(tool_name, args, "gemini_cli")


def _evaluate_antigravity_tool(payload: dict) -> str | None:
    last_user = str(payload.get("lastUserInput") or "")
    if SKIP_CODEX_RE.search(last_user):
        return None

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return None
    return evaluate_tool_action(tool_name, args, "antigravity")


def main() -> int:
    reason = None
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            event = payload.get("hook_event_name")
            if event == "BeforeAgent":
                _update_skip_state(payload)
            elif "toolCall" in payload:
                reason = _evaluate_antigravity_tool(payload)
            elif event == "BeforeTool" or "tool_name" in payload:
                reason = _evaluate_gemini_tool(payload)

        if reason is not None:
            deny(reason)
        else:
            allow()
    except Exception:
        allow()
    return 0


if __name__ == "__main__":
    sys.exit(main())
