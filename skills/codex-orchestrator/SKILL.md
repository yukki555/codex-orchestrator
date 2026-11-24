---
name: codex-orchestrator
description: >-
  Orchestrate local OpenAI Codex CLI (codex exec) via pure-Python codex_orch kernel
  (run_codex.sh / run_codex_parallel.sh, or codex-worker for multi-round repairs & SSH/GPU
  isolation) with declarative profiles, patch custody, host-side Proof-of-Work, opt-in --tdd,
  and loose composition with /swarm and /run-workflow.
---

# Codex Orchestrator (v3.6)

Operate a **General-Purpose Manager + High-Execution Specialist** workflow in Google Antigravity built around a decoupled **Brain (`rules/`, `skills/`, `workflows/`) / Session (`SPEC.md` + immutable `attempts/attempt-NNN/` + `handoff/` packets) / Hands (`codex_orch/` Python kernel + `config/profiles.json` + thin `.sh` shims)** architecture.

## Fold-First Cost Ladder & Entry Points

Before spawning workers, apply the Fold-First ladder (multi-agent fan-out can consume ~15× tokens vs chat, compared to ~4× for single-agent loops):

1. **Tier 0 (Main Loop + Skills)**: Single-fact lookups, docs/config edits, or small fixes within the active Guard limit (`<= 10` lines under default `strict`, or `<= 30` lines on an existing code file when `CODEX_GUARD_PROFILE=pragmatic`).
2. **Tier 1 (Direct 1-Hop `run_codex.sh` — Default)**: Clean single-pass `implement`, `consult`, `review --structured`, `research`, `critique`, or `analyze` via `run_command`.
3. **Tier 2 (Isolator, Parallel DAG, or Native Composition — Only When Justified)**:
   * **`codex-worker` subagent (`inheritMcp: false`)**: Only when isolating multi-round repair loops (`<= 2` rounds), remote SSH/GPU checks, or noisy build/test logs.
   * **Parallel `/dag` (`run_codex_parallel.sh`)**: `>= 3` loosely coupled code files (`> 200` lines), with automatic public Ownership Table injection, private-index patch custody, and optional combined host-side Proof-of-Work.
   * **Native Antigravity Composition**:
     * **`/swarm` -> `/review` (Interactive Multi-Agent Coding + Cross-Model Final Audit)**: When parallel tasks require real-time mid-flight negotiation, run native `/swarm` (`land-before-kill`) and then invoke `/review` (`run_codex.sh review --uncommitted --structured` or `--base`) on the exact landed baseline for an independent cross-model audit.
     * **`/run-workflow` Read-Only Templates**: For multi-slice research or multi-perspective critique with Antigravity Phase/Agent cards, use [`workflows/research_parallel.py`](./workflows/research_parallel.py) or [`workflows/debate_multi_critic.py`](./workflows/debate_multi_critic.py) mounting `agent(type_name="codex-worker", schema=...)`. Keep write `/dag` batches on headless `run_codex_parallel.sh` for guaranteed patch custody.

## Guard Policy (`PreToolUse` `codex-delegate-guard` via `codex_orch.host_adapter`)

* **`strict` (default)**: Blocks new/overwritten code files and edits `> 10` changed lines unless explicitly opted out (`不用codex` / `skip codex` / `你直接改`).
* **`CODEX_GUARD_PROFILE=pragmatic` (stateless opt-in)**: Allows `<= 30` changed lines on existing single code files while still denying new/overwritten code files and falling back to `strict` (`<= 10` lines) whenever `/impl` or `/dag` is invoked.
* Controlled path exemptions are restricted strictly to `/tmp/codex-orchestrator/` and `~/.gemini/antigravity/brain/`.

## Declarative Profiles, Proof-of-Work & Opt-In Independent TDD

