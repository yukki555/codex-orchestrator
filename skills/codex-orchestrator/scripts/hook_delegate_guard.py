#!/usr/bin/env python3
"""PreToolUse delegate guard for codex-orchestrator.

Prevents Gemini from bypassing Codex on non-trivial local code writes/edits
while allowing Docs/Configs, /tmp specs, <=10-line micro-fixes, and explicit
user opt-outs. Always fails open on parse or unexpected errors.
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
MAX_DIRECT_EDIT_LINES = 10
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

        if tool_name in ("write_file", "write_to_file"):
            content = str(args.get("Content") or args.get("CodeContent") or "")
            if args.get("Append"):
                line_count = len(content.splitlines())
                if line_count > MAX_DIRECT_EDIT_LINES:
                    deny(
                        f"codex-delegate-guard: Appending {line_count} lines to code file "
                        f"'{target_path}' exceeds the <= {MAX_DIRECT_EDIT_LINES}-line direct-edit limit. "
                        "Write a task Spec under /tmp/codex-orchestrator/<task-id>/spec.md and "
                        "delegate implementation to the `codex-worker` subagent or `run_codex.sh implement`."
                    )
                    return 0
                allow()
                return 0

            deny(
                f"codex-delegate-guard: Creating or overwriting code file '{target_path}' must be "
                "delegated to Codex. Write a task Spec under /tmp/codex-orchestrator/<task-id>/spec.md "
                "and delegate to the `codex-worker` subagent or `run_codex.sh implement`."
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

            if changed > MAX_DIRECT_EDIT_LINES:
                deny(
                    f"codex-delegate-guard: Editing {changed} lines in code file '{target_path}' "
                    f"exceeds the <= {MAX_DIRECT_EDIT_LINES}-line direct-edit limit. "
                    "Write a task Spec under /tmp/codex-orchestrator/<task-id>/spec.md and "
                    "delegate implementation to the `codex-worker` subagent or `run_codex.sh implement`."
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
