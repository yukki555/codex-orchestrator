# Research / Critique / Analysis Spec Template (GPT-6.2 Tuned, v3.9)

Copy this structure to `/tmp/codex-orchestrator/<task-id>/spec-<slice>.md` before dispatching via `run_codex.sh research|critique|analyze` or `run_codex_parallel.sh --manifest`.

```markdown
## Goal & Decision Question
<!-- Exact question, comparison scope, or hypothesis this worker must investigate, challenge, or verify. -->

## Instruction Precedence
This Task Spec overrides repository `AGENTS.md` and generic skills wherever instructions conflict.

## Corpus & Source Index
<!-- For offline reproducible runs (default), list the frozen read-only files under `/tmp/codex-orchestrator/<task-id>/corpus/` and relevant workspace paths. -->
- Corpus directory: `/tmp/codex-orchestrator/<task-id>/corpus/`
- Source index: `/tmp/codex-orchestrator/<task-id>/corpus/sources_index.json`
- Attached images (if `--image` passed): <!-- e.g., `loss_curve.png` (training curves), `fig3.png` (paper architecture) -->
- Additional read-only paths: <!-- e.g., `<repo>/src/model.py` -->

## Mode-Specific Instructions
<!-- Pick the matching block and delete the others: -->

### For `research` (Evidence Extraction & Trade-Study)
- Evaluate options across these fixed dimensions: <!-- e.g., Accuracy, Latency, Memory, Implementation Complexity, Failure Modes -->
- Label every claim's `confidence` strictly:
  - `verified`: directly supported by an explicit quote, formula, metric, or code line in the corpus (must cite `source_ids` and exact `quote_or_location`).
  - `inferred`: logical deduction from verified facts (state the premise clearly).
  - `uncertain`: conflicting, outdated, or incomplete evidence (also record in `knowledge_gaps`).
- Actively seek `counter_evidence` (edge cases, assumptions, regimes where a claimed advantage breaks down).

### For `critique` (Blind Red-Team Review)
- Treat the target proposal as an unverified draft written by an unknown author; do NOT flatter or defer.
- Hunt specifically for: hidden assumptions, survivor/selection bias, stale benchmarks, mismatched metric definitions, missing simpler alternatives, and P0/P1 failure modes.
- Every P0/P1 challenge MUST cite concrete evidence or exact logical counterexample and propose a concrete `suggested_fix`.

### For `analyze` (Paper / Code / Experiment Consistency Audit)
- Cross-check expected formulas/configs against actual implementation lines and observed experiment logs/metrics.
- You may run small read-only `python3` calculations on files inside the corpus/workspace to verify numbers, statistics, or tensor shapes (record real commands & exit codes in `verification`).
- Classify each checked dimension as `match`, `mismatch`, or `unverified` with exact `evidence_location`.

## Environment & Tooling Facts
- Read-only sandbox (`-s read-only`): do NOT attempt to edit workspace files or run remote SSH commands.
- Network access: <!-- "Offline frozen corpus only; do NOT attempt curl/network fetches." OR "Live `--web-search` enabled for up-to-date external facts; always cite exact URLs in `source_ids`." -->

<!-- Execution Budget (--max-cmds) and Output Clarity Contract are auto-appended by run_codex.sh unless disabled via --max-cmds 0 / --no-clarity-footer; do not duplicate them here. -->

## Output Contract
- Write complete, self-contained sentences without internal shorthand, step or slot codes, or invented compound labels. Preserve exact evidence anchors and disclose skipped checks, unverified assumptions, and blockers.
- Never fabricate citations, metrics, URLs, or `file:line` references; put missing items into `knowledge_gaps` or mark `unverified`.
- For `review --structured`, use `review-output-schema.json` and report findings and unverified areas explicitly.
- With `--structured`: strictly populate every required JSON field of the mode's bundled schema (`research-output-schema.json`, `critique-output-schema.json`, or `analysis-output-schema.json`).
- Without `--structured`: present verified facts vs inferences, explicit source citations, counter-evidence, and decision tradeoffs clearly.
```
