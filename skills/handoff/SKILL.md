---
name: handoff
description: Inspect Antigravity context, write a verified handoff packet, and launch a successor with a terminal receipt.
---

# Session Handoff (`/handoff`)

1. **Quiescence Gate**. Collect active subagent receipts and wait for in-session background tasks. For long remote jobs, collect the Launch-Verify-Return receipt. Finish the atomic milestone or leave the workspace conflict-free.
2. Run `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/handoff.sh inspect`. Check context usage, band, checkpoints, model, tool activity, and effective mode. Consider `/compact` for light trimming in the current session.
3. Write the eight headings below into `<artifactDirectoryPath>/handoff/draft-handoff.md` or `/tmp/codex-orchestrator/handoff-draft.md`. Keep the draft within 400–16,384 bytes (<= 16 KB), compress prior-hop history, cite existing paths in Sections 6–7, and omit placeholders, comments, and secrets.

- **`## 1. Objective & Immutable Constraints`**. State the goal, definition of done, constraints, and standing user approvals.
- **`## 2. Current Phase & Remaining Checklist`**. Record completed and pending items, the next atomic action, and any parallel `remote-runner` or `research` assignment for Turn 1.
- **`## 3. Verified Facts & Evidence Anchors`**. Cite exact files and lines, commit IDs, command exits, and measurements. Label hypotheses.
- **`## 4. Architecture & Decisions Locked`**. Record settled decisions and their rationale.
- **`## 5. Dead Ends & Do-Not-Repeat Pitfalls`**. Preserve failed approaches, root causes, and environment quirks.
- **`## 6. Active Working Set`**. Give Immediate Touch (1–2 files with exact paths and line ranges) and a Reference Map for on-demand bounded reads or `research`.
- **`## 7. Codex Orchestrator & Artifact State`**. Link relevant specs, receipts, verification files, and thread IDs. Say which threads can resume.
- **`## 8. Open Risks, Unverified Boundaries & Pending User Decisions`**. Record unverified items and decisions that need the user.

4. Run `handoff.sh create --packet "<draft>" --workspace "<repo>" --artifact-dir "<artifacts>" --resume-mode auto` with the same script. Use `pause` when a user decision is pending. Add `--archive-old` only with user authorization. Explicit `CODEX_HANDOFF_MODE` takes precedence over chain inheritance.
   - The CLI appends the Section 9 snapshot and writes the manifest and receipt. The successor reads only `handoff-hop-NN.md` at cold start. It checks Git state and opens Section 6 fragments on demand or invokes `research` on Turn 1 when it must understand >= 3 files.
   - On exit 7, report the persisted fallback prompt path so the user can start the successor manually.
5. **Terminal-Action Receipt**. After exit 0, stop all workspace mutations. Report the clickable successor conversation and packet, context usage and limit, model, hop count and limit, and the next atomic action in natural Chinese.
   - Follow **Communication Charter v3.2** in `../codex-orchestrator/references/user-brief-template.md`. Reuse it on routine hops once familiar and disclose any verification gaps.
