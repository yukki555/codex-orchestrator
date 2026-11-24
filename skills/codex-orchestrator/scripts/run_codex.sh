#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
run_codex.sh v3.2.0

Usage:
  run_codex.sh consult   --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh implement --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh review    --dir <DIR> [--uncommitted | --base <BRANCH> | --commit <SHA>] [--spec <FILE> | --prompt <TEXT>] [options]
  run_codex.sh resume    --dir <DIR> (--id <SESSION_ID> | --last) (--spec <FILE> | --prompt <TEXT>) [--sandbox read-only|workspace-write] [options]
  run_codex.sh research  --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh critique  --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh analyze   --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]

Common options:
  --dir <DIR>            Target working directory (default: .)
  --spec <FILE>          Markdown/text prompt file passed via stdin
  --prompt <TEXT>        Inline prompt text passed via stdin (mutually exclusive with --spec)
  --model <MODEL|inherit>
                         Codex model override (-m), or 'inherit' to use config.toml
  --effort <LEVEL>       Reasoning effort: low|medium|high|xhigh|max|ultra|inherit
  --fast                 Preset for unset model/effort: gpt-6.2-sol / low
  --speed <TIER>        default|fast|ultrafast|inherit
                        When omitted, inherit with --profile. With --fast or low/medium
                        effort, use ultrafast on gpt-6.2-sol and fast on other models.
                        Use default otherwise. Astra downgrades ultrafast to fast.
  --no-mcp               Disable external MCP servers via -c 'mcp_servers={}' (default: enabled)
  --with-mcp             Allow external MCP servers from ~/.codex/config.toml
  --profile <NAME>       Codex config profile (-p); omitted model/effort default to inherit
  --ignore-user-config   Ignore ~/.codex/config.toml while preserving ~/.codex auth (default: enabled)
  --use-user-config      Load ~/.codex/config.toml (auto-enabled when --profile or --with-mcp is set)
  --ignore-rules         Skip Codex rules files (default)
  --use-rules            Load rules files (mutually exclusive with --ignore-rules)
  --ephemeral            Run without persisting session files to disk
  --worktree             Run in a managed Git worktree (requires a Git repo, unavailable for resume)
  -c, --config <k=v>     Additional Codex TOML config override (repeatable)
  --timeout <SECONDS>    Kill Codex process group after N seconds and exit 124
  --stall-timeout <SECONDS>
                        Kill Codex after N seconds without event output and exit 125
                        (disabled by default)
  --stdout-mode <auto|compact|full>
                        Last-message display (default: auto)
  --invocation-id <ID>   Non-empty run ID for summary.json (default: generated UUID4 hex)
  --max-cmds <N>         Prompt command budget (0 disables the footer)
                        Defaults: implement=12, research/analyze=10,
                        consult/critique/resume/read-only review=8, native review=0
  --add-dir <DIR>        Additional writable directory for implement or workspace-write resume
                        Repeatable. Resume by ID inherits prior directories if omitted.
  --structured           Enforce the bundled JSON Schema for the mode (mutually exclusive with --schema)
  --schema <FILE>        JSON Schema file for --output-schema
  -i, --image <FILE>     Attach a readable image file (repeatable)
  --web-search           Enable live web search
  --out-dir <DIR>        Directory for artifacts (default: /tmp/codex-orchestrator/<mode>-<ts>-<pid>)
  -h, --help             Show this help message

Default model & effort routing (when --profile and --fast are not set):
  implement              gpt-6.2-sol   / medium
  consult                gpt-6.2-astra / medium
  review                 gpt-6.2-astra / medium
  resume                 gpt-6.2-astra / medium
  research               gpt-6.2-astra / medium
  critique               gpt-6.2-astra / medium
  analyze                gpt-6.2-astra / medium

Mode-specific options:
  research, critique, analyze:
    Run in the read-only sandbox. --structured selects the research, critique,
    or analysis output schema, respectively.
  review:
    Uses native review for a bare target (including --max-cmds 0).
    Uses read-only exec with --spec, --prompt, --structured, --schema, --image, or a positive command budget.
    --uncommitted    Review staged, unstaged, and untracked changes (default)
    --base <BRANCH>  Review changes against base branch
    --commit <SHA>   Review changes introduced by a commit
  resume:
    --id <ID>        Thread/session ID to resume
    --last           Resume most recent recorded session
    --sandbox <MODE> Sandbox mode: read-only (default) or workspace-write
EOF
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
  consult|implement|review|resume|research|critique|analyze)
    MODE="$1"
    shift
    ;;
  *)
    echo "Error: unknown mode '$1'" >&2
    usage >&2
    exit 2
    ;;
esac

TARGET_DIR="."
SPEC_FILE=""
PROMPT_TEXT=""
MODEL=""
EFFORT=""
FAST_MODE=false
SPEED=""
SPEED_EXPLICIT=false
NO_MCP=true
MCP_CHOICE=""
PROFILE=""
IGNORE_USER_CONFIG=true
USER_CONFIG_CHOICE=""
IGNORE_RULES=true
RULES_CHOICE=""
EPHEMERAL=false
WORKTREE=false
TIMEOUT_S=0
STALL_TIMEOUT_S=0
STDOUT_MODE="auto"
INVOCATION_ID=""
MAX_CMDS=0
MAX_CMDS_SET=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/references"
STRUCTURED=false
SCHEMA_FILE=""
SCHEMA_SET=false
WEB_SEARCH=false
OUT_DIR=""
REVIEW_TARGET=""
REVIEW_ARG=""
REVIEW_ENGINE=""
RESUME_ID=""
RESUME_LAST=false
RESUME_SANDBOX="read-only"
declare -a REQUESTED_ADD_DIRS=()
declare -a ADD_DIRS=()
declare -a EXTRA_CONFIGS=()
declare -a IMAGES=()

