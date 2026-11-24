#!/usr/bin/env python3
"""Antigravity PreInvocation and Stop entry point for context handoff."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from codex_orch.context_hook import run_context_hook_cli
    except Exception:
        print("{}")
    else:
        sys.exit(run_context_hook_cli())
