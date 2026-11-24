---
name: handoff
description: >-
  Inspect live Antigravity context utilization, author a verified 9-section
  Handoff Packet, and launch a clean successor conversation via handoff.sh
  (ConversationService) with model/project preservation and anti-split-brain receipt.
---

# Session Handoff (`/handoff`)

Act as **Principal Engineering Manager** in Google Antigravity to seamlessly transfer a long-running, multi-phase task into a fresh top-level conversation before context rot or opaque platform compaction degrades quality.

Execute strictly in order:

1. **Quiescence Gate (Never Handoff Mid-Flight)**:
   * Wait for all active `codex-worker` subagents and background `run_command` tasks to finish and land their outputs (successor conversations cannot receive wakeups from the old session's background tasks).
   * Finish the current atomic milestone or leave the workspace in a clean, conflict-free state (`git status` with zero unresolved merge conflicts).
2. **Inspect Live Context Telemetry**:
   * Run `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/handoff.sh inspect` to read exact `active_tokens`, `context_limit`, `band`, `checkpoint_count`, and `active_model`.
   * If you only need lightweight noise trimming inside the same UI tab (and are not running an unattended multi-phase pipeline or experiencing checkpoint fatigue), inform the user that native `/compact` is also an option.
3. **Author Verified 9-Section Handoff Packet**:
   * Follow `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/handoff-packet-template.md` and write a concise, high-signal packet (`1.5K–3.5K` tokens, `400 B – 24 KB`) to `<artifactDirectoryPath>/handoff/draft-handoff.md` (or `/tmp/codex-orchestrator/handoff-draft.md`).
   * Include all 8 human/model-authored sections (`## 1.` .. `## 8.`), remove all `<!-- ... -->` template comments, cite only existing file paths in Sections 6–7, and never embed credentials or CSRF tokens.
4. **Execute Handoff (`handoff.sh create`)**:
   * Run:
     ```bash
     ~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/handoff.sh create \
       --packet "<draft_packet_path>" \
       --workspace "<repo_dir>" \
       --artifact-dir "/tmp/codex-orchestrator/<task-id>" \
       --resume-mode auto
     ```
     (Use `--resume-mode pause` when waiting on a pending user decision; add `--archive-old` only if the user requested archiving the old thread.)
   * `handoff.sh` automatically validates Sections 1–8, appends deterministic `## 9. Deterministic Runtime Snapshot`, writes `handoff-manifest.json` + `handoff-receipt.json`, preserves `projectId` / `environmentId` / `conversationGroupId` / active model (with tier fallback if needed), tags the old session `handoff-completed`, and spawns the successor with the 3-step cold-start protocol.
   * If `handoff.sh` returns exit `7` (`ls_api_unreachable` / sandbox-blocked), keep the persisted packet intact and present the fallback prompt path clearly to the user.
5. **Terminal-Action Receipt (Zero Subsequent Edits)**:
   * Immediately after `handoff.sh create` exits `0`, execute **ZERO further workspace edits or mutating commands** (prevents split-brain races with the newly started successor).
   * Reply concisely in natural Chinese following Communication Charter v3.1 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`: provide the clickable successor link `[新会话标题](conversation://<new_conversation_id>)`, clickable Handoff Packet file link, context/model telemetry (`active_tokens / context_limit`, `model_used`, `hop_index / max_hops`), and the exact next atomic action the successor is resuming.