canonical_file_path() {
  # The trailing marker preserves newlines at the end of a path in command substitution.
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]), end=".")' "$1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      [[ $# -ge 2 ]] || { echo "Error: --dir requires a value" >&2; exit 2; }
      TARGET_DIR="$2"
      shift 2
      ;;
    --spec)
      [[ $# -ge 2 ]] || { echo "Error: --spec requires a value" >&2; exit 2; }
      SPEC_FILE="$2"
      shift 2
      ;;
    --prompt)
      [[ $# -ge 2 ]] || { echo "Error: --prompt requires a value" >&2; exit 2; }
      PROMPT_TEXT="$2"
      shift 2
      ;;
    --model)
      [[ $# -ge 2 ]] || { echo "Error: --model requires a value" >&2; exit 2; }
      MODEL="$2"
      shift 2
      ;;
    --effort)
      [[ $# -ge 2 ]] || { echo "Error: --effort requires a value" >&2; exit 2; }
      EFFORT="$2"
      shift 2
      ;;
    --fast)
      FAST_MODE=true
      shift
      ;;
    --speed)
      [[ $# -ge 2 ]] || { echo "Error: --speed requires a value" >&2; exit 2; }
      case "$2" in
        default|fast|ultrafast|inherit) ;;
        *) echo "Error: invalid --speed '$2'; expected default, fast, ultrafast, or inherit" >&2; exit 2 ;;
      esac
      SPEED="$2"
      SPEED_EXPLICIT=true
      shift 2
      ;;
    --no-mcp)
      if [[ "$MCP_CHOICE" == "with" ]]; then
        echo "Error: --no-mcp and --with-mcp are mutually exclusive" >&2
        exit 2
      fi
      NO_MCP=true
      MCP_CHOICE="no"
      shift
      ;;
    --with-mcp)
      if [[ "$MCP_CHOICE" == "no" ]]; then
        echo "Error: --no-mcp and --with-mcp are mutually exclusive" >&2
        exit 2
      fi
      NO_MCP=false
      MCP_CHOICE="with"
      shift
      ;;
    --profile)
      [[ $# -ge 2 ]] || { echo "Error: --profile requires a value" >&2; exit 2; }
      PROFILE="$2"
      shift 2
      ;;
    --ignore-user-config)
      if [[ "$USER_CONFIG_CHOICE" == "use" ]]; then
        echo "Error: --ignore-user-config and --use-user-config are mutually exclusive" >&2
        exit 2
      fi
      IGNORE_USER_CONFIG=true
      USER_CONFIG_CHOICE="ignore"
      shift
      ;;
    --use-user-config)
      if [[ "$USER_CONFIG_CHOICE" == "ignore" ]]; then
        echo "Error: --ignore-user-config and --use-user-config are mutually exclusive" >&2
        exit 2
      fi
      IGNORE_USER_CONFIG=false
      USER_CONFIG_CHOICE="use"
      shift
      ;;
    --ignore-rules|--use-rules)
      if [[ -n "$RULES_CHOICE" && "$RULES_CHOICE" != "$1" ]]; then
        echo "Error: --ignore-rules and --use-rules are mutually exclusive" >&2
        exit 2
      fi
      RULES_CHOICE="$1"
      IGNORE_RULES=true
      if [[ "$1" == "--use-rules" ]]; then
        IGNORE_RULES=false
      fi
      shift
      ;;
    --ephemeral)
      EPHEMERAL=true
      shift
      ;;
    --worktree)
      WORKTREE=true
      shift
      ;;
    -c|--config)
      [[ $# -ge 2 ]] || { echo "Error: $1 requires a <key=value> argument" >&2; exit 2; }
      EXTRA_CONFIGS+=("$2")
      shift 2
      ;;
    --timeout|--stall-timeout)
      [[ $# -ge 2 ]] || { echo "Error: $1 requires a positive integer (seconds)" >&2; exit 2; }
      if ! [[ "$2" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: $1 must be a positive integer (seconds), got '$2'" >&2
        exit 2
      fi
      if [[ "$1" == "--timeout" ]]; then
        TIMEOUT_S="$2"
      else
        STALL_TIMEOUT_S="$2"
      fi
      shift 2
      ;;
    --stdout-mode)
      [[ $# -ge 2 ]] || { echo "Error: --stdout-mode requires a value" >&2; exit 2; }
      case "$2" in
        auto|compact|full) STDOUT_MODE="$2" ;;
        *) echo "Error: invalid --stdout-mode '$2'; expected auto, compact, or full" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    --invocation-id)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Error: --invocation-id requires a non-empty ID" >&2
        exit 2
      fi
      INVOCATION_ID="$2"
      shift 2
      ;;
    --max-cmds)
      [[ $# -ge 2 ]] || { echo "Error: --max-cmds requires a non-negative integer" >&2; exit 2; }
      if ! [[ "$2" =~ ^[0-9]+$ ]]; then
        echo "Error: --max-cmds must be a non-negative integer, got '$2'" >&2
        exit 2
      fi
      # Strip leading zeroes to avoid Bash interpreting decimal budgets as octal.
      MAX_CMDS="${2#"${2%%[!0]*}"}"
      MAX_CMDS="${MAX_CMDS:-0}"
      MAX_CMDS_SET=true
      shift 2
      ;;
    --add-dir)
      [[ $# -ge 2 ]] || { echo "Error: --add-dir requires a value" >&2; exit 2; }
      REQUESTED_ADD_DIRS+=("$2")
      shift 2
      ;;
    --structured)
      STRUCTURED=true
      shift
      ;;
    --schema)
      [[ $# -ge 2 ]] || { echo "Error: --schema requires a value" >&2; exit 2; }
      SCHEMA_FILE="$2"
      SCHEMA_SET=true
      shift 2
      ;;
    -i|--image)
      [[ $# -ge 2 ]] || { echo "Error: $1 requires a value" >&2; exit 2; }
      if [[ ! -f "$2" || ! -r "$2" ]]; then
        echo "Error: image file is missing or unreadable: $2" >&2
        exit 2
      fi
      resolved_image="$(canonical_file_path "$2")"
      IMAGES+=("${resolved_image%.}")
      shift 2
      ;;
    --web-search)
      WEB_SEARCH=true
      shift
      ;;
    --out-dir)
      [[ $# -ge 2 ]] || { echo "Error: --out-dir requires a value" >&2; exit 2; }
      OUT_DIR="$2"
      shift 2
      ;;
    --uncommitted)
      if [[ -n "$REVIEW_TARGET" && "$REVIEW_TARGET" != "uncommitted" ]]; then
        echo "Error: --uncommitted, --base, and --commit are mutually exclusive" >&2
        exit 2
      fi
      REVIEW_TARGET="uncommitted"
      shift
      ;;
    --base)
      [[ $# -ge 2 ]] || { echo "Error: --base requires a value" >&2; exit 2; }
      if [[ -n "$REVIEW_TARGET" ]]; then
        echo "Error: --uncommitted, --base, and --commit are mutually exclusive" >&2
        exit 2
      fi
      REVIEW_TARGET="base"
      REVIEW_ARG="$2"
      shift 2
      ;;
    --commit)
      [[ $# -ge 2 ]] || { echo "Error: --commit requires a value" >&2; exit 2; }
      if [[ -n "$REVIEW_TARGET" ]]; then
        echo "Error: --uncommitted, --base, and --commit are mutually exclusive" >&2
        exit 2
      fi
      REVIEW_TARGET="commit"
      REVIEW_ARG="$2"
      shift 2
      ;;
    --id)
      [[ $# -ge 2 ]] || { echo "Error: --id requires a value" >&2; exit 2; }
      RESUME_ID="$2"
      shift 2
      ;;
    --last)
      RESUME_LAST=true
      shift
      ;;
    --sandbox)
      [[ $# -ge 2 ]] || { echo "Error: --sandbox requires a value" >&2; exit 2; }
      RESUME_SANDBOX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "Error: target directory does not exist: $TARGET_DIR" >&2
  exit 2
fi
TARGET_DIR="$(cd "$TARGET_DIR" && pwd -P)"

if [[ -n "$SPEC_FILE" && -n "$PROMPT_TEXT" ]]; then
  echo "Error: --spec and --prompt are mutually exclusive" >&2
  exit 2
fi

if [[ "$USER_CONFIG_CHOICE" == "ignore" && -n "$PROFILE" ]]; then
  echo "Error: --ignore-user-config and --profile are mutually exclusive" >&2
  exit 2
fi

if [[ "$USER_CONFIG_CHOICE" == "ignore" && "$MCP_CHOICE" == "with" ]]; then
  echo "Error: --ignore-user-config and --with-mcp are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$PROFILE" || "$MCP_CHOICE" == "with" ]]; then
  IGNORE_USER_CONFIG=false
fi

if [[ -n "$EFFORT" ]]; then
  case "$EFFORT" in
    low|medium|high|xhigh|max|ultra|inherit) ;;
    *)
      echo "Error: invalid --effort '$EFFORT'. GPT-6.2 models require one of: low, medium, high, xhigh, max, ultra (or 'inherit')." >&2
      exit 2
      ;;
  esac
fi

if [[ -n "$SPEC_FILE" && ( -d "$SPEC_FILE" || ! -r "$SPEC_FILE" ) ]]; then
  echo "Error: spec file is missing or unreadable: $SPEC_FILE" >&2
  exit 2
fi

if [[ "$STRUCTURED" == true && "$SCHEMA_SET" == true ]]; then
  echo "Error: --structured and --schema are mutually exclusive" >&2
  exit 2
fi

if [[ "$STRUCTURED" == true ]]; then
  case "$MODE" in
    research) SCHEMA_FILE="$REF_DIR/research-output-schema.json" ;;
    critique) SCHEMA_FILE="$REF_DIR/critique-output-schema.json" ;;
    analyze) SCHEMA_FILE="$REF_DIR/analysis-output-schema.json" ;;
    *) SCHEMA_FILE="$REF_DIR/codex-output-schema.json" ;;
  esac
