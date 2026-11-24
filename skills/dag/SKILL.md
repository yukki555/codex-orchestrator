---
name: dag
description: Run a DAG multi-module parallel Codex workflow with shared contract, base_oid patch custody, atomic merge preflight, and Chinese brief.
---

# DAG Multi-Module Parallel Codex Workflow (`/dag`)

Act as **Principal Engineering Manager** in Google Antigravity orchestrating zero-conflict multi-module parallelism via `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh`.

Execute strictly in order:

1. **Fold-First Gate & Disjoint Split**:
   * Do NOT fan out for tightly coupled shared-state edits or `< 3` files (`<= 200` lines) — fold back to single-hop `/impl` to avoid unnecessary multi-agent token amplification.
   * Inspect only the relevant entry files via `read_file` and split implementation into `2–3` disjoint modules (no shared files across Stage 1 workers) plus `1` Stage 2 integration task if glue files exist.
2. **Stage 0 — Lock Shared Contract & Manifest**:
   * Write `/tmp/codex-orchestrator/<task-id>/contract.md` locking cross-module signatures, schema/dict keys, CLI flags, checkpoint formats, and isolated remote test directories.
   * Write focused `spec-<mod>.md` files (~3–4 KB each) referencing `contract.md`, plus `/tmp/codex-orchestrator/<task-id>/manifest.json` (use default `medium` + `ultrafast` tier on `gpt-6.2-sol` for routine modules; escalate to `effort: "high"` only for core algorithmic modules).
3. **Stage 1 — Parallel Managed Worktree Execution**:
   * Ensure target checkout is clean (default `--dirty-policy fail`; pass `--dirty-policy head` only when intentionally ignoring uncommitted local edits).
   * Run directly via `run_command` (or via `codex-worker` when absorbing multi-round repairs):
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh --out-dir /tmp/codex-orchestrator/<task-id>/stage1 --mode implement --structured --max-parallel 3 --dir "<repo>" --timeout 240 --manifest /tmp/codex-orchestrator/<task-id>/manifest.json`
4. **Patch Custody Preflight & Stage 2 Integration**:
   * Inspect `parallel-summary.json` (`status`, `merge_ok`, `merge_check[*].base_reconstruct_ok`, `target_apply_ok`, per-task `custody_verified`, `changed_files`, `patch_file`).
   * Only when top-level `status == "ok"` **and** `merge_ok` is `true`, apply the clean `changes.patch` files (`git apply`) and run the Stage 2 integration worker on glue files. If any worker failed, hit `paused_quota`, failed custody check, or `merge_ok` is `false`, resolve the failure before applying any patch.
5. **Stage 3 — Parallel Verification & Diff Review**:
   * Once local/CPU unit checks pass, run higher-tier / remote GPU tests (outside Codex) concurrently with local `run_codex.sh review --uncommitted --structured`.
6. **Chinese Executive Brief**:
   * Synthesize the verified architecture, behavioral/contract change map, verification watermark, and real pending gates/decisions in calm, precise natural Chinese following the **heavyweight engineering delivery route** of the Communication Charter v3.1 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (avoid cross-section repetition or raw orchestration metadata).
