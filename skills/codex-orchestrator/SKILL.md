---
name: codex-orchestrator
description: >-
  Orchestrate local OpenAI Codex CLI (codex exec) via codex-worker subagents or
  run_codex.sh / run_codex_parallel.sh across local coding, DAG parallel multi-module
  implementation, Git review, multi-model research, blind red-team debate, and experiment data
  analysis. Always use for new local code files, edits >10 lines, Codex QA, and /research /debate /analyze.
---

# Codex Orchestrator (v3.1)

Operate a **General-Purpose Manager + High-Execution Specialist** workflow in Google Antigravity: Gemini acts as **Principal Manager, Collector & Decision-Maker**, while local **Codex CLI** acts as **High-Execution Staff Implementer, Independent Reviewer & Red-Team Analyst**.

## Role Split, Entry Points & Delegate Guard

* **Gemini (Principal Manager, Collector & Decision-Maker)**:
  * Explores local context (`read_file` / search), collects web/remote evidence into frozen corpora (`/tmp/codex-orchestrator/<task-id>/corpus/`), aligns tradeoffs with the user (`/plan`, `/grill-me`), locks shared contracts & GPT-6.2-tuned Specs, orchestrates `codex-worker` subagents or `run_codex.sh` / `run_codex_parallel.sh`, performs layered verification & citation spot-checks, and rewrites raw evidence into human-readable Chinese briefs.
  * **Guardrail**: The plugin's root `hooks.json` (`PreToolUse` `codex-delegate-guard`) blocks Gemini from creating new code files or making `> 10`-line logic edits directly unless the user explicitly asks to skip Codex (`不用codex` / `skip codex` / `你直接改`). Read-only research/debate/analysis tasks are **not** hook-blocked; they use tiered soft routing (`/research`, `/debate`, `/analyze`, or auto-upgrade on >=3-option trade studies / high-stakes decisions).
* **`codex-worker` Subagent vs Direct `run_command`**:
  * Delegate to `codex-worker` when isolating multi-round repair loops (`<= 2` rounds), SHA-256 remote SSH syncs, and noisy GPU/PyTorch test logs out of the main conversation.
  * Call `run_codex.sh` / `run_codex_parallel.sh` directly via `run_command` for fast single-turn `consult`, `review --structured`, `research`, `critique`, `analyze`, clean 1-shot `implement`, or `--manifest` parallel fan-outs.
* **Bundled Slash Skills**:
  * Engineering: `/impl` (single implementation), `/review` (structured diff QA), `/dag` (multi-module DAG parallel workflow).
  * General / Research: `/research` (frozen-corpus parallel research + blind critique), `/debate` (blind red-team decision debate), `/analyze` (paper/code/experiment consistency analysis).

## DAG Parallelism Gate (Code & Research)

* **Multi-File Code Features (`>= 3` loosely coupled files or `> 200` lines)**:
  1. **Stage 0 (Contract)**: Write `/tmp/codex-orchestrator/<task-id>/contract.md` pinning cross-module signatures, schema/dict keys, CLI args, and remote isolation paths.
  2. **Stage 1 (2–3 Parallel Workers)**: Fan out disjoint modules with `--timeout 240–300` via [run_codex_parallel.sh](./scripts/run_codex_parallel.sh) (`--task` or `--manifest <file.json>` with per-task `model`/`effort`/`speed`/`add_dirs`/`images`/`web_search`/`schema`). In Git repos with valid `HEAD`, `--mode implement` defaults to managed detached worktrees + `changes.patch` (including new untracked files via `git add -N`) + stacked `merge_check` (`--shared-dir` skips worktrees; `--keep-worktrees` retains them).
  3. **Stage 2 (Integration Worker)**: Apply verified Stage 1 patches (`git apply`), then run one integration worker on glue files.
  4. **Stage 3 (Parallel Verification ∥ Codex Review)**: Run remote GPU/E2E tests concurrently with local `run_codex.sh review --structured`.