* **Declarative Profiles ([`config/profiles.json`](./config/profiles.json))**: Externalizes mode model/effort/speed defaults, `gpt-6.2-sol` (`ultrafast`) vs `gpt-6.2-astra` (`fast`) capability rules, `max_cmds` defaults, and bundled schema filenames (override with `--profiles-file <PATH>` or `CODEX_ORCH_PROFILES_FILE`).
* **Trusted Repo Contract (`.codex-orchestrator.json`)**: Optional JSON file read strictly from pre-run `base_oid` (`git show <base_oid>:.codex-orchestrator.json`) or `--contract <FILE>` (never from the candidate tree modified by Codex; rejects arbitrary `before_run` shell hooks). Supports `allowed_paths`, `protected_paths`, `dirty_policy`, `max_timeout_s`, and `verify_commands` (argv lists run with `shell=False` under stripped `build_child_env()`).
* **Host-Side Proof-of-Work (`--verify-cmd` / `--verify-preset` / `--allowed-path` / `--protected-path`)**: Enforces hard path policies (exit `5` on violation) and runs host verification on a reconstructed clean worktree to emit `verification.json` (exit `6` on failure), strictly separate from Codex self-reported `verification[]`.
* **Opt-In Cross-Model Independent TDD (`--tdd`)**: Best for bug fixes and algorithmic contracts:
  1. Freezes immutable `test_snapshot` (`base_oid -> test_snapshot_commit`) via `--tdd-test-dir` or `--tdd-from-working-tree` + `--tdd-test-path`.
  2. Verifies **true RED** before launching Codex (exit `4` if tests pass, collect 0 tests, or hit `ImportError`/`SyntaxError`).
  3. Runs fresh Codex `implement` relative to `test_snapshot_commit` with frozen tests enforced in `protected_paths` (exit `5` on any add/edit/delete/rename of protected tests).
  4. Reconstructs combined candidate tree (`base_oid -> final_tree_oid`) and verifies **true GREEN** with zero unexpected skips (exit `6` on failure unless `--tdd-allow-skips`).

## Supervision, Immutable Attempts & Fail-Closed Exit Codes

See [`SPEC.md`](../../../SPEC.md) for the complete exit-code table (`0` ok, `2` preflight/self-hosting/unsupported schema/invalid contract, `3` schema_error, `4` tdd_red_failed, `5` protected_path/scope violation, `6` verification_failed, `75` paused_quota on non-zero child exit, `124` timeout, `125` stall_timeout, `128+sig` cancelled).

* **Immutable Attempt History**: Every run records immutable files under `<out-dir>/attempts/attempt-NNN/` and atomically publishes top-level `summary.json` last (`resume` discovery ignores `attempts/*/`).
* **Bounded Stdout (`--stdout-mode auto|compact|full`)**: Default `auto` compacts `last_message.md > 4096` bytes into a UTF-8-safe preview with file pointers. Always `read_file` `last_message.md` / `summary.json` / `verification.json` when inspecting full details.
* **Self-Hosting Safety**: Both `.sh` shims invoke `exec python3 -I -B` and refuse in-place `implement` targeting `scripts/` without `--worktree`.

## Context Telemetry & Native Session Handoff (`/handoff`, `v3.6`)

