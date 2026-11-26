# Codex Orchestrator — Dual-Brain Global Policy (v4.0)

Pair **Gemini** (Manager, Corpus Collector, Verifier) with **local Codex CLI** (Implementer, Reviewer, Red-Team Analyst) using a **Fold-First** cost ladder:

1. **Tier 0 — Stay in main loop**: Single-fact lookups, docs/config edits, or micro-fixes (`<= 10` lines under default `strict` guard, or `<= 30` lines on an existing code file when `CODEX_GUARD_PROFILE=pragmatic`).
2. **Tier 1 — Direct 1-hop `run_codex.sh` (default)**: Clean single-shot `implement`, `consult`, `review --structured`, `research`, `critique`, or `analyze` via `run_command` (avoids subagent hop overhead and Telephone Game drift).
3. **Tier 2 — Fan-out or Isolator (only when justified)**:
   * `remote-runner` subagent (prefer `Model="flash"`) for cross-host file sync, remote job Launch-Verify-Return, or remote probing/polling expected to take `>= 2` turns. Keep single read-only probes in the main loop.
   * `/dispatch` (`remote_dispatch.sh`) for multi-server dynamic GPU sweeps.
   * Built-in research subagent for read-only comprehension/audit across >= 3 files or any > 20 KB file (keep main-loop read_file to bounded <= 200-line slices for editing or spot-checks).
   * `codex-worker` subagent for multi-round Codex repair loops (`<= 2` rounds) or noisy local test/build log isolation.
   * `/dag` (`run_codex_parallel.sh`) for `>= 3` loosely coupled code files (`> 200` lines); `/research` or `/analyze` for `>= 2` orthogonal slices / `>= 3` options; `/debate` for high-stakes architecture trade-offs.

## Hard Guardrails

* **Code Delegation (`PreToolUse` `codex-delegate-guard`)**: New code files and edits over the active guard limit MUST go through Codex (`run_codex.sh` / `/impl` / `/dag`) unless the user explicitly opts out (`不用codex`, `skip codex`, `你直接改`). In `/plan`, plan around Spec -> Codex -> layered verification.
* **Read Guard (`codex-read-guard`)**. Use bounded reads, extraction, or `research` when the > 20 KB unbounded `read_file` guard fires.
* **Local-Only & Anti-Bloat**: Keep Codex local-only and offline by default (`corpus/` frozen by Gemini outside Codex). Reserve `resume` for healthy short loops (`< 1,000,000` prior `tokens.input`, non-timeout); otherwise start a fresh `implement` run.
* **Self-Hosting Safety**: Never run `implement` in-place against this plugin's own script directory; use `--worktree` or a frozen `/tmp` script copy.
* **Context Handoff (`codex-context-handoff` / `/handoff`)**: Respect Soft/Hard watermarks (`min(200K, 45%)` / `min(300K, 65%)` of model `cpLimit`). Collect active subagent receipts and wait for in-session background tasks before `handoff.sh create`. After success, stop mutating the workspace and emit only the final Chinese receipt.
  * Scope subagent work to a single atomic wave. Long remote jobs use Launch-Verify-Return.
  * `--resume-mode auto` carries auto-handoff forward unless explicit `CODEX_HANDOFF_MODE` overrides it.
* **Read Before Orchestrating**: Load `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/SKILL.md` only when invoking Codex, DAG, TDD, or handoff orchestration.

## Mandatory Communication Charter (v3.2)

Before replying after any Codex run:
1. **Objective artifacts first**: inspect `git diff --stat`, key hunks and host `verification.json` before reading Codex prose. Audit every Spec acceptance item and mark missing proof as unverified. For read-only modes, spot-check `>= 2` cited `file:line` or `source_id` anchors when available.
2. **Synthesis Firewall**: preserve exact `file:line`, symbols, commands, exit codes and measurements. Strip Codex shorthand and internal codes and write self-contained natural Chinese. Follow **Communication Charter v3.2** and its Dynamic Structure Routing in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`. Reuse the Charter on routine hops once familiar.
