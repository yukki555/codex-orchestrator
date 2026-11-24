#!/usr/bin/env python3
"""PreToolUse delegate guard for codex-orchestrator v3.2.0.

Prevents Gemini from bypassing Codex on non-trivial local code writes/edits
while allowing Docs/Configs, controlled scratch paths, profile-limited edits,
and explicit user opt-outs. Always fails open on parse or unexpected errors.
"""

from __future__ import annotations

import json
import os
import re
import sys
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


def allow() -> None:
    print(json.dumps({"decision": "allow"}))


def deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))


def is_safe_temp_or_config_path(path_str: str) -> bool:
    # realpath resolves symlinks in the existing prefix, even for a new file.
    path = Path(os.path.realpath(os.path.expanduser(path_str)))
    safe_roots = (
        Path(os.path.realpath("/tmp")) / "codex-orchestrator",
        Path("/tmp/codex-orchestrator"),
        Path("/private/tmp/codex-orchestrator"),
        Path(os.path.realpath(os.path.expanduser("~/.gemini/antigravity/brain"))),
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


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            allow()
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            allow()
            return 0

        last_user = str(payload.get("lastUserInput") or "")
        if SKIP_CODEX_RE.search(last_user):
            allow()
            return 0

        tool_call = payload.get("toolCall")
        if not isinstance(tool_call, dict):
            allow()
            return 0

        tool_name = str(tool_call.get("name") or "")
        args = tool_call.get("args")
        if not isinstance(args, dict):
            allow()
            return 0

        target_path = str(
            args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        ).strip()
        if not target_path:
            allow()
            return 0

        if is_safe_temp_or_config_path(target_path):
            allow()
            return 0

        if not is_code_file(target_path):
            allow()
            return 0

        profile = os.environ.get("CODEX_GUARD_PROFILE", "strict").strip().lower()
        limit = STRICT_EDIT_LIMIT
        if profile == "pragmatic" and not DELEGATION_COMMAND_RE.search(last_user):
            limit = PRAGMATIC_EDIT_LIMIT

        if tool_name in ("write_file", "write_to_file"):
            content = str(args.get("Content") or args.get("CodeContent") or "")
            if args.get("Append") is True:
                line_count = len(content.splitlines())
                if line_count > limit:
                    deny(
                        f"codex-delegate-guard: Appending {line_count} lines to code file "
                        f"'{target_path}' exceeds the {limit}-line direct-edit limit. "
                        + DELEGATION_GUIDANCE
                    )
                    return 0
                allow()
                return 0

            deny(
                f"codex-delegate-guard: Creating or overwriting code file '{target_path}' must be "
                "delegated to Codex. " + DELEGATION_GUIDANCE
            )
            return 0

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
                deny(
                    f"codex-delegate-guard: Editing {changed} lines in code file '{target_path}' "
                    f"exceeds the {limit}-line direct-edit limit. "
                    + DELEGATION_GUIDANCE
                )
                return 0

            allow()
            return 0

        allow()
        return 0
    except Exception:
        allow()
        return 0


if __name__ == "__main__":
    sys.exit(main())