* **Dual-Constraint Performance Thresholds (`config/profiles.json` `context_handoff`)**: Resolves Antigravity platform truncation limits (`cpLimit`: Gemini 4 / Orchestrator `512K`, Claude 5.5 / Gemini 3.8 `256K`, Claude 4.6 `160K`, Gemini 3.1 Pro `128K`, GPT-OSS `80K`; Orchestrator dual-model threads take the conservative minimum) and computes `Soft = min(200K, 45% × cpLimit)`, `Hard = min(300K, 65% × cpLimit)`, and `Emergency = 80% × cpLimit` (overridable via `CODEX_HANDOFF_*` env vars).
* **Native Lifecycle Hook (`codex-context-handoff` in `hooks.json`)**: Fail-open `PreInvocation` + `Stop` guard (`hook_context_guard.py`, `timeout: 5s`) shares `$ANTIGRAVITY_APP_DATA_DIR/plugin_data/codex-orchestrator/handoff_state` with `handoff.sh`, skips child subagents, resets on `/compact` (`checkpoint_epoch`), fires each band at most once per epoch (omitting `systemMessage.metadata` by default to avoid Antigravity `v2.35.1`'s `ScrubInternalFields` map panic; opt in via `CODEX_HANDOFF_HOOK_METADATA=1`), guards against post-handoff split-brain edits, and supports `CODEX_HANDOFF_MODE=propose|auto|off` (`propose` default; `Stop` nudge active only in `auto` mode with `fullyIdle == true` and `continuationActive == false`).
* **Lossless Handoff CLI (`scripts/handoff.sh`)**: `inspect` reads live transcript telemetry; `validate` & `create` enforce the 8-section Handoff Packet contract (`references/handoff-packet-template.md`), verify referenced paths & Git conflict-free status, redact/block secrets, append deterministic `## 9. Deterministic Runtime Snapshot`, preserve `projectId` / `environmentId` / `conversationGroupId` / active model (with tier fallback), enforce `max_hops` and per-epoch idempotency, and fall back cleanly with exit `7` (`ls_api_unreachable`) if local LS RPC is sandbox-blocked.

## Layered Verification & Communication Charter (`v3.1`)

Before replying after any Codex run:
1. Inspect `summary.json` / `parallel-summary.json` / `verification.json` and `git diff`; for read-only modes, spot-check `>= 2` cited `file:line` or `source_id` entries directly.
2. Rewrite from scratch in calm, precise natural Chinese following [user-brief-template.md](./references/user-brief-template.md) (Communication Charter v3.1). Never paste raw Codex `last_message.md` or internal metadata.

## Invocation Examples

```bash
SCRIPTS=~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts

# 1. Default 1-hop structured implementation with host-side Proof-of-Work
"$SCRIPTS/run_codex.sh" implement \
  --dir "/path/to/repo" --structured --timeout 240 --stall-timeout 120 \
  --verify-cmd '["python3", "-m", "unittest", "discover", "-s", "tests"]' \
  --spec /tmp/codex-orchestrator/task-1/spec.md \
  --out-dir /tmp/codex-orchestrator/task-1/implement

# 2. Opt-in Cross-Model Independent TDD (staging tests in /tmp/.../tdd_tests)
"$SCRIPTS/run_codex.sh" implement \
  --dir "/path/to/repo" --structured --tdd \
  --tdd-test-dir /tmp/codex-orchestrator/bug-1/tdd_tests \
  --tdd-test-path "tests/test_bugfix.py" \
  --verify-cmd '["python3", "-m", "unittest", "tests/test_bugfix.py"]' \
  --spec /tmp/codex-orchestrator/bug-1/spec.md \
  --out-dir /tmp/codex-orchestrator/bug-1/tdd

# 3. Parallel DAG implementation with auto ownership table, custody check & combined Proof-of-Work
"$SCRIPTS/run_codex_parallel.sh" \
  --out-dir /tmp/codex-orchestrator/topic-1/stage1 \
  --mode implement --structured --max-parallel 3 --timeout 240 \
  --verify-preset unit --dir "/path/to/repo" \
  --manifest /tmp/codex-orchestrator/topic-1/manifest.json

# 4. Inspect live context health or launch a lossless successor conversation
"$SCRIPTS/handoff.sh" inspect
"$SCRIPTS/handoff.sh" create \
  --packet /tmp/codex-orchestrator/topic-1/handoff-draft.md \
  --workspace "/path/to/repo" \
  --artifact-dir /tmp/codex-orchestrator/topic-1 \
  --resume-mode auto
```

Never print, inspect, or log `~/.codex/auth.json` or API keys.
