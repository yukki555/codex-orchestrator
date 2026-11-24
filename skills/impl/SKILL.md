---
name: impl
description: Plan, write a Spec, delegate implementation to Codex CLI via run_codex.sh or codex-worker, verify independently, and present a clear Chinese brief.
---

# Single-Task Structured Implementation (`/impl`)

Act as **Principal Engineering Manager** in Antigravity CLI (`agy`) following `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/SKILL.md`.

Execute strictly in order:

1. **Targeted Exploration & Alignment**:
   * Read only the relevant files via `read_file` and compact read-only checks.
   * Clarify any major architecture or scope tradeoff with the user before implementing. If the task modifies `>= 3` loosely coupled files or `> 200` lines, switch to the `/dag` parallel workflow instead.
2. **Author GPT-6.2 Spec**:
   * Write `/tmp/codex-orchestrator/<task-id>/spec.md` using `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/task-spec-template.md` (include `## Environment & Tooling Facts` and keep Codex local-only).
3. **Delegate Implementation**:
   * Call `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh implement --dir "<repo>" --structured --timeout 240 --spec /tmp/codex-orchestrator/<task-id>/spec.md --out-dir /tmp/codex-orchestrator/<task-id>/implement` directly via `run_command` (or delegate to `codex-worker` when isolating multi-round failure repairs or remote SSH/GPU tests).
4. **Layered Independent Verification**:
   * Inspect `git status` / `git diff --stat` (and `summary.json["partial_progress"]` if `status != ok`).
   * Run verification tests (without redundantly repeating identical unit tests already verified by `codex-worker`).
5. **Chinese Executive Brief**:
   * Rewrite findings from scratch in calm, precise natural Chinese following the **Communication Charter v3.1** in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (use the compact lightweight route for 1–2 file targeted changes, or the heavyweight engineering route for multi-module/cross-host deliveries; never paste raw Codex prose or internal metadata).
