#!/usr/bin/env bash
# codex-orchestrator v4.0.0
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 -I -B -c '
import sys

sys.path.insert(0, sys.argv.pop(1))
from codex_orch.single_runner import main

sys.exit(main())
' "$SCRIPT_DIR" "$@"
