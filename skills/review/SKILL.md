---
name: review
description: Run a single-pass structured Codex CLI review on uncommitted Git changes and summarize risks in plain Chinese.
---

# Single-Pass Structured Git Diff Review (`/review`)

Act as **Principal Engineering Manager** in Google Antigravity running an independent structured Git QA review via `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh`.

Execute strictly in order:

1. **Snapshot Target Repo**:
   * Run `git -C "<repo>" status --short` and `git -C "<repo>" diff --stat HEAD` via `run_command` to confirm the exact scope under review.
2. **Run Single-Pass Structured Review**:
   * Invoke directly via `run_command` (no wrapper subagent needed for single-turn review):
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh review --dir "<repo>" --uncommitted --structured --timeout 240 --out-dir /tmp/codex-orchestrator/review-$(date +%Y%m%d-%H%M%S) --prompt "Review all uncommitted changes thoroughly for correctness bugs, edge cases, backward-compatibility breaks, numerical/concurrency issues, and missing test coverage. Focus on any user-specified concerns."`
   * Note: passing `--structured` and `--prompt` automatically routes `review` through read-only exec with a bounded Git status/diff snapshot so structured JSON output works in one pass.
3. **Spot-Check & Fact-Check**:
   * Confirm `structured_valid: true` in `summary.json` and read `last_message.md`.
   * Spot-check at least 1–2 cited `file:line` claims directly via `read_file` to separate verified defects from unverified hypotheses.
4. **Chinese Risk Brief**:
   * Present findings prioritized by severity (`P0/P1/P2/P3`) in calm, precise natural Chinese following the **heavyweight engineering / independent `review` route** of the Communication Charter v3.1 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (lead with verdict & verification watermark, state root cause without repeating the issue table, preserve exact `file:line` anchors, and never paste raw Codex output or internal metadata).
