---
name: codex-orchestrator
description: >-
  Orchestrate local OpenAI Codex CLI (codex exec) directly via run_shell_command or
  via @codex-worker using run_codex.sh / run_codex_parallel.sh for local coding, DAG
  parallel multi-module implementation, debugging, test authoring, and independent
  code reviews. Always use for new local code files, edits >10 lines, and Codex QA.
---

# Codex Orchestrator (Gemini CLI Edition, v2.1)

Operate a **Manager + High-Execution Implementer** workflow: **Gemini CLI** acts as the **Principal Engineering Manager & Decision-Maker**, while local **Codex CLI** acts as the **High-Execution Staff Implementer & Independent Reviewer**.

## Role Split & Delegate Guard

* **Gemini CLI (Principal Engineering Manager & Decision-Maker)**:
  * Explores context via `read_file` / search, aligns architecture and tradeoffs with the user, locks shared interface contracts & GPT-6.2-tuned Specs under `/tmp/codex-orchestrator/<task-id>/`, orchestrates `run_codex.sh` / `run_codex_parallel.sh` directly via `run_shell_command` (or via `@codex-worker` when isolating noisy logs or remote SSH checks), verifies diffs/tests, and rewrites raw evidence into human-readable review briefs.
  * **Guardrail**: The extension's `BeforeAgent` + `BeforeTool` hooks (`codex-delegate-guard`) block Gemini CLI from creating new code files via `write_file` or making `> 10`-line logic-code edits via `replace` unless the user explicitly asks to skip Codex in that turn (`不用codex` / `skip codex` / `你直接改`).
* **Codex CLI (High-Execution Local Implementer & Reviewer)**:
  * Executes local coding (`implement`), structured diagnostics (`consult`), and Git QA (`review`), continuing on short threads via `resume` or fresh targeted runs when needed.
  * **Local-Only Boundary**: Keep Codex local to code/static/unit checks without `network_access=true` SSH loops; let Gemini CLI (`run_shell_command`) or `@codex-worker` run remote SSH syncs and GPU/PyTorch tests.

## Gemini CLI Direct vs `@codex-worker` Subagent Routing

* **Direct `run_shell_command` invocation (Preferred for low latency)**: Ideal in Gemini CLI for single-turn `consult`, `review --structured`, clean 1-shot `implement`, or `run_codex_parallel.sh --manifest` DAG fan-outs (zero subagent hop overhead).
* **`@codex-worker` local subagent**: Use when you want to isolate multi-round repair loops (`<= 2` rounds), SHA-256 remote SSH syncs, and verbose GPU/PyTorch test logs out of the main Manager conversation.
* **Custom Slash Shortcuts**: `/codex:impl` (single implementation), `/codex:review` (structured diff QA), `/codex:dag` (multi-module DAG parallel workflow).

## DAG Parallelism Gate (Multi-File Features)

When `implement` touches **>= 3 loosely coupled files** or **> 200 lines**, split into a conflict-free DAG rather than a single monolithic `600s` thread:

1. **Stage 0 (Contract)**: Write `/tmp/codex-orchestrator/<task-id>/contract.md` pinning shared function signatures, dict/schema keys, CLI args, and remote isolation paths.
2. **Stage 1 (2–3 Parallel Workers)**: Fan out disjoint modules with `--timeout 240–300` via [run_codex_parallel.sh](./scripts/run_codex_parallel.sh) (supports `--task` or `--manifest <file.json>` with per-task `model`/`effort`/`speed`/`add_dirs`).
   * In Git repos with valid `HEAD`, `--mode implement` defaults to **managed detached worktrees**, exports `changes.patch` (including new untracked files via `git add -N`), cleans up passing worktrees automatically (`--keep-worktrees` retains them), and runs cross-worker `merge_check` (working-tree `git apply --check` + sequential stacked apply verification in a throwaway worktree).
   * Pass `--shared-dir` only when file ownership is 100% disjoint and zero merge step is desired.
3. **Stage 2 (Integration Worker)**: Apply verified Stage 1 patches (`git apply`), then run one integration worker on glue files (`train.py` / scripts).
4. **Stage 3 (Parallel GPU Test ∥ Codex Review)**: Run remote GPU/E2E validation concurrently with local `run_codex.sh review --structured`.

## Modes, Model, Effort & Decoupled Speed Routing

Speed tier (`--speed <default|fast|ultrafast|inherit>`) is **orthogonal** to model and reasoning effort:

| Mode | Default Model / Effort | Default Speed (`service_tier`) | Default `--max-cmds` | Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| `implement` | `gpt-6.2-sol` / `medium` | `fast` (because `medium`) | `12` (`0` disables) | `-s workspace-write` |
| `consult` | `gpt-6.2-astra` / `medium` | `fast` (because `medium`) | `8` (`0` disables) | `-s read-only` |
| `review` (with `--structured`/`--prompt`/`--spec`/`--max-cmds > 0`) | `gpt-6.2-astra` / `medium` | `fast` (because `medium`) | `8` (`0` disables) | `read-only-exec` with bounded git stat snapshot + JSON schema |
| `review` (bare target, `--max-cmds 0` or omitted w/o prompt/schema) | `gpt-6.2-astra` / `medium` | `fast` (because `medium`) | `0` | Native `codex exec review` |
| `resume` | Auto-inherited prior model / `medium` | `fast` (because `medium`) | `8` (`0` disables) | `codex exec resume` (inherits prior `add_dirs` for `workspace-write`) |
| `--fast` preset | Sets unset options to `gpt-6.2-sol` / `low` / `fast` | `fast` | Mode default | Never clobbers explicit `--model`/`--effort`/`--speed` |

