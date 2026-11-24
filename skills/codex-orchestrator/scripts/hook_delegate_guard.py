#!/usr/bin/env python3
"""Antigravity PreToolUse entry point for the Codex delegation guard."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    from codex_orch.host_adapter import run_guard_cli

    sys.exit(run_guard_cli(host="antigravity"))