elif [[ "$SCHEMA_SET" == true && -z "$SCHEMA_FILE" ]]; then
  echo "Error: --schema requires a non-empty path" >&2
  exit 2
fi

if [[ -n "$SCHEMA_FILE" ]]; then
  if ! python3 -I -B -c \
    'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("codex_orch.schema_check", run_name="__main__")' \
    "$SCRIPT_DIR" preflight "$SCHEMA_FILE"; then
    if [[ ! -f "$SCHEMA_FILE" || ! -r "$SCHEMA_FILE" ]]; then
      echo "Error: schema file is missing or unreadable: $SCHEMA_FILE" >&2
    fi
    exit 2
  fi
  if [[ "$SCHEMA_SET" == true ]]; then
    SCHEMA_FILE="$(canonical_file_path "$SCHEMA_FILE")"
    SCHEMA_FILE="${SCHEMA_FILE%.}"
  fi
fi

if [[ "$MODE" != "review" && -z "$SPEC_FILE" && -z "$PROMPT_TEXT" ]]; then
  echo "Error: mode '$MODE' requires --spec <FILE> or --prompt <TEXT>" >&2
  exit 2
fi

if [[ "$MODE" == "resume" ]]; then
  if [[ "$WORKTREE" == true ]]; then
    echo "Error: --worktree is not supported in 'resume' mode" >&2
    exit 2
  fi
  if [[ -n "$RESUME_ID" && "$RESUME_LAST" == true ]]; then
    echo "Error: --id and --last are mutually exclusive" >&2
    exit 2
  fi
  if [[ -z "$RESUME_ID" && "$RESUME_LAST" != true ]]; then
    echo "Error: 'resume' mode requires --id <SESSION_ID> or --last" >&2
    exit 2
  fi
  if [[ "$RESUME_SANDBOX" != "read-only" && "$RESUME_SANDBOX" != "workspace-write" ]]; then
    echo "Error: --sandbox must be 'read-only' or 'workspace-write'" >&2
    exit 2
  fi
fi

add_writable_dir() {
  local directory="$1" existing
  for existing in ${ADD_DIRS[@]+"${ADD_DIRS[@]}"}; do
    if [[ "$existing" == "$directory" ]]; then
      return
    fi
  done
  ADD_DIRS+=("$directory")
}

