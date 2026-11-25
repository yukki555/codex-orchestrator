---
name: research
description: >-
  Run a 4-stage multi-model deep research workflow: fold-first gate, freeze a verified corpus,
  fan out parallel Codex research workers (CLI or /run-workflow template) with structured
  citations, optional blind critique, spot-check sources, and deliver a clear Chinese brief.
---

# Deep Multi-Model Research Workflow (`/research`)

Act as **Principal Research Manager** in Google Antigravity orchestrating **Gemini (breadth collection, synthesis, source spot-checking) + Codex `gpt-6.2-astra` (deep corpus extraction, structured evidence, blind critique)**.

Execute strictly in order:

1. **Stage 0 — Fold-First Gate, Scope Alignment & Frozen Corpus (`corpus/`)**:
   * **Fold-First Gate**: Lightweight single-fact lookups or 1–2 tool-call comparisons do NOT justify ~15× multi-agent token overhead — answer directly in the main loop. Fan out only when there are `>= 2` orthogonal research slices or `>= 3` multi-dimensional options.
   * Gather primary sources using Gemini's `search_web`, `browser` subagent, `read_file`, or external SSH fetches into `/tmp/codex-orchestrator/<task-id>/corpus/` along with `sources_index.json`.
   * Keep Codex **offline on the frozen `corpus/` by default**; pass `--web-search` only when live search inside Codex is explicitly desired.
2. **Stage 1 — Heterogeneous Parallel Deep-Dive**:
   * **Headless CLI batch (default)**: Author slice Specs + `manifest.json` and run `run_codex_parallel.sh --out-dir /tmp/codex-orchestrator/<task-id>/stage1 --mode research --structured --max-parallel 3 --dir /tmp/codex-orchestrator/<task-id>/corpus --timeout 240 --manifest /tmp/codex-orchestrator/<task-id>/manifest.json`.
   * **Native Antigravity `/run-workflow` card UI (optional for interactive read-only slices)**: Run `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/workflows/research_parallel.py` via `run_workflow(ScriptPath=..., Args={"corpus_dir": ..., "slices": [...], "out_dir": ..., "run_critique": true})`.
   * For single-slice research, invoke 1-hop `run_codex.sh research --dir "<corpus_or_repo>" --structured --timeout 240 --spec ...` directly (read `last_message.md` if stdout was compacted).
3. **Stage 2 — Blind Red-Team Cross-Check (Recommended for High-Stakes Decisions)**:
   * Write a neutral synthesis draft (`/tmp/codex-orchestrator/<task-id>/draft.md`) and run one independent Codex `critique` pass (`run_codex.sh critique --dir /tmp/codex-orchestrator/<task-id> --structured --timeout 180 --spec ...`).
4. **Stage 3 — Source Spot-Check & Chinese Decision Brief**:
   * Verify `status == "ok"` and `structured_valid: true` in `summary.json` / `parallel-summary.json`.
   * Spot-check at least `2–3` core `claims` against their cited `source_ids` / `quote_or_location` directly via `read_file` (or source URL).
   * Rewrite into a decision-ready Chinese brief following the **deep research / red-team / analysis route** of the Communication Charter v3.2 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`. Never paste raw Codex JSON/prose.

Apply the Charter’s Synthesis Firewall. Preserve exact evidence anchors and measured values, rewrite working shorthand and internal codes as self-contained Chinese, and disclose gaps. Routine `consult` defaults to `gpt-6.2-sol`. Deep `research`, `critique`, and `analyze` default to `gpt-6.2-astra`.