* **Multi-Angle Research / Experiment Analysis (`>= 2` slices or `>= 3` options/runs)**:
  1. **Stage 0 (Frozen Corpus)**: Gemini gathers web/browser/local/SSH materials into `/tmp/codex-orchestrator/<task-id>/corpus/` + `sources_index.json`.
  2. **Stage 1 (Heterogeneous Parallel Fan-out)**: Run `run_codex_parallel.sh --mode research|analyze --structured --manifest ...` against `corpus/` (works in non-Git directories via `shared-dir`, optional `--image` plots & opt-in `--web-search`) alongside optional Antigravity `research`/`browser` subagents.
  3. **Stage 2 (Blind Critique)**: Run `run_codex.sh critique --structured` on a neutralized draft to expose P0/P1 flaws and missing alternatives.
  4. **Stage 3 (Citation Spot-Check & Decision Brief)**: Spot-check 2–3 core `source_ids`/`evidence_location` entries directly, then write a Chinese decision brief.

## Modes, Models, Schemas & Decoupled Speed Routing

Speed tier (`--speed <default|fast|ultrafast|inherit>`) is **orthogonal** to model and reasoning effort:

| Mode | Default Model / Effort | Default Speed | Default `--max-cmds` | Sandbox & `--structured` Schema |
| :--- | :--- | :--- | :--- | :--- |
| `implement` | `gpt-6.2-sol` / `medium` | `ultrafast` | `12` (`0` disables) | `-s workspace-write` -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `consult` | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `-s read-only` -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `review` (`--structured`/`--prompt`/`--spec`/`--image`/`--max-cmds > 0`) | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `read-only-exec` + bounded git snapshot -> [codex-output-schema.json](./references/codex-output-schema.json) |
| `review` (bare target, `--max-cmds 0` or omitted w/o prompt/schema/image) | `gpt-6.2-astra` / `medium` | `fast` | `0` | Native `codex exec review` |
| `resume` | Auto-inherited prior model / `medium` | `ultrafast` (`sol`) / `fast` (`astra`) | `8` (`0` disables) | `codex exec resume` (inherits prior `add_dirs` for `workspace-write`) |
| `research` | `gpt-6.2-astra` / `medium` | `fast` | `10` (`0` disables) | `-s read-only` -> [research-output-schema.json](./references/research-output-schema.json) |
| `critique` | `gpt-6.2-astra` / `medium` | `fast` | `8` (`0` disables) | `-s read-only` -> [critique-output-schema.json](./references/critique-output-schema.json) |
| `analyze` | `gpt-6.2-astra` / `medium` | `fast` | `10` (`0` disables) | `-s read-only` -> [analysis-output-schema.json](./references/analysis-output-schema.json) |
| `--fast` preset | Sets unset options to `gpt-6.2-sol` / `low` / `ultrafast` | `ultrafast` (`sol`) / `fast` (`other`) | Mode default | Never clobbers explicit `--model`/`--effort`/`--speed` |

* **Multimodal & Live Search Flags**:
  * `--image <FILE>` (repeatable): Attaches PNG/JPG charts, diagrams, or screenshots (`-i <FILE>`) to `consult`, `implement`, `research`, `critique`, `analyze`, `resume`, or `review` (auto-routes `review` via `read-only-exec`). Supported in `run_codex_parallel.sh` via `--image` or per-task manifest `"images": [...]`.
  * `--web-search`: Opt-in flag (default **OFF** with explicit `-c web_search="disabled"` to keep runs reproducible on local/frozen corpora). When enabled, invokes `codex --search exec ...` so Codex can call the native Responses `web_search` tool. Supported in `run_codex_parallel.sh` via `--web-search` or per-task manifest `"web_search": true`.
* **Speed Defaults**: Effective effort `low` or `medium` (and `--fast`) automatically uses `service_tier="ultrafast"` on `gpt-6.2-sol` and `service_tier="fast"` on `gpt-6.2-astra`/other models. `high`, `xhigh`, `max`, `ultra` default to `default` (pass `--speed ultrafast` explicitly on `gpt-6.2-sol` or `--speed fast` on either model when speed matters; `--speed default` forces standard tier on `low`/`medium`). `ultrafast` on `gpt-6.2-astra` auto-downgrades to `fast` with a warning.
* **Rules & Config Isolation**: Both `--ignore-user-config` and `--ignore-rules` default to **enabled** (preserving `~/.codex` login while skipping `~/.codex/config.toml` and `~/.codex/rules`); `--use-user-config` / `--use-rules` automatically fall back and retry if blocked.
* **`--structured` & Telemetry**: Enforces and recursively validates the mode's bundled strict JSON Schema (or custom `--schema <FILE>`, including per-task `schema` in parallel manifests), records `structured_valid`, `web_search`, `images`, `schema_file`, and `modified_files` in `summary.json` (printing `structured`, `web_search`, `structured_valid`, and `partial_progress` to stdout).

