---
name: codex-orchestrator
description: >-
  Orchestrate local OpenAI Codex CLI (codex exec) via run_codex.sh / run_codex_parallel.sh
  (or codex-worker for multi-round repairs & remote SSH/GPU isolation) across scoped coding,
  DAG parallel implementation, Git review, deep research, blind debate, and experiment analysis.
---

# Codex Orchestrator (v3.2)

Operate a **General-Purpose Manager + High-Execution Specialist** workflow in Google Antigravity built around a decoupled **Brain (`rules/`, `skills/`) / Session (`SPEC.md` + immutable artifacts) / Hands (`run_codex.sh`, `run_codex_parallel.sh`, `codex_orch/`)** architecture.

## Fold-First Cost Ladder & Entry Points

Before spawning workers, apply the Fold-First ladder (multi-agent fan-out can consume ~15× tokens vs chat, compared to ~4× for single-agent loops):

1. **Tier 0 (Main Loop + Skills)**: Single-fact lookups, docs/config edits, or small fixes within the active Guard limit (`<= 10` lines under default `strict`, or `<= 30` lines on an existing code file when `CODEX_GUARD_PROFILE=pragmatic`).
2. **Tier 1 (Direct 1-Hop `run_codex.sh` — Default)**: Clean single-pass `implement`, `consult`, `review --structured`, `research`, `critique`, or `analyze` via `run_command`. Eliminates unnecessary subagent hops and Telephone Game drift.
3. **Tier 2 (Isolator or Parallel Fan-Out — Only When Justified)**:
   * **`codex-worker` subagent**: Only when isolating multi-round repair loops (`<= 2` rounds), remote SSH/GPU checks, or noisy test logs out of the main conversation.
   * **Parallel `/dag` / `/research` / `/analyze`**: Only when `>= 3` loosely coupled code files (`> 200` lines) or `>= 2` orthogonal research/experiment slices exist.
   * **Bundled Slash Skills**: `/impl`, `/review`, `/dag`, `/research`, `/debate`, `/analyze`.

## Guard Policy (`PreToolUse` `codex-delegate-guard`)

* **`strict` (default)**: Blocks new/overwritten code files and edits `> 10` changed lines unless the user explicitly opts out (`不用codex` / `skip codex` / `你直接改`).
* **`CODEX_GUARD_PROFILE=pragmatic` (stateless opt-in)**: Relaxes single-file edits on existing code files up to `<= 30` changed lines, while still blocking new/overwritten code files and falling back to `strict` (`<= 10` lines) whenever the user invokes `/impl` or `/dag`.
* Path exemptions are restricted strictly to controlled scratch/artifact roots (`/tmp/codex-orchestrator/` and `~/.gemini/antigravity/brain/`).

## DAG Parallelism Gate & Patch Custody (v3.2)

* **Multi-File Code Features (`>= 3` loosely coupled files or `> 200` lines)**:
  1. **Stage 0 (Contract)**: Write `/tmp/codex-orchestrator/<task-id>/contract.md` pinning cross-module signatures, schema/dict keys, CLI args, and remote isolation paths.
  2. **Stage 1 (2–3 Parallel Workers)**: Fan out disjoint modules via [run_codex_parallel.sh](./scripts/run_codex_parallel.sh) (`--manifest <file.json>`). For Git repos with valid `HEAD`, `--mode implement` defaults to managed detached worktrees pinned per checkout to `base_oid`, exports `changes.patch` + `changed_files` via a private `GIT_INDEX_FILE` (`base_oid -> final_tree_oid`, capturing in-worktree commits and respecting `.gitignore`), and verifies `reconstructed_tree_oid == final_tree_oid` before cleaning up any worktree. Dirty checkouts are rejected by default (`--dirty-policy fail`; pass `--dirty-policy head` only to explicitly ignore uncommitted local changes).
  3. **Stage 2 (Integration Worker)**: Apply verified Stage 1 patches ONLY when `status == "ok"` and `merge_ok` is `true` (`base_reconstruct_ok` and `target_apply_ok`), then run integration/test work.
  4. **Stage 3 (Verification ∥ Codex Review)**: Run higher-tier checks concurrently with local `run_codex.sh review --uncommitted --structured`.
* **Multi-Angle Research / Experiment Analysis (`>= 2` slices or `>= 3` options/runs)**:
  1. **Stage 0 (Frozen Corpus)**: Collect web/browser/local/SSH evidence into `/tmp/codex-orchestrator/<task-id>/corpus/` + `sources_index.json`.
  2. **Stage 1 (Parallel Fan-Out)**: Run `run_codex_parallel.sh --mode research|analyze --structured --manifest ...` against `corpus/` (offline by default; optional `--image`, opt-in `--web-search`).
  3. **Stage 2 (Blind Critique)**: Run `run_codex.sh critique --structured` on a neutralized draft to expose P0/P1 flaws.
  4. **Stage 3 (Citation Spot-Check & Decision Brief)**: Spot-check `>= 2` cited `source_ids`/`evidence_location` entries directly, then write a Chinese decision brief.

## Modes, Models, Supervision & Fail-Closed Exit Codes

