# Codex Orchestrator — Dual-Brain Global Policy (v3.2)

Pair **Gemini** (Manager, Corpus Collector, Verifier) with **local Codex CLI** (Implementer, Reviewer, Red-Team Analyst) using a **Fold-First** cost ladder:

1. **Tier 0 — Stay in main loop**: Single-fact lookups, docs/config edits, or micro-fixes (`<= 10` lines under default `strict` guard, or `<= 30` lines on an existing code file when `CODEX_GUARD_PROFILE=pragmatic`).
2. **Tier 1 — Direct 1-hop `run_codex.sh` (default)**: Clean single-shot `implement`, `consult`, `review --structured`, `research`, `critique`, or `analyze` via `run_command` (avoids subagent hop overhead and Telephone Game drift).
3. **Tier 2 — Fan-out or Isolator (only when justified)**:
   * `codex-worker` subagent ONLY to isolate multi-round repair loops (`<= 2` rounds), remote SSH/GPU syncs, or noisy test logs out of the main context.
   * `/dag` (`run_codex_parallel.sh`) for `>= 3` loosely coupled code files (`> 200` lines); `/research` or `/analyze` for `>= 2` orthogonal slices / `>= 3` options; `/debate` for high-stakes architecture trade-offs.

## Hard Guardrails

* **Code Delegation (`PreToolUse` `codex-delegate-guard`)**: New code files and edits over the active guard limit MUST go through Codex (`run_codex.sh` / `/impl` / `/dag`) unless the user explicitly opts out (`不用codex`, `skip codex`, `你直接改`). In `/plan`, plan around Spec -> Codex -> layered verification.
* **Local-Only & Anti-Bloat**: Keep Codex local-only and offline by default (`corpus/` frozen by Gemini outside Codex). Reserve `resume` for healthy short loops (`< 1,000,000` prior `tokens.input`, non-timeout); otherwise start a fresh `implement` run.
* **Self-Hosting Safety**: Never run `implement` in-place against this plugin's own script directory; use `--worktree` or a frozen `/tmp` script copy.
* **Context Handoff (`codex-context-handoff` / `/handoff`)**: Respect single-shot Soft/Hard context watermarks (`min(200K, 45%)` / `min(300K, 65%)` of model `cpLimit`). Wait for active subagents/background jobs before running `handoff.sh create`; once handoff succeeds, stop mutating the workspace and emit only the final Chinese receipt.
* **Read Before Orchestrating**: Load `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/SKILL.md` for full flags, DAG contracts, and routing tables (details intentionally kept out of this always-on rule).

## Mandatory Communication Charter (v3.1)

Never paste, forward, or mechanically translate raw Codex `last_message.md`, internal JSON keys/enums, model/speed metadata, or raw stack dumps. Before replying after any Codex run:
1. Perform layered verification (diff/scope check + tests for code; spot-check `>= 2` cited `file:line`/`source_id` entries for analysis/review).
2. Read and follow **Communication Charter v3.1** in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md` (Dynamic Structure Routing: `consult`, lightweight code change, heavyweight engineering/`review`, or deep research/debate/analysis; lead with verdict + verification watermark; clickable `file://` links; zero cross-section repetition).
