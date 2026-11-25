---
name: dag
description: Run a DAG multi-module parallel Codex workflow with shared contract, auto-injected ownership table, base_oid patch custody, host-side Proof-of-Work, and Chinese brief.
---

# DAG Multi-Module Parallel Codex Workflow (`/dag`)

Act as **Principal Engineering Manager** in Google Antigravity orchestrating zero-conflict multi-module parallelism via `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh`.

Execute strictly in order:

1. **Fold-First Gate & Composition Choice**:
   * Do NOT fan out for tightly coupled shared-state edits or `< 3` files (`<= 200` lines) — fold back to single-hop `/impl`.
   * If modules require active mid-flight back-and-forth negotiation, use native Antigravity `/swarm` (`land-before-kill`) and finish with an external Codex `/review` on the exact landed commit/diff.
   * Otherwise split cleanly into `2–3` disjoint modules with explicit `allowed_paths` / `protected_paths` in `manifest.json` (plus Stage 2 integration if glue files exist).
2. **Stage 0 — Lock Shared Contract & Manifest**:
   * Write `/tmp/codex-orchestrator/<task-id>/contract.md` locking cross-module signatures, schema/dict keys, CLI flags, and verification commands.
   * Write focused `spec-<mod>.md` files (~3–4 KB each) referencing `contract.md` and `/tmp/codex-orchestrator/<task-id>/manifest.json` with per-task `allowed_paths` (and optional `protected_paths`). `run_codex_parallel.sh` automatically injects a public **Parallel Ownership Table** into per-task `effective_spec.md` without modifying source specs.
3. **Stage 1 — Parallel Managed Worktree Execution & Optional Host Proof-of-Work**:
   * Ensure target checkout is clean (default `--dirty-policy fail`; pass `--dirty-policy head` only to explicitly ignore uncommitted local edits).
   * Run directly via `run_command` (pass `--verify-preset` or `--verify-cmd` to verify the combined reconstructed tree via `verification.json`):
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh --out-dir /tmp/codex-orchestrator/<task-id>/stage1 --mode implement --structured --stdout-mode facts --max-parallel 3 --dir "<repo>" --timeout 240 --manifest /tmp/codex-orchestrator/<task-id>/manifest.json`
4. **Patch Custody Preflight & Stage 2 Integration**:
   * Inspect `parallel-summary.json` for status, mergeability, custody, and patch paths. With `--structured` and `--stdout-mode facts`, inspect `git diff --stat` and key hunks for each patch and read host `verification.json` before `last_message.md`. Audit each Spec `Done When` item and mark missing diff or test proof as unverified.
   * Only when top-level `status == "ok"` **and** `merge_ok` is `true`, apply the clean `changes.patch` files (`git apply`) and run Stage 2 integration on glue files.
5. **Stage 3 — Parallel Verification & Diff Review**:
   * Run higher-tier / remote GPU tests (outside Codex) concurrently with local `run_codex.sh review --uncommitted --structured`.
6. **Chinese Executive Brief**:
   * Synthesize verified architecture, behavioral/contract change map, verification watermark, and real pending gates/decisions in calm, precise natural Chinese following the **heavyweight engineering delivery route** of the Communication Charter v3.2 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`.
