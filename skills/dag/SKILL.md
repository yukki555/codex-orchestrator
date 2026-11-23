---
name: dag
description: Run a DAG multi-module parallel Codex workflow with shared contract, managed worktrees, patch preflight, and Chinese brief.
---

# DAG Multi-Module Parallel Codex Workflow (`/dag`)

Act as **Principal Engineering Manager** in Antigravity CLI (`agy`) orchestrating zero-conflict multi-module parallelism via `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh`.

Execute strictly in order:

1. **Dependency Analysis & Disjoint Split**:
   * Inspect only the relevant entry files via `read_file` and split implementation into `2–3` disjoint modules (no shared files across Stage 1 workers) plus `1` Stage 2 integration task if glue files (`train.py`, CLI entrypoints) exist.
2. **Stage 0 — Lock Shared Contract & Manifest**:
   * Write `/tmp/codex-orchestrator/<task-id>/contract.md` locking cross-module signatures, schema/dict keys, CLI flags, checkpoint formats, and isolated remote test directories.
   * Write focused `spec-<mod>.md` files (~3–4 KB each) referencing `contract.md`, plus `/tmp/codex-orchestrator/<task-id>/manifest.json` (use default `medium` + `fast` tier for routine modules; escalate to `effort: "high"` only for core algorithmic modules).
3. **Stage 1 — Parallel Managed Worktree Execution**:
   * Run directly via `run_command`:
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh --out-dir /tmp/codex-orchestrator/<task-id>/stage1 --mode implement --structured --max-parallel 3 --dir "<repo>" --timeout 240 --manifest /tmp/codex-orchestrator/<task-id>/manifest.json`
4. **Merge Preflight & Stage 2 Integration**:
   * Inspect `parallel-summary.json` (`merge_ok`, `merge_check`, per-task `changed_files` & `patch_file`).
   * If `merge_ok` is `true`, apply the clean `changes.patch` files (`git apply`), then run the Stage 2 integration worker on glue files. If `merge_ok` is `false`, resolve `overlapping_files` before merging.
5. **Stage 3 — Parallel Verification & Diff Review**:
   * Once local/CPU unit checks pass, run remote GPU/E2E tests (executed by `agy` / `codex-worker`, never inside Codex) concurrently with local `run_codex.sh review --uncommitted --structured`.
6. **Chinese Executive Brief**:
   * Synthesize the verified architecture, patch map, `merge_check` + test results, and remaining decisions in natural Chinese following `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`.
