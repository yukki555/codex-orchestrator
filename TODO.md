# Codex Orchestrator — Backlog & TODO

## Context Hygiene (`v3.9.0`)

- [x] Slim the cold-start policy and skills, move advanced CLI guidance to `cli-reference.md`, add two-tier `read_noise` and `codex-read-guard`, and exclude errored reads from telemetry.

## Historical release v3.8.0 — Dispatch Clarity and Synthesis Verification

- [x] Route verbosity declaratively and add the per-dispatch Output Clarity Contract.
- [x] Add structured review receipts and facts-only implementation previews.
- [x] Adopt objective-first verification and the Synthesis Firewall in Communication Charter v3.2.

## Horizontal Subagent Denoising & Chain-Inherited Auto-Handoff (`v3.7.0`)

- [x] Add `remote-runner` and built-in `research` routing, plus configurable checkpoint-scoped noise nudges.
- [x] Propagate auto-handoff to successor conversations and support a parallel subagent track in the Handoff Packet.

## Context Compaction & Session Handoff Automation (`v3.6.0`)

**Status**: Option 1 & Option 3 Implemented in `v3.6.0`; Option 2 Backlog  
**Motivation**: Prevent late-stage context rot in long Antigravity sessions before hitting the platform's automatic `CHECKPOINT` ceiling.

### Verified Platform Constraint

* Antigravity does **not** expose a model-callable `/compact` tool, a `ConversationService/Compact` RPC method, or a user-configurable auto-compaction threshold.
* In interactive sessions, `/compact` (or `/compact <next instruction>`) only triggers on a leading user message.

### Implementation Status

- [x] **Option 1 (Implemented in `v3.6.0`) — Context Watermark Hook (`PreInvocation` + `Stop`)**
  * Reads latest `input_tokens + cache_read_tokens` after the last `CHECKPOINT` from `transcriptPath` via zero-dependency `context_telemetry.py` + `hook_context_guard.py` (`timeout: 5s`, fail-open), sharing `$ANTIGRAVITY_APP_DATA_DIR/plugin_data/codex-orchestrator/handoff_state` with `handoff.sh`.
  * Applies model-family `cpLimit` dual-constraint thresholds (`Soft = min(200K, 45%)`, `Hard = min(300K, 65%)`, `Emergency = 80%`) with `CODEX_HANDOFF_*` env overrides, single-shot per-epoch firing, and `continuationActive` + `fullyIdle` `Stop` guard (`auto` mode). Omits `systemMessage.metadata` by default to work around Antigravity `v2.35.1`'s `ScrubInternalFields` Go panic on `map<string, string>` metadata (`CODEX_HANDOFF_HOOK_METADATA=1` opt-in once fixed upstream).
- [ ] **Option 2 (Backlog — Headless CLI / unattended pipelines) — External `agy` Auto-Compact Wrapper**
  * Wrap multi-stage headless pipelines to inspect transcript token usage between stages and run `agy --conversation <ID> -p "/compact <next stage instruction>"` automatically when over threshold.
- [x] **Option 3 (Implemented in `v3.6.0`) — Automated Session Handoff (`/handoff` + `handoff.sh`)**
  * Validates a 9-section Handoff Packet (ignoring single-segment slash commands in Sections 6–7), appends deterministic Git & Codex artifact snapshots, preserves `projectId`/`environmentId`/`conversationGroupId`/model via `product.v1.ConversationService`, and prevents split-brain edits with terminal-action & idempotency guards.

### Verification Status (`v3.6.0`)

- [x] Verified live Antigravity `v2.35.1` hook behavior: discovered `ScrubInternalFields` (`proto_utils.go:34`) panics when `systemMessage.metadata` is populated, so default injection embeds the Chinese token summary in `systemMessage` without `metadata` (`CODEX_HANDOFF_HOOK_METADATA=1` opt-in retained for future host releases).
- [x] Confirmed zero external dependencies, zero `__pycache__`, zero personal paths, and fail-open behavior on malformed/missing transcript entries.
- [x] Passed `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -v` and `agy plugin validate --strict`.
