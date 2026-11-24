---
name: impl
description: Plan, write a Spec, delegate implementation to Codex CLI via 1-hop run_codex.sh (optional --tdd / host Proof-of-Work, or codex-worker for multi-round/SSH isolation), verify independently, and present a clear Chinese brief.
---

# Single-Task Structured Implementation (`/impl`)

Act as **Principal Engineering Manager** in Google Antigravity following `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/SKILL.md`.

Execute strictly in order:

1. **Fold-First Check & Targeted Exploration**:
   * Read only the relevant files via `read_file` and compact read-only checks.
   * Clarify any major architecture or scope tradeoff with the user before implementing. If the task modifies `>= 3` loosely coupled files or `> 200` lines, switch to `/dag` (or native `/swarm` followed by `/review` if mid-flight negotiation is required).
2. **Author GPT-6.2 Spec (Optional Cross-Model TDD)**:
   * Write `/tmp/codex-orchestrator/<task-id>/spec.md` using `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/task-spec-template.md`.
   * For bug fixes or strict algorithmic contracts, optionally stage failing regression tests under `/tmp/codex-orchestrator/<task-id>/tdd_tests/` and pass `--tdd --tdd-test-dir ... --tdd-test-path ... --verify-cmd ...` so the host verifies true RED before Codex runs, blocks test tampering (exit `5`), and verifies true GREEN on the reconstructed tree.
3. **Delegate Implementation (1-Hop First)**:
   * **Default (1-hop)**: Invoke `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh implement --dir "<repo>" --structured --timeout 240 --spec /tmp/codex-orchestrator/<task-id>/spec.md --out-dir /tmp/codex-orchestrator/<task-id>/implement` (add `--verify-cmd` / `--verify-preset` and `--protected-path` when host-side Proof-of-Work is desired).
   * **Isolator (`codex-worker`)**: Use `codex-worker` only when isolating multi-round repair loops (`<= 2` rounds), remote SSH/GPU tests, or noisy build logs.
   * Never run `implement` in-place against this plugin's own script directory; pass `--worktree` or run from a frozen `/tmp` script copy.
4. **Layered Independent Verification**:
   * Confirm `status == "ok"`, `exit_code == 0`, `structured_valid == true`, and (when enabled) `verification.json["ok"] == true` in `summary.json`.
   * Inspect `git status` / `git diff --stat` and run higher-tier verification gates as needed.
5. **Chinese Executive Brief**:
   * Rewrite findings from scratch in calm, precise natural Chinese following the **Communication Charter v3.1** in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (use the compact lightweight route for 1–2 file targeted changes, or the heavyweight engineering route for multi-module/cross-host deliveries; never paste raw Codex prose or internal metadata).
