---
name: research
description: >-
  Run a 4-stage multi-model deep research workflow: freeze a verified corpus, fan out parallel
  Codex research workers with structured citations, run optional blind critique, spot-check sources,
  and deliver a clear Chinese decision brief.
---

# Deep Multi-Model Research Workflow (`/research`)

Act as **Principal Research Manager** in Google Antigravity orchestrating **Gemini (breadth collection, synthesis, source spot-checking) + Codex `gpt-6.2-astra` (deep corpus extraction, structured evidence, blind critique)**.

Execute strictly in order:

1. **Stage 0 — Scope Alignment & Frozen Corpus (`corpus/`)**:
   * Clarify comparison dimensions, decision goal, and time/scope boundaries with the user if ambiguous (lightweight 1–2 step factual lookups do NOT need `/research` — answer directly).
   * Split the question into `2–3` orthogonal research slices (e.g., *mechanism & theory*, *engineering tradeoffs & cost*, *ecosystem & failure modes*).
   * Gather primary sources using Gemini's native `search_web`, `browser` subagent, `read_file`, or external SSH fetches, and save normalized snapshots into `/tmp/codex-orchestrator/<task-id>/corpus/` along with `sources_index.json` (`source_id`, `title`, `url_or_path`, `fetched_at`, `notes`).
   * Keep Codex **offline on the frozen `corpus/` by default** for reproducible, traceable citations; pass `--web-search` only when live external verification inside Codex is explicitly desired.
2. **Stage 1 — Heterogeneous Parallel Deep-Dive**:
   * Author focused slice Specs (`/tmp/codex-orchestrator/<task-id>/spec-<slice>.md`) using `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/research-spec-template.md` and a `manifest.json`.
   * Run directly via `run_command`:
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex_parallel.sh --out-dir /tmp/codex-orchestrator/<task-id>/stage1 --mode research --structured --max-parallel 3 --dir /tmp/codex-orchestrator/<task-id>/corpus --timeout 240 --manifest /tmp/codex-orchestrator/<task-id>/manifest.json`
   * Optionally run Antigravity `research` or `browser` subagents in parallel for live interactive pages or community counter-examples.
   * For single-slice research, invoke `run_codex.sh research --dir "<corpus_or_repo>" --structured --timeout 240 --spec ...` directly (pass `--image <file>` when charts/figures matter).
3. **Stage 2 — Blind Red-Team Cross-Check (Recommended for High-Stakes Decisions)**:
   * Write a neutral synthesis draft (`/tmp/codex-orchestrator/<task-id>/draft.md`, stripping author identity and leading bias) and run one independent Codex `critique` pass (`run_codex.sh critique --dir /tmp/codex-orchestrator/<task-id> --structured --timeout 180 --spec ...`) to surface P0/P1 blind spots, stale data, and missing alternatives.
4. **Stage 3 — Source Spot-Check & Chinese Decision Brief**:
   * Verify `structured_valid: true` in `summary.json` / `parallel-summary.json`.
   * Spot-check at least `2–3` core `claims` against their cited `source_ids` / `quote_or_location` directly via `read_file` (or source URL) to eliminate misquoting or hallucinated citations.
   * Rewrite into a decision-ready Chinese brief following the **deep research / red-team / analysis route** of the Communication Charter v3.1 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (upfront verdict + evidence watermark -> prioritized conflicts & comparison matrix with inline `source_id` anchors -> verified facts vs derivations vs open hypotheses -> non-redundant next steps). Never paste raw Codex JSON/prose.