* **Speed Defaults**: Effective effort `low` or `medium` automatically uses `service_tier="fast"` (`2x` speed). `high`, `xhigh`, `max`, `ultra` default to `default` (add `--speed fast` explicitly when speed matters; `--speed default` forces standard tier on `low`/`medium`). `ultrafast` works on `gpt-6.2-sol` and auto-downgrades to `fast` with a warning on `gpt-6.2-astra`.
* **Rules & Config Isolation**: Both `--ignore-user-config` and `--ignore-rules` default to **enabled** (preserving `~/.codex` login while skipping `~/.codex/config.toml` and `~/.codex/rules`); `--use-user-config` / `--use-rules` automatically fall back and retry if blocked.
* **`--structured` & Telemetry**: Enforces [codex-output-schema.json](./references/codex-output-schema.json), records `structured_valid`, `modified_files`, and on failure/timeout extracts `partial_progress` (`modified_files`, last 3 `agent_message`s, failed commands, in-flight commands) directly into `summary.json` and stdout.

## Context Hygiene & Anti-Bloat Rules

* **Anti-Bloat `resume` Threshold**: Reserve `resume` for immediate short loops (`< 1,000,000` prior `tokens.input`, non-timeout). When applying post-review `file:line` fixes or recovering from a huge/timed-out run, spawn a **fresh `implement --effort medium --speed fast`** run instead of resuming multi-million-token history.
* **Environment Facts in Spec**: List local runtime limitations (e.g., "macOS host has no PyTorch/zarr; do NOT probe disk or read `~/.codex/skills/*`") in `## Environment & Tooling Facts` so Codex skips wasted discovery commands.

## Layered Verification & Human-Readable Brief ("说人话" Gate)

Before replying to the user after any Codex run:

1. **Diff & Scope Check (`implement`)**: Inspect `git status` / `git diff` (or `parallel-summary.json` `merge_ok` + patches) to confirm zero scope drift.
2. **Layered Verification (No Redundant Reruns)**:
   * If `@codex-worker` (or Codex self-check) already verified unit tests and remote SHA-256 sync with real command outputs, Manager **does not** re-run identical CPU unit tests — instead spot-check 1–2 code edits (`read_file`) and run higher-tier gates (multi-GPU smoke / E2E).
   * For `consult` / `review`, spot-check at least 1–2 cited `file:line` claims directly and label verified facts vs hypotheses.
3. **Human-Readable Brief Rewrite**:
   * **Hard Ban**: Never paste, echo, or mechanically translate Codex's raw `last_message.md` text/JSON to the user.
   * Rewrite from scratch in clear, natural Chinese following [user-brief-template.md](./references/user-brief-template.md).

## Invocation Examples

```bash
SCRIPTS=~/.gemini/extensions/codex-orchestrator/skills/codex-orchestrator/scripts

# 1. Default medium+fast structured implementation
"$SCRIPTS/run_codex.sh" implement \
  --dir "/path/to/repo" --structured --timeout 240 \
  --spec /tmp/codex-orchestrator/task-1/spec.md \
  --out-dir /tmp/codex-orchestrator/task-1/implement

# 2. High-stakes consult (effort=high, optional --speed fast)
"$SCRIPTS/run_codex.sh" consult \
  --dir "/path/to/repo" --effort high --speed fast --ephemeral --structured \
  --spec /tmp/codex-orchestrator/task-2/spec.md \
  --out-dir /tmp/codex-orchestrator/task-2/consult

# 3. Single-pass structured Git diff review (routes cleanly via read-only exec)
"$SCRIPTS/run_codex.sh" review \
  --dir "/path/to/repo" --uncommitted --structured --max-cmds 8 \
  --prompt "Check regressions, edge cases, and backward compatibility." \
  --out-dir /tmp/codex-orchestrator/task-3/review

# 4. Short-loop resume (auto-inherits prior model and --add-dir paths)
"$SCRIPTS/run_codex.sh" resume \
  --dir "/path/to/repo" --id "<thread_id>" --sandbox workspace-write --structured \
  --prompt "Tests failed on case X with error Y. Fix within allowed scope and re-verify." \
  --out-dir /tmp/codex-orchestrator/task-1/resume-1

# 5. Parallel managed-worktree implementation + patch conflict preflight
"$SCRIPTS/run_codex_parallel.sh" \
  --out-dir /tmp/codex-orchestrator/task-4/parallel-impl \
  --mode implement --structured --max-parallel 3 --dir "/path/to/repo" \
  --timeout 240 --manifest /tmp/codex-orchestrator/task-4/manifest.json
```

Never print, inspect, or log `~/.codex/auth.json` or API keys.