## Context Hygiene & Anti-Bloat Rules

* **Anti-Bloat `resume` Threshold**: Reserve `resume` for immediate short loops (`< 1,000,000` prior `tokens.input`, non-timeout). When applying post-review `file:line` fixes or recovering from a huge/timed-out run, spawn a **fresh `implement --effort medium --speed ultrafast`** run instead of resuming multi-million-token history.
* **Environment & Corpus Facts in Spec**: State local runtime limitations and corpus index paths explicitly so Codex skips wasted discovery commands.

## Layered Verification & Human-Readable Brief ("说人话" Gate)

Before replying to the user after any Codex run:

1. **Diff & Scope Check (`implement`)**: Inspect `git status` / `git diff` (or `parallel-summary.json` `merge_ok` + patches) to confirm zero scope drift.
2. **Layered Verification / Citation Spot-Checks**:
   * Code: If `codex-worker` (or Codex self-check) already verified unit tests and remote SHA-256 sync with real command outputs, Manager does not re-run identical CPU unit tests — instead spot-check 1–2 code edits (`read_file`) and run higher-tier gates (multi-GPU smoke / E2E).
   * Consult / Review / Research / Critique / Analyze: Confirm `structured_valid: true` and spot-check at least `2` cited `file:line`, `source_ids`, or raw metric locations directly, labeling verified facts vs inferences and `knowledge_gaps`.
3. **Human-Readable Brief Rewrite (Communication Charter v3.1)**:
   * **Hard Ban**: Never paste, echo, or mechanically translate Codex's raw `last_message.md` text/JSON or internal orchestration metadata to the user.
   * Rewrite from scratch in calm, precise, natural Chinese following the 5 iron rules and 4 task-weight dynamic routes (`consult`, lightweight code change, heavyweight engineering / `review`, and `research`/`critique`/`analyze`) in [user-brief-template.md](./references/user-brief-template.md).

## Invocation Examples

```bash
SCRIPTS=~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts

# 1. Default medium+ultrafast structured implementation (gpt-6.2-sol)
"$SCRIPTS/run_codex.sh" implement \
  --dir "/path/to/repo" --add-dir "/path/to/tests" \
  --structured --timeout 240 \
  --spec /tmp/codex-orchestrator/task-1/spec.md \
  --out-dir /tmp/codex-orchestrator/task-1/implement

# 2. Structured deep research on a frozen non-Git corpus (+ optional chart image)
"$SCRIPTS/run_codex.sh" research \
  --dir /tmp/codex-orchestrator/topic-1/corpus --structured --ephemeral \
  --image /tmp/codex-orchestrator/topic-1/corpus/benchmark.png \
  --spec /tmp/codex-orchestrator/topic-1/spec-research.md \
  --out-dir /tmp/codex-orchestrator/topic-1/research

# 3. Blind red-team critique with high reasoning effort + fast tier
"$SCRIPTS/run_codex.sh" critique \
  --dir /tmp/codex-orchestrator/debate-1 --structured --effort high --speed fast --ephemeral \
  --spec /tmp/codex-orchestrator/debate-1/spec-critique.md \
  --out-dir /tmp/codex-orchestrator/debate-1/critique

# 4. Experiment & code consistency analysis with plots
"$SCRIPTS/run_codex.sh" analyze \
  --dir "/path/to/repo" --structured --image /tmp/codex-orchestrator/exp-1/loss.png \
  --spec /tmp/codex-orchestrator/exp-1/spec-analyze.md \
  --out-dir /tmp/codex-orchestrator/exp-1/analyze

# 5. Parallel heterogeneous research + critique manifest (or parallel worktree implement)
"$SCRIPTS/run_codex_parallel.sh" \
  --out-dir /tmp/codex-orchestrator/topic-1/parallel \
  --mode research --structured --max-parallel 3 \
  --dir /tmp/codex-orchestrator/topic-1/corpus --timeout 240 \
  --manifest /tmp/codex-orchestrator/topic-1/manifest.json
```

Never print, inspect, or log `~/.codex/auth.json` or API keys.
