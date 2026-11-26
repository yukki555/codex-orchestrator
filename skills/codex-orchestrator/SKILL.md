---
name: codex-orchestrator
description: Orchestrate local OpenAI Codex CLI, isolated workers, native research and remote-runner, verified patches, and session handoff.
---

# Codex Orchestrator (v3.8)

## Fold-First Routing

1. **Tier 0 — Main loop**. Handle single-fact lookups, docs/config edits, and small fixes within the active guard limit. Use bounded `read_file` with `StartLine`/`EndLine` and at most 200 lines for edits or spot-checks.
2. **Tier 1 — Direct Codex**. Use `scripts/run_codex.sh` for a single-pass `implement`, `consult`, `review --structured`, `research`, `critique`, or `analyze`.
3. **Tier 2 — Subagents and parallel work**.
   - Dispatch built-in `research` with `invoke_subagent(TypeName="research")` for read-only exploration or audit across >= 3 files or any > 20 KB file. Ask for findings and exact evidence anchors.
   - Use `remote-runner` (prefer `Model="flash"`) for cross-host sync, Launch-Verify-Return, and remote probing or polling expected to take >= 2 turns.
   - Use `codex-worker` to isolate noisy local test/build logs or multi-round Codex repair (<= 2 rounds).
   - Use `/dag` (`scripts/run_codex_parallel.sh`) for loosely coupled changes across >= 3 code files or > 200 lines. Use native `/swarm` when workers must coordinate mid-flight, then `/review` on the landed baseline. Read-only composition can use `/run-workflow` with the bundled workflows.

## Guard Policy

- `codex-delegate-guard` defaults to `strict`, blocking new or overwritten code files and edits > 10 changed lines unless the user opts out. `CODEX_GUARD_PROFILE=pragmatic` permits <= 30 changed lines in an existing code file and falls back to strict for `/impl` and `/dag`.
- `codex-read-guard` prompts for a tighter range, extraction with `rg` or `python3`, or delegation to `research` when a main-loop read exceeds the whole-file size threshold of 20 KiB.
- Use `--worktree` for self-hosted implementation. Keep Codex offline by default and protect API keys and `~/.codex/auth.json`.
- Consult [cli-reference.md](./references/cli-reference.md) for advanced flags, profiles, trusted contracts, TDD, exit codes, and examples. Use `/handoff` for a fresh top-level conversation when context pressure requires it.

## Layered Verification and Communication Charter v3.2

1. Inspect `git diff --stat`, key hunks, and host `verification.json` when available before relying on Codex prose. Audit each Spec acceptance item and disclose gaps. For read-only modes, spot-check at least two cited `file:line` or `source_id` anchors when available.
2. Follow the Synthesis Firewall and Dynamic Structure Routing in [user-brief-template.md](./references/user-brief-template.md). Preserve exact paths, symbols, commands, exit codes, and measurements. Rewrite working shorthand and internal codes as self-contained natural Chinese. Reuse the Charter on routine hops once familiar.
