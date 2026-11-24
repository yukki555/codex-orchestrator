---
name: analyze
description: >-
  Run structured experiment, data, and paper-code consistency analysis: collect local or remote
  logs and figures into a read-only corpus, delegate quantitative verification to Codex analyze
  mode, spot-check numbers, and deliver a Chinese brief.
---

# Experiment, Data & Paper-Code Consistency Analysis (`/analyze`)

Act as **Principal Research & Systems Analyst** orchestrating **Gemini (remote/local log & figure collection, synthesis)** + **Codex `gpt-6.2-astra` (`analyze` read-only quantitative audit)** to verify whether theory/papers, source code, and observed experiment metrics actually agree.

Execute strictly in order:

1. **Collect Artifacts & Freeze Read-Only Corpus**:
   * Keep Codex **local-only**: if metrics, training logs, checkpoints metadata, or plots live on remote SSH/GPU hosts, Gemini (or `codex-worker`) fetches compact excerpts/CSVs/JSONs/PNGs into `/tmp/codex-orchestrator/<task-id>/corpus/` first.
   * Record provenance (host, path, run ID, commit hash, timestamp) in `/tmp/codex-orchestrator/<task-id>/corpus/sources_index.json`.
2. **Author Analysis Spec & Dispatch `analyze`**:
   * Write `/tmp/codex-orchestrator/<task-id>/spec-analyze.md` using the `analyze` section of `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/research-spec-template.md`.
   * Invoke directly via `run_command` (attach training curves or plots with repeatable `--image <FILE>` whenever visual evidence helps):
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh analyze --dir "<workspace_or_corpus>" --structured --timeout 240 --spec /tmp/codex-orchestrator/<task-id>/spec-analyze.md --out-dir /tmp/codex-orchestrator/<task-id>/analyze`
   * When comparing `>= 3` runs, ablations, or modules, fan out via `run_codex_parallel.sh --mode analyze --structured --manifest ...`.
3. **Spot-Check Calculations & Provenance**:
   * Confirm `structured_valid: true` and inspect `key_metrics`, `consistency_checks`, `anomalies_and_root_causes`, and `verification` exit codes in `last_message.md`.
   * Independently spot-check `1–2` critical numbers or mismatch locations (`evidence_location`) against the raw log or source file via `read_file` / compact command.
4. **Chinese Analytical Brief**:
   * Present an executive Chinese brief following the **deep research / red-team / analysis route** of the Communication Charter v3.1 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (upfront verdict + evidence watermark -> Expected vs Code vs Observed table with inline anchors -> P0/P1 discrepancies -> verified facts vs derivations vs unverified hypotheses -> prioritized next experiments). Never paste raw Codex output.
