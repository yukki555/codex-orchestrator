"""Adapt host tool payloads to the Codex delegation policy."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

HOST_ANTIGRAVITY = "antigravity"
HOST_GEMINI_CLI = "gemini-cli"

CODE_EXTS = {
    ".py", ".pyi", ".sh", ".bash", ".zsh",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".cu", ".cuh", ".rb", ".php", ".scala",
    ".sql", ".lua", ".r",
}
CODE_BASENAMES = {"Makefile", "Dockerfile"}
STRICT_EDIT_LIMIT = 10
PRAGMATIC_EDIT_LIMIT = 30
DELEGATION_COMMAND_RE = re.compile(r"(?:^|\s)/(?:impl|dag)(?:\s|$)")
DELEGATION_GUIDANCE = (
    "Write a task Spec under /tmp/codex-orchestrator/<task-id>/spec.md and use "
    "1-hop `run_codex.sh implement` first. Use `codex-worker` for multi-round repair loops "
    "or remote SSH/GPU checks."
)
SKIP_CODEX_RE = re.compile(
    r"(?:不用\s*codex|别用\s*codex|不要用\s*codex|跳过\s*codex|不走\s*codex|"
    r"skip\s+codex|without\s+codex|no\s+codex|direct\s+edit|你直接改|你直接写)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HostAdapter:
    host: str = HOST_ANTIGRAVITY

    def tool_call(self, payload: dict) -> object:
        if self.host != HOST_GEMINI_CLI or "toolCall" in payload:
            return payload.get("toolCall")
        name = payload.get("tool_name")
        args = payload.get("tool_input")
        if not isinstance(args, dict):
            return None
        return {
            "name": "edit_file" if name == "replace" else name,
            "args": {
                "Path": args.get("file_path"),
                "Content": args.get("content"),
                "Target": args.get("old_string"),
                "Replacement": args.get("new_string"),
            },
        }


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason + DELEGATION_GUIDANCE}


def is_safe_temp_or_config_path(path_str: str) -> bool:
    # realpath resolves symlinks in the existing prefix, even for a new file.
    path = Path(os.path.realpath(os.path.expanduser(path_str)))
    gemini_dir = Path.home() / ".gemini"
    brain_roots = [gemini_dir / host / "brain" for host in (
        "antigravity", "antigravity-cli", "antigravity-ide",
    )]
    app_data_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if app_data_dir:
        brain_roots.append(Path(app_data_dir).expanduser() / "brain")
    safe_roots = (
        Path(os.path.realpath("/tmp")) / "codex-orchestrator",
        Path("/tmp/codex-orchestrator"),
        Path("/private/tmp/codex-orchestrator"),
        *(Path(os.path.realpath(root)) for root in brain_roots),
    )
    return any(path == root or root in path.parents for root in safe_roots)


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


def evaluate_guard_payload(
    payload: object,
    *,
    environ: Mapping[str, str] | None = None,
    host: str = HOST_ANTIGRAVITY,
) -> dict[str, str]:
    try:
        if not isinstance(payload, dict):
            return {"decision": "allow"}

        last_user = str(payload.get("lastUserInput") or "")
        if SKIP_CODEX_RE.search(last_user):
            return {"decision": "allow"}

        tool_call = HostAdapter(host).tool_call(payload)
        if not isinstance(tool_call, dict):
            return {"decision": "allow"}

        tool_name = str(tool_call.get("name") or "")
        args = tool_call.get("args")
        if not isinstance(args, dict):
            return {"decision": "allow"}

        target_path = str(
            args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        ).strip()
        if not target_path or is_safe_temp_or_config_path(target_path) or not is_code_file(target_path):
            return {"decision": "allow"}

        env = os.environ if environ is None else environ
        profile = env.get("CODEX_GUARD_PROFILE", "strict").strip().lower()
        limit = STRICT_EDIT_LIMIT
        if profile == "pragmatic" and not DELEGATION_COMMAND_RE.search(last_user):
            limit = PRAGMATIC_EDIT_LIMIT

        if tool_name in ("write_file", "write_to_file"):
            content = str(args.get("Content") or args.get("CodeContent") or "")
            if args.get("Append") is True:
                line_count = len(content.splitlines())
                if line_count > limit:
                    return _deny(
                        f"codex-delegate-guard: Appending {line_count} lines to code file "
                        f"'{target_path}' exceeds the {limit}-line direct-edit limit. "
                    )
                return {"decision": "allow"}

            return _deny(
                f"codex-delegate-guard: Creating or overwriting code file '{target_path}' must be "
                "delegated to Codex. "
            )

        if tool_name in ("edit_file", "replace_file_content", "multi_replace_file_content"):
            changed = 0
            if tool_name == "multi_replace_file_content":
                chunks = args.get("ReplacementChunks")
                if isinstance(chunks, list):
                    for chunk in chunks:
                        if isinstance(chunk, dict):
                            old_t = str(chunk.get("TargetContent") or "")
                            new_t = str(chunk.get("ReplacementContent") or "")
                            changed += count_changed_lines(old_t, new_t)
            else:
                old_t = str(args.get("Target") or args.get("TargetContent") or "")
                new_t = str(args.get("Replacement") or args.get("ReplacementContent") or "")
                changed = count_changed_lines(old_t, new_t)

            if changed > limit:
                return _deny(
                    f"codex-delegate-guard: Editing {changed} lines in code file '{target_path}' "
                    f"exceeds the {limit}-line direct-edit limit. "
                )

        return {"decision": "allow"}
    except Exception:
        # This PreToolUse hook must fail open on unexpected host input.
        return {"decision": "allow"}


def run_guard_cli(host: str = HOST_ANTIGRAVITY) -> int:
    try:
        decision = evaluate_guard_payload(json.load(sys.stdin), host=host)
    except Exception:
        decision = {"decision": "allow"}
    print(json.dumps(decision, ensure_ascii=False))
    return 0