if [[ ${#REQUESTED_ADD_DIRS[@]} -gt 0 ]]; then
  if [[ "$MODE" != "implement" && ( "$MODE" != "resume" || "$RESUME_SANDBOX" != "workspace-write" ) ]]; then
    echo "Error: --add-dir requires implement or resume with --sandbox workspace-write" >&2
    exit 2
  fi
  for d in "${REQUESTED_ADD_DIRS[@]}"; do
    if [[ ! -d "$d" ]] || ! resolved_dir="$(CDPATH= cd -- "$d" && pwd -P && printf '.')"; then
      echo "Error: --add-dir path is not an accessible directory: $d" >&2
      exit 2
    fi
    add_writable_dir "${resolved_dir%$'\n.'}"
  done
fi

if [[ "$MODE" == "review" ]]; then
  REVIEW_TARGET="${REVIEW_TARGET:-uncommitted}"
  if [[ "$STRUCTURED" == false && -z "$SCHEMA_FILE" && -z "$SPEC_FILE" && -z "$PROMPT_TEXT" &&
        ${#IMAGES[@]} -eq 0 &&
        ( "$MAX_CMDS_SET" == false || "$MAX_CMDS" == 0 ) ]]; then
    REVIEW_ENGINE="native-review"
  else
    REVIEW_ENGINE="read-only-exec"
  fi
fi

if [[ "$MAX_CMDS_SET" == false ]]; then
  if [[ "$MODE" == "implement" ]]; then
    MAX_CMDS=12
  elif [[ "$MODE" == "research" || "$MODE" == "analyze" ]]; then
    MAX_CMDS=10
  elif [[ "$REVIEW_ENGINE" != "native-review" ]]; then
    MAX_CMDS=8
  fi
fi

IS_GIT_REPO=false
if git -C "$TARGET_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  IS_GIT_REPO=true
fi

if [[ "$MODE" == "review" && "$IS_GIT_REPO" == false ]]; then
  echo "Error: '$TARGET_DIR' is not inside a git repository. Use 'consult' mode for non-git review." >&2
  exit 2
fi

if [[ "$WORKTREE" == true && "$IS_GIT_REPO" == false ]]; then
  echo "Error: --worktree requires '$TARGET_DIR' to be inside a git repository." >&2
  exit 2
fi

if [[ "$USER_CONFIG_CHOICE" == "use" && -e "$HOME/.codex/config.toml" && ! -r "$HOME/.codex/config.toml" ]]; then
  echo "Error: --use-user-config requires read access to $HOME/.codex/config.toml" >&2
  exit 2
fi

if [[ "$RULES_CHOICE" == "--use-rules" && -e "$HOME/.codex/rules" && ! -r "$HOME/.codex/rules" ]]; then
  echo "Error: --use-rules requires read access to $HOME/.codex/rules" >&2
  exit 2
fi

if [[ "$MODE" == "implement" && "$WORKTREE" == false && "${CODEX_ORCH_ALLOW_INPLACE_SELF_EDIT:-0}" != "1" ]]; then
  python3 - "$SCRIPT_DIR" "$TARGET_DIR" ${ADD_DIRS[@]+"${ADD_DIRS[@]}"} <<'PY' || exit 2
import os
import sys

script_dir = os.path.realpath(sys.argv[1])
for directory in sys.argv[2:]:
    root = os.path.realpath(directory)
    common = os.path.commonpath((root, script_dir))
    if common in (root, script_dir):
        print(
            f"Error: the writable directory {root} overlaps the running orchestrator scripts. "
            "Pass --worktree or run from a frozen /tmp copy of the scripts.",
            file=sys.stderr,
        )
        sys.exit(2)
PY
fi

CODEX_BIN="$(command -v codex || true)"
if [[ -z "$CODEX_BIN" && -x /opt/homebrew/bin/codex ]]; then
  CODEX_BIN="/opt/homebrew/bin/codex"
fi
if [[ -z "$CODEX_BIN" ]]; then
  echo "Error: 'codex' binary not found in PATH or /opt/homebrew/bin/codex" >&2
  exit 127
fi

if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/tmp/codex-orchestrator/${MODE}-$(date +%Y%m%d-%H%M%S)-$$"
fi
python3 -I -B - "$SCRIPT_DIR" "$OUT_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from codex_orch.env import ensure_private_dir

ensure_private_dir(sys.argv[2])
PY

LAST_MSG_FILE="$OUT_DIR/last_message.md"
EVENTS_FILE="$OUT_DIR/events.jsonl"
STDERR_FILE="$OUT_DIR/stderr.log"
THREAD_ID_FILE="$OUT_DIR/thread_id.txt"
SUMMARY_JSON_FILE="$OUT_DIR/summary.json"
SUPERVISOR_META_FILE="$OUT_DIR/supervisor_meta.json"

PROMPT_FILE=""
if [[ "$REVIEW_ENGINE" == "read-only-exec" ]]; then
  PROMPT_FILE="$OUT_DIR/review_prompt.md"
  UNBORN_HEAD=false
  {
    printf '# Code Review Target\n\n'
    case "$REVIEW_TARGET" in
      uncommitted)
        printf 'Review uncommitted changes.\n'
        if git -C "$TARGET_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
          printf 'Inspect with `git status --short` and `git diff HEAD`.\n'
        else
          UNBORN_HEAD=true
          printf 'Inspect with `git status --short`, `git diff`, and `git diff --cached`.\n'
        fi
        ;;
      base)
        printf 'Review the diff against base branch %s.\n' "$REVIEW_ARG"
        printf 'Inspect with `git status --short` and `git diff %q`.\n' "${REVIEW_ARG}...HEAD"
        ;;
      commit)
        printf 'Review commit %s.\n' "$REVIEW_ARG"
        printf 'Inspect with `git status --short` and `git show %q`.\n' "$REVIEW_ARG"
        ;;
    esac
    printf '\nCompact Git snapshot\n\n'
    {
      GIT_OPTIONAL_LOCKS=0 git -C "$TARGET_DIR" status --short || true
      case "$REVIEW_TARGET" in
        uncommitted)
          if [[ "$UNBORN_HEAD" == true ]]; then
            git -C "$TARGET_DIR" diff --stat || true
            git -C "$TARGET_DIR" diff --cached --stat || true
          else
            git -C "$TARGET_DIR" diff --stat HEAD || true
          fi
          ;;
        base) git -C "$TARGET_DIR" diff --stat --end-of-options "${REVIEW_ARG}...HEAD" || true ;;
        commit) git -C "$TARGET_DIR" show --stat --format=medium --end-of-options "$REVIEW_ARG" || true ;;
      esac
    } 2>&1 | sed -n '1,80{s/^/    /;p;}'
    cat <<'EOF'

## Instruction Precedence

