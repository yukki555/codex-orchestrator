#!/usr/bin/env bash
# codex-orchestrator v3.5.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -I -B -c '
import sys
sys.path.insert(0, sys.argv.pop(1))
from codex_orch.parallel_runner import main
sys.exit(main())
' "$SCRIPT_DIR" "$SCRIPT_DIR/run_codex.sh" "$@"