| Mode | Default Model / Effort | Default Speed | Default `--max-cmds` | Sandbox & `--structured` Schema |
| :--- | :--- | :--- | :--- | :--- |
| `implement` | `gpt-6.2-sol` / `medium` | `ultrafast` | `12` (`0` disables) | `-s workspace-write` -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `consult` | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `-s read-only` -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `review` (`--structured`/`--prompt`/`--spec`/`--image`/`--max-cmds > 0`) | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `read-only-exec` + bounded git snapshot -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `review` (bare target, `--max-cmds 0`) | `gpt-6.2-astra` / `medium` | `fast` | `0` | Native `codex exec review` |
| `resume` | Auto-inherited prior model / `medium` | `ultrafast` (`sol`) / `fast` (`astra`) | `8` (`0` disables) | `codex exec resume` (inherits prior `add_dirs` for `workspace-write`) |
| `research` | `gpt-6.2-astra` / `medium` | `fast` | `10` (`0` disables) | `-s read-only` -> [research-output-schema.json](./references/research-output-schema.json) |
| `critique` | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `-s read-only` -> [critique-output-schema.json](./references/critique-output-schema.json) |
| `analyze` | `gpt-6.2-astra` / `medium` | `fast` | `10` (`0` disables) | `-s read-only` -> [analysis-output-schema.json](./references/analysis-output-schema.json) |
| `--fast` preset | Sets unset options to `gpt-6.2-sol` / `low` / `ultrafast` | `ultrafast` (`sol`) / `fast` (`other`) | Mode default | Never clobbers explicit `--model`/`--effort`/`--speed` |

* **Supervision, Security & Fail-Closed Contract** (see [SPEC.md](../../../SPEC.md)):
  * Streaming JSONL supervision via `codex_orch.supervisor` with monotonic `--timeout <SEC>` (exit `124`), opt-in `--stall-timeout <SEC>` (exit `125`), full process-group cleanup (`SIGTERM` -> `SIGKILL`), child credential stripping (`codex_orch.env`), `0700` artifact dirs, and secret redaction.
  * Preflight rejects unsupported custom `--schema` keywords and self-hosting in-place `implement` edits before creating `--out-dir` (exit `2`).
  * Invalid structured output fails closed (`status: "schema_error"`, exit `3`). Quota/rate-limit failures emit `status: "paused_quota"` (exit `75` or child code; never blind-retry writes).
  * **Bounded Stdout (`--stdout-mode auto|compact|full`)**: Default `auto` prints full `last_message.md` up to `4096` bytes and switches to a UTF-8-safe compact preview with artifact file pointers above `4096` bytes. Always `read_file` `last_message.md` / `summary.json` when you need full details.
  * **Budget Telemetry**: `summary.json` records `invocation_id`, `codex_exit_code`, `budget_exceeded`, `budget_utilization`, `cmd_per_modified_file`, `last_message_bytes`, `stdout_truncated`, and `malformed_event_lines`.
* **Multimodal & Live Search**: `--image <FILE>` (repeatable) attaches plots/screenshots; `--web-search` is opt-in (default OFF via `-c web_search="disabled"`).
* **Anti-Bloat `resume`**: Use `resume` only for short healthy loops (`< 1,000,000` prior `tokens.input`, non-timeout); otherwise spawn a fresh `implement --effort medium --speed ultrafast`.

## Layered Verification & Communication Charter (`v3.1`)

Before replying to the user after any Codex run:

1. **Verify Independently**: Inspect `git status`/`git diff` (or `parallel-summary.json` `merge_ok` + `custody_verified`) and run higher-tier gates for code; confirm `structured_valid: true` and spot-check `>= 2` cited `file:line`/`source_ids` for analysis/review.
2. **Rewrite From Scratch**: Never paste raw Codex `last_message.md` or internal orchestration metadata. Follow the 5 iron rules and 4 task-weight dynamic routes in [user-brief-template.md](./references/user-brief-template.md) (Communication Charter v3.1).

## Invocation Examples

```bash
SCRIPTS=~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts

# 1. Default 1-hop structured implementation (gpt-6.2-sol, compact stdout > 4 KB)
"$SCRIPTS/run_codex.sh" implement \
  --dir "/path/to/repo" --structured --timeout 240 --stall-timeout 120 \
  --spec /tmp/codex-orchestrator/task-1/spec.md \
  --out-dir /tmp/codex-orchestrator/task-1/implement

# 2. Structured deep research on a frozen corpus (+ chart image, full stdout override)
"$SCRIPTS/run_codex.sh" research \
  --dir /tmp/codex-orchestrator/topic-1/corpus --structured --ephemeral \
  --stdout-mode full --image /tmp/codex-orchestrator/topic-1/corpus/benchmark.png \
  --spec /tmp/codex-orchestrator/topic-1/spec-research.md \
  --out-dir /tmp/codex-orchestrator/topic-1/research

# 3. Parallel managed-worktree DAG implementation with patch custody & merge preflight
"$SCRIPTS/run_codex_parallel.sh" \
  --out-dir /tmp/codex-orchestrator/topic-1/stage1 \
  --mode implement --structured --max-parallel 3 --timeout 240 \
  --dir "/path/to/repo" --manifest /tmp/codex-orchestrator/topic-1/manifest.json
```

Never print, inspect, or log `~/.codex/auth.json` or API keys.