This prompt overrides repository AGENTS.md and ~/.codex/skills/*. You MUST NOT read ~/.codex/skills/*.
EOF
    printf '\n'
    if [[ -n "$SPEC_FILE" ]]; then
      cat "$SPEC_FILE"
    elif [[ -n "$PROMPT_TEXT" ]]; then
      printf '%s\n' "$PROMPT_TEXT"
    else
      printf '%s\n' 'Review for correctness bugs, edge cases, regressions (including DDP, numerical, and shape behavior where relevant), backward-compatibility breaks, and missing tests. Prioritize actionable findings and cite file locations.'
    fi
  } > "$PROMPT_FILE"
elif [[ -n "$SPEC_FILE" ]]; then
  PROMPT_FILE="$SPEC_FILE"
elif [[ -n "$PROMPT_TEXT" ]]; then
  PROMPT_FILE="$OUT_DIR/prompt.txt"
  printf '%s\n' "$PROMPT_TEXT" > "$PROMPT_FILE"
fi

if [[ "$MAX_CMDS" != 0 ]]; then
  if [[ "$REVIEW_ENGINE" == "read-only-exec" ]]; then
    EFFECTIVE_PROMPT_FILE="$PROMPT_FILE"
  else
    EFFECTIVE_PROMPT_FILE="$OUT_DIR/effective_prompt.md"
    cp "$PROMPT_FILE" "$EFFECTIVE_PROMPT_FILE"
  fi
  cat >> "$EFFECTIVE_PROMPT_FILE" <<EOF

## Execution Budget
- Batch multi-file reads, searches, and JSON/log parsing into at most $MAX_CMDS concise python3 or shell commands instead of sequential per-file calls.
- Never run broad repo scans (rg --files, find, ls -R) when exact paths are already named above.
- Treat verified inline snapshots/metrics in this prompt as ground truth; do not re-read them unless checking a discrepancy.
- Keep total command stdout concise (< 20 KB) and stop once required findings are verified.
- Do NOT read ~/.codex/skills/* or probe/scan host disks for missing runtimes when environment facts are given in the Spec.
EOF
  PROMPT_FILE="$EFFECTIVE_PROMPT_FILE"
fi

PRIOR_MODEL=""
WRITABLE_ROOTS_TOML=""
if [[ "$MODE" == "resume" && -n "$RESUME_ID" ]]; then
  INHERIT_ADD_DIRS=false
  if [[ "$RESUME_SANDBOX" == "workspace-write" && ${#REQUESTED_ADD_DIRS[@]} -eq 0 ]]; then
    INHERIT_ADD_DIRS=true
  fi
  {
    IFS= read -r -d '' PRIOR_MODEL || true
    while IFS= read -r -d '' d; do
      add_writable_dir "$d"
    done
  } < <(python3 - "$RESUME_ID" "$INHERIT_ADD_DIRS" <<'PY'
import glob
import json
import os
import sys

tid, inherit_dirs = sys.argv[1:]
paths = []
for depth in range(1, 4):
    for path in glob.glob("/tmp/codex-orchestrator" + "/*" * depth + "/summary.json"):
        try:
            paths.append((os.path.getmtime(path), path))
        except OSError:
            continue

for _, path in sorted(paths, reverse=True):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        continue
    if isinstance(data, dict) and data.get("thread_id") == tid:
        model = data.get("model")
        if not isinstance(model, str) or model == "inherit" or "\0" in model:
            model = ""
        fields = [model]
        dirs = data.get("add_dirs")
        if inherit_dirs == "true" and isinstance(dirs, list):
            fields.extend(d for d in dirs if isinstance(d, str) and os.path.isdir(d))
        sys.stdout.buffer.write(b"".join(os.fsencode(field) + b"\0" for field in fields))
        break
PY
  )
fi

if [[ "$MODE" == "resume" && "$RESUME_SANDBOX" == "workspace-write" && ${#ADD_DIRS[@]} -gt 0 ]]; then
  WRITABLE_ROOTS_TOML="$(python3 - "$TARGET_DIR" "${ADD_DIRS[@]}" <<'PY'
import json
import sys

roots = list(dict.fromkeys(sys.argv[1:]))
print(json.dumps(roots, ensure_ascii=False).replace("\x7f", "\\u007f"))
PY
  )"
fi

if [[ "$FAST_MODE" == true ]]; then
  DEFAULT_MODEL="gpt-6.2-sol"
  DEFAULT_EFFORT="low"
elif [[ -n "$PROFILE" ]]; then
  DEFAULT_MODEL="inherit"
  DEFAULT_EFFORT="inherit"
else
  DEFAULT_EFFORT="medium"
  case "$MODE" in
    implement) DEFAULT_MODEL="gpt-6.2-sol" ;;
    resume) DEFAULT_MODEL="${PRIOR_MODEL:-gpt-6.2-astra}" ;;
    *) DEFAULT_MODEL="gpt-6.2-astra" ;;
  esac
fi

EFFECTIVE_MODEL="${MODEL:-$DEFAULT_MODEL}"
EFFECTIVE_EFFORT="${EFFORT:-$DEFAULT_EFFORT}"
if [[ "$SPEED_EXPLICIT" == true ]]; then
  EFFECTIVE_SPEED="$SPEED"
elif [[ -n "$PROFILE" ]]; then
  EFFECTIVE_SPEED="inherit"
elif [[ "$FAST_MODE" == true || "$EFFECTIVE_EFFORT" == "low" || "$EFFECTIVE_EFFORT" == "medium" ]]; then
  if [[ "$EFFECTIVE_MODEL" == "gpt-6.2-sol" ]]; then
    EFFECTIVE_SPEED="ultrafast"
  else
    EFFECTIVE_SPEED="fast"
  fi
else
  EFFECTIVE_SPEED="default"
fi
if [[ "$EFFECTIVE_SPEED" == "ultrafast" && "$EFFECTIVE_MODEL" == "gpt-6.2-astra" ]]; then
  echo "warning: gpt-6.2-astra does not support ultrafast; downgrading speed to fast" >&2
  EFFECTIVE_SPEED="fast"
fi

declare -a COMMON_CFG=()
declare -a CMD=()
SANDBOX_DESC=""

build_codex_cmd() {
  local -a prompt_args=()
  local img
  [[ -n "$SCHEMA_FILE" ]] && prompt_args+=("--output-schema" "$SCHEMA_FILE")
  for img in ${IMAGES[@]+"${IMAGES[@]}"}; do
    prompt_args+=("-i" "$img")
  done

  COMMON_CFG=()
  if [[ "$NO_MCP" == true && "$IGNORE_USER_CONFIG" == false ]]; then
    COMMON_CFG+=("-c" "mcp_servers={}")
  fi
  if [[ "$EFFECTIVE_EFFORT" != "inherit" ]]; then
    COMMON_CFG+=("-c" "model_reasoning_effort=\"$EFFECTIVE_EFFORT\"")
  fi
  if [[ "$EFFECTIVE_SPEED" == "fast" || "$EFFECTIVE_SPEED" == "ultrafast" ||
        ( "$SPEED_EXPLICIT" == true && "$EFFECTIVE_SPEED" == "default" ) ]]; then
    COMMON_CFG+=("-c" "service_tier=\"$EFFECTIVE_SPEED\"")
  fi
  if [[ -n "$WRITABLE_ROOTS_TOML" ]]; then
    COMMON_CFG+=("-c" "sandbox_workspace_write.writable_roots=$WRITABLE_ROOTS_TOML")
  fi
  if [[ "$WEB_SEARCH" == false ]]; then
    COMMON_CFG+=("-c" 'web_search="disabled"')
  fi
  if [[ ${#EXTRA_CONFIGS[@]} -gt 0 ]]; then
    for cfg in "${EXTRA_CONFIGS[@]}"; do
      COMMON_CFG+=("-c" "$cfg")
    done
  fi

  if [[ "$WEB_SEARCH" == true ]]; then
    CMD=("$CODEX_BIN" "--search" "exec")
  else
    CMD=("$CODEX_BIN" "exec")
  fi
  SANDBOX_DESC=""

  case "$MODE:$REVIEW_ENGINE" in
    consult:|research:|critique:|analyze:|review:read-only-exec)
      SANDBOX_DESC="read-only"
      CMD+=("-s" "read-only" "-C" "$TARGET_DIR")
      if [[ "$IS_GIT_REPO" == false ]]; then
        CMD+=("--skip-git-repo-check")
      fi
      [[ "$EPHEMERAL" == true ]] && CMD+=("--ephemeral")
      [[ "$WORKTREE" == true ]] && CMD+=("--worktree")
      [[ "$IGNORE_USER_CONFIG" == true ]] && CMD+=("--ignore-user-config")
      [[ "$IGNORE_RULES" == true ]] && CMD+=("--ignore-rules")
      [[ "$EFFECTIVE_MODEL" != "inherit" ]] && CMD+=("-m" "$EFFECTIVE_MODEL")
      [[ -n "$PROFILE" ]] && CMD+=("-p" "$PROFILE")
      if [[ ${#COMMON_CFG[@]} -gt 0 ]]; then
        CMD+=("${COMMON_CFG[@]}")
      fi
      CMD+=(${prompt_args[@]+"${prompt_args[@]}"} "--json" "-o" "$LAST_MSG_FILE" "-")
      ;;
    implement:)
      SANDBOX_DESC="workspace-write"
      CMD+=("-s" "workspace-write" "-C" "$TARGET_DIR")
      if [[ "$IS_GIT_REPO" == false ]]; then
        CMD+=("--skip-git-repo-check")
      fi
      [[ "$EPHEMERAL" == true ]] && CMD+=("--ephemeral")
      [[ "$WORKTREE" == true ]] && CMD+=("--worktree")
      [[ "$IGNORE_USER_CONFIG" == true ]] && CMD+=("--ignore-user-config")
      [[ "$IGNORE_RULES" == true ]] && CMD+=("--ignore-rules")
      [[ "$EFFECTIVE_MODEL" != "inherit" ]] && CMD+=("-m" "$EFFECTIVE_MODEL")
      [[ -n "$PROFILE" ]] && CMD+=("-p" "$PROFILE")
      if [[ ${#COMMON_CFG[@]} -gt 0 ]]; then
        CMD+=("${COMMON_CFG[@]}")
      fi
      if [[ ${#ADD_DIRS[@]} -gt 0 ]]; then
        for d in "${ADD_DIRS[@]}"; do
          CMD+=("--add-dir" "$d")
        done
      fi
      CMD+=(${prompt_args[@]+"${prompt_args[@]}"} "--json" "-o" "$LAST_MSG_FILE" "-")
      ;;
    review:native-review)
      SANDBOX_DESC="review-default"
      CMD+=("-C" "$TARGET_DIR")
      [[ -n "$PROFILE" ]] && CMD+=("-p" "$PROFILE")
      CMD+=("review")
      [[ "$EPHEMERAL" == true ]] && CMD+=("--ephemeral")
      [[ "$WORKTREE" == true ]] && CMD+=("--worktree")
      [[ "$IGNORE_USER_CONFIG" == true ]] && CMD+=("--ignore-user-config")
      [[ "$IGNORE_RULES" == true ]] && CMD+=("--ignore-rules")
      case "$REVIEW_TARGET" in
        uncommitted)
          CMD+=("--uncommitted")
          ;;
        base)
          CMD+=("--base" "$REVIEW_ARG")
          ;;
        commit)
          CMD+=("--commit" "$REVIEW_ARG")
          ;;
      esac
      [[ "$EFFECTIVE_MODEL" != "inherit" ]] && CMD+=("-m" "$EFFECTIVE_MODEL")
      if [[ ${#COMMON_CFG[@]} -gt 0 ]]; then
        CMD+=("${COMMON_CFG[@]}")
      fi
      CMD+=("--json" "-o" "$LAST_MSG_FILE")
      ;;
    resume:)
      SANDBOX_DESC="$RESUME_SANDBOX"
      CMD+=("-C" "$TARGET_DIR" "-s" "$RESUME_SANDBOX")
      [[ -n "$PROFILE" ]] && CMD+=("-p" "$PROFILE")
      CMD+=("resume")
      [[ "$EPHEMERAL" == true ]] && CMD+=("--ephemeral")
      [[ "$IGNORE_USER_CONFIG" == true ]] && CMD+=("--ignore-user-config")
      [[ "$IGNORE_RULES" == true ]] && CMD+=("--ignore-rules")
      if [[ "$IS_GIT_REPO" == false ]]; then
        CMD+=("--skip-git-repo-check")
      fi
      [[ "$EFFECTIVE_MODEL" != "inherit" ]] && CMD+=("-m" "$EFFECTIVE_MODEL")
      if [[ ${#COMMON_CFG[@]} -gt 0 ]]; then
        CMD+=("${COMMON_CFG[@]}")
      fi
      CMD+=(${prompt_args[@]+"${prompt_args[@]}"} "--json" "-o" "$LAST_MSG_FILE")
      if [[ "$RESUME_LAST" == true ]]; then
        CMD+=("--last")
      else
        CMD+=("$RESUME_ID")
      fi
      CMD+=("-")
      ;;
  esac
}

build_codex_cmd
rm -f -- "$LAST_MSG_FILE" "$SUPERVISOR_META_FILE"

SUPERVISOR_PID=""
SUPERVISOR_EXIT=""
WRAPPER_SIGNAL=0
forward_supervisor_signal() {
  if (( WRAPPER_SIGNAL == 0 )); then
    WRAPPER_SIGNAL=$1
  fi
  if [[ -n "$SUPERVISOR_PID" && -z "$SUPERVISOR_EXIT" ]]; then
    trap '' TERM INT
    kill -"$WRAPPER_SIGNAL" "$SUPERVISOR_PID" 2>/dev/null || true
    wait "$SUPERVISOR_PID"
    SUPERVISOR_EXIT=$?
  fi
}

set +e
trap 'forward_supervisor_signal 15' TERM
trap 'forward_supervisor_signal 2' INT
python3 -I -B -c \
  'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("codex_orch.supervisor", run_name="__main__")' \
  "$SCRIPT_DIR" \
  --timeout "$TIMEOUT_S" --stall-timeout "$STALL_TIMEOUT_S" --prompt-file "${PROMPT_FILE:-}" \
  --events "$EVENTS_FILE" --stderr "$STDERR_FILE" --meta-out "$SUPERVISOR_META_FILE" -- "${CMD[@]}" &
SUPERVISOR_PID=$!
if (( WRAPPER_SIGNAL != 0 )); then
  forward_supervisor_signal "$WRAPPER_SIGNAL"
fi
if [[ -z "$SUPERVISOR_EXIT" ]]; then
  wait "$SUPERVISOR_PID"
  SUPERVISOR_EXIT="${SUPERVISOR_EXIT:-$?}"
fi
trap - TERM INT
set -e

python3 -I -B - \
  "$EVENTS_FILE" "$STDERR_FILE" "$SUMMARY_JSON_FILE" "$THREAD_ID_FILE" \
  "$SCRIPT_DIR" "$SUPERVISOR_META_FILE" "$SUPERVISOR_EXIT" "$WRAPPER_SIGNAL" "$MODE" "$EFFECTIVE_MODEL" "$EFFECTIVE_EFFORT" \
  "$EFFECTIVE_SPEED" "$FAST_MODE" "$NO_MCP" "$IGNORE_USER_CONFIG" "$IGNORE_RULES" \
  "$STRUCTURED" "$EPHEMERAL" "$WORKTREE" "$REVIEW_ENGINE" \
  "$MAX_CMDS" "$TIMEOUT_S" "$STALL_TIMEOUT_S" "$STDOUT_MODE" "$INVOCATION_ID" \
  "$TARGET_DIR" "$SANDBOX_DESC" "$OUT_DIR" "$LAST_MSG_FILE" \
  "$WEB_SEARCH" "$SCHEMA_FILE" "${#ADD_DIRS[@]}" \
  ${ADD_DIRS[@]+"${ADD_DIRS[@]}"} ${IMAGES[@]+"${IMAGES[@]}"} <<'PY'
import json
import os
import sys
import uuid
from collections import deque
from contextlib import nullcontext
from pathlib import Path

(
    events_p, stderr_p, summary_p, tid_p,
    script_dir, supervisor_meta_p, supervisor_exit_s, wrapper_signal_s, mode, model, effort,
    speed, fast_preset_s, no_mcp_s, ignore_cfg_s, ignore_rules_s,
    structured_s, ephemeral_s, worktree_s, review_engine,
    max_cmds_s, timeout_s, stall_timeout_s, stdout_mode, invocation_id,
    target_dir, sandbox_desc, out_dir, last_msg_p,
    web_search_s, schema_file, add_dirs_count, *paths
) = sys.argv[1:]

sys.path.insert(0, script_dir)
from codex_orch import compact_stdout, env, schema_check

add_dirs = paths[:int(add_dirs_count)]
images = paths[int(add_dirs_count):]

events_path = Path(events_p)
stderr_path = Path(stderr_p)
summary_path = Path(summary_p)
tid_path = Path(tid_p)

try:
    meta = json.loads(Path(supervisor_meta_p).read_text(encoding="utf-8"))
    codex_exit_code = meta["codex_exit_code"]
    duration_s = meta["duration_s"]
    timed_out = meta["timed_out"]
    stall_timed_out = meta["stall_timed_out"]
    paused_quota = meta["paused_quota"]
    cancelled = meta["cancelled"]
    sig = meta["signal"]
    if (
        type(codex_exit_code) is not int
        or type(duration_s) not in (int, float)
        or any(type(flag) is not bool for flag in (timed_out, stall_timed_out, paused_quota, cancelled))
        or (sig is not None and (type(sig) is not int or not 0 < sig < 128))
    ):
        raise ValueError("unexpected supervisor metadata types")
except (OSError, ValueError, TypeError, KeyError) as error:
    print(f"Error: unable to read supervisor metadata: {error}", file=sys.stderr)
    codex_exit_code = int(supervisor_exit_s) or 1
    duration_s = 0.0
    timed_out = stall_timed_out = paused_quota = cancelled = False
    sig = None

if not cancelled and (wrapper_signal := int(wrapper_signal_s)):
    cancelled, sig = True, wrapper_signal

thread_id = ""
commands = 0
cmd_output_bytes = 0
malformed_event_lines = 0
tokens = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
seen_errors = set()
event_errors = []
modified_files = {}
in_flight = {}
failed_commands = deque(maxlen=5)
last_agent_messages = deque(maxlen=3)

def reject_json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


event_stream = events_path.open("rb") if events_path.is_file() else nullcontext(())
with event_stream as events:
    for raw_line in events:
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8"), parse_constant=reject_json_constant)
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, dict):
            malformed_event_lines += 1
            continue

        if not thread_id:
            for key in ("thread_id", "session_id", "conversation_id"):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    thread_id = val
                    break
            if not thread_id:
                for parent in ("thread", "session"):
                    nested = obj.get(parent)
                    if isinstance(nested, dict):
                        val = nested.get("id")
                        if isinstance(val, str) and val:
                            thread_id = val
                            break

        etype = obj.get("type")
        if etype in ("item.started", "item.completed"):
            item = obj.get("item")
            if not isinstance(item, dict):
                continue
            item_type, item_id = item.get("type"), item.get("id")
            if etype == "item.started":
                command = item.get("command")
                if item_type == "command_execution" and isinstance(item_id, str) and isinstance(command, str):
                    in_flight[item_id] = env.redact_secrets(command)[:200]
                continue

            if isinstance(item_id, str):
                in_flight.pop(item_id, None)
            if item_type == "command_execution":
                commands += 1
                out_text = item.get("aggregated_output")
                if not isinstance(out_text, str):
                    out_text = ""
                cmd_output_bytes += len(out_text.encode("utf-8", errors="replace"))
                exit_code = item.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    command = item.get("command")
                    failed_commands.append({
                        "exit_code": exit_code,
                        "command": env.redact_secrets(command)[:200] if isinstance(command, str) else "",
                        "output_tail": env.redact_secrets(out_text)[-300:],
                    })
            elif item_type == "file_change":
                changes = item.get("changes")
                if isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict) and isinstance(change.get("path"), str):
                            modified_files[change["path"]] = None
            elif item_type == "agent_message" and isinstance(item.get("text"), str):
                last_agent_messages.append(item["text"][:400])
        elif etype == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict):
                tokens["input"] = int(usage.get("input_tokens") or 0)
                tokens["cached"] = int(usage.get("cached_input_tokens") or 0)
                tokens["output"] = int(usage.get("output_tokens") or 0)
                tokens["reasoning"] = int(usage.get("reasoning_output_tokens") or 0)
        elif etype == "error":
            val = obj.get("message")
            if isinstance(val, str) and val and val not in seen_errors:
                seen_errors.add(val)
                event_errors.append(f"error: {env.redact_secrets(val)}")
        elif etype == "turn.failed":
            err = obj.get("error")
            msg = ""
            if isinstance(err, dict):
                val = err.get("message")
                if isinstance(val, str) and val:
                    msg = val
            elif isinstance(err, str) and err:
                msg = err
            if msg and msg not in seen_errors:
                seen_errors.add(msg)
                event_errors.append(f"turn.failed: {env.redact_secrets(msg)}")

stderr_errors = 0
stderr_tail = deque(maxlen=30)
if stderr_path.is_file():
    with stderr_path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stderr_tail.append(line)
            if "ERROR" in line:
                stderr_errors += 1

if thread_id:
    tid_path.write_text(thread_id + "\n", encoding="utf-8")
else:
    thread_id = "unknown"

cmd_output_kb = round(cmd_output_bytes / 1024.0, 2)
max_cmds = int(max_cmds_s)
budget_exceeded = max_cmds > 0 and commands > max_cmds
budget_utilization = commands / max_cmds if max_cmds > 0 else None
cmd_per_modified_file = commands / len(modified_files) if modified_files else None

last_msg_path = Path(last_msg_p)
last_message = last_msg_path.read_bytes() if last_msg_path.is_file() else b""
structured = structured_s == "true"
structured_valid = None
structured_error = None
if codex_exit_code == 0 and not (cancelled or timed_out or stall_timed_out or paused_quota) and (structured or schema_file):
    structured_valid, structured_error, _ = schema_check.validate_last_message(
        last_message, schema_file or None
    )

if cancelled:
    status, wrapper_exit_code = "cancelled", 128 + (sig or 15)
elif stall_timed_out:
    status, wrapper_exit_code = "stall_timeout", 125
elif timed_out:
    status, wrapper_exit_code = "timeout", 124
elif paused_quota:
    status, wrapper_exit_code = "paused_quota", codex_exit_code or 75
elif codex_exit_code != 0:
    status, wrapper_exit_code = "error", codex_exit_code
elif (structured or schema_file) and structured_valid is False:
    status, wrapper_exit_code = "schema_error", 3
else:
    status, wrapper_exit_code = "ok", 0

preview, stdout_truncated = "", False
if last_message:
    preview, stdout_truncated = compact_stdout.format_last_message_preview(
        last_message, mode=stdout_mode, threshold_bytes=4096, max_preview_bytes=2048,
        last_message_path=last_msg_p, summary_path=summary_p,
    )

summary = {
    "status": status,
    "exit_code": wrapper_exit_code,
    "codex_exit_code": codex_exit_code,
    "cancelled": cancelled,
    "signal": sig,
    "invocation_id": invocation_id or uuid.uuid4().hex,
    "timed_out": timed_out,
    "stall_timed_out": stall_timed_out,
    "paused_quota": paused_quota,
    "mode": mode,
    "model": model,
    "effort": effort,
    "fast": speed in ("fast", "ultrafast"),
    "speed": speed,
    "fast_preset": fast_preset_s == "true",
    "no_mcp": no_mcp_s == "true",
    "ignore_user_config": ignore_cfg_s == "true",
    "ignore_rules": ignore_rules_s == "true",
    "structured": structured,
    "structured_valid": structured_valid,
    "schema_file": schema_file,
    "web_search": web_search_s == "true",
    "images": images,
    "ephemeral": ephemeral_s == "true",
    "worktree": worktree_s == "true",
    "review_engine": review_engine,
    "add_dirs": add_dirs,
    "modified_files": list(modified_files),
    "max_cmds": max_cmds,
    "budget_exceeded": budget_exceeded,
    "budget_utilization": budget_utilization,
    "cmd_per_modified_file": cmd_per_modified_file,
    "timeout_s": int(timeout_s),
    "stall_timeout_s": int(stall_timeout_s),
    "duration_s": duration_s,
    "thread_id": thread_id,
    "commands": commands,
    "malformed_event_lines": malformed_event_lines,
    "cmd_output_kb": cmd_output_kb,
    "tokens": tokens,
    "stderr_errors": stderr_errors,
    "dir": target_dir,
    "sandbox": sandbox_desc,
    "out_dir": out_dir,
    "last_message": last_msg_p,
    "last_message_bytes": len(last_message),
    "stdout_mode": stdout_mode,
    "stdout_truncated": stdout_truncated,
    "events": events_p,
    "stderr": stderr_p,
    "summary_json": summary_p,
}
if status != "ok":
    summary["partial_progress"] = {
        "modified_files": summary["modified_files"],
        "last_agent_messages": list(last_agent_messages),
        "failed_commands": list(failed_commands),
        "in_flight_commands": list(in_flight.values())[:5],
    }
summary_tmp = summary_path.with_name(f".summary.json.tmp.{os.getpid()}")
summary_tmp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
os.replace(summary_tmp, summary_path)


def print_field(key):
    value = summary[key]
    print(f"{key}: {json.dumps(value) if isinstance(value, bool) else value}")


for key in ("status", "exit_code", "codex_exit_code", "mode", "model", "effort", "fast", "speed", "no_mcp",
            "ignore_user_config", "ignore_rules", "structured", "web_search"):
    print_field(key)
if review_engine:
    print_field("review_engine")
if structured_valid is not None:
    print_field("structured_valid")
    if structured_error:
        flag = "--structured" if structured else "--schema"
        print(f"warning: {flag} output in last_message.md {env.redact_secrets(structured_error)}", file=sys.stderr)
for key in ("duration_s", "commands", "cmd_output_kb", "budget_exceeded", "stdout_truncated"):
    print_field(key)
print(f"tokens: in={tokens['input']} cached={tokens['cached']} out={tokens['output']} reason={tokens['reasoning']}")
for key in ("stderr_errors", "dir", "sandbox", "thread_id", "out_dir",
            "summary_json", "last_message", "events", "stderr"):
    print_field(key)

if stderr_errors and not summary["no_mcp"]:
    print(f"tip: stderr reported {stderr_errors} error(s) with --with-mcp enabled; retry with default --no-mcp or --ignore-user-config")

progress = summary.get("partial_progress", {})
if any(progress.values()):
    print("--- partial_progress ---")
    for key, entries in progress.items():
        if entries:
            detail = json.dumps(entries[:5])
            suffix = f" (+{len(entries) - 5} more in summary.json)" if len(entries) > 5 else ""
            print(f"{key}: {detail}{suffix}")

if preview:
    print("--- last_message ---")
    print(preview, end="" if preview.endswith("\n") else "\n")

if status != "ok" and event_errors:
    print("--- event_errors ---")
    print("\n".join(event_errors))

if wrapper_exit_code != 0 and stderr_tail:
    print("--- stderr (tail) ---")
    print(env.redact_secrets("".join(stderr_tail)), end="")

sys.exit(wrapper_exit_code)
PY
