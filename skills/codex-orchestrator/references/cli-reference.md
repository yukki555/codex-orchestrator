# Codex Orchestrator CLI Reference

## Declarative Profiles, Proof-of-Work & Opt-In Independent TDD

* **Declarative Profiles ([`config/profiles.json`](../config/profiles.json))**: `consult` and `implement` default to `gpt-6.2-sol`. `review`, `resume`, `research`, `critique`, and `analyze` default to `gpt-6.2-astra` (resume can inherit its prior model). `verbosity_by_mode` sets `low` for `implement` and `medium` elsewhere, with explicit `--verbosity` and profile inheritance supported. Override routing with `--profiles-file` or `CODEX_ORCH_PROFILES_FILE`.
* **Dispatch and Receipts**: `OUTPUT_CLARITY_FOOTER` asks exec workers for self-contained prose, exact anchors, and explicit verification gaps. Use `--no-clarity-footer` to omit it. `review --structured` uses `review-output-schema.json` with P0–P3 findings and unverified areas.
* **Trusted Repo Contract (`.codex-orchestrator.json`)**: Optional JSON file read strictly from pre-run `base_oid` (`git show <base_oid>:.codex-orchestrator.json`) or `--contract <FILE>` (never from the candidate tree modified by Codex; rejects arbitrary `before_run` shell hooks). Supports `allowed_paths`, `protected_paths`, `dirty_policy`, `max_timeout_s`, and `verify_commands` (argv lists run with `shell=False` under stripped `build_child_env()`).
* **Host-Side Proof-of-Work (`--verify-cmd` / `--verify-preset` / `--allowed-path` / `--protected-path`)**: Enforces hard path policies (exit `5` on violation) and runs host verification on a reconstructed clean worktree to emit `verification.json` (exit `6` on failure), strictly separate from Codex self-reported `verification[]`.
* **Opt-In Cross-Model Independent TDD (`--tdd`)**: Best for bug fixes and algorithmic contracts:
  1. Freezes immutable `test_snapshot` (`base_oid -> test_snapshot_commit`) via `--tdd-test-dir` or `--tdd-from-working-tree` + `--tdd-test-path`.
  2. Verifies **true RED** before launching Codex (exit `4` if tests pass, collect 0 tests, or hit `ImportError`/`SyntaxError`).
  3. Runs fresh Codex `implement` relative to `test_snapshot_commit` with frozen tests enforced in `protected_paths` (exit `5` on any add/edit/delete/rename of protected tests).
  4. Reconstructs combined candidate tree (`base_oid -> final_tree_oid`) and verifies **true GREEN** with zero unexpected skips (exit `6` on failure unless `--tdd-allow-skips`).

## Supervision, Immutable Attempts & Fail-Closed Exit Codes

The runners use the exit codes below. For handoff, exit `7` means the local conversation service is unavailable and a fallback prompt was persisted.

| Wrapper `status` | Wrapper `exit_code` | Condition |
| :--- | ---: | :--- |
| `ok` | `0` | Child exited `0`, no cancellation/timeout/quota failure, requested schema valid, path policies clean, custody verified, and host verification (if requested) passed. |
| Usage / Preflight error | `2` | Invalid CLI args, unreadable/unsupported `--schema` keyword, invalid trusted `.codex-orchestrator.json` (e.g. forbidden `before_run` hook), unreadable explicit config/rules, bidirectional self-hosting guard hit, or `--dirty-policy fail` on dirty checkout. **Must exit before creating `--out-dir`.** |
| `schema_error` | `3` | Child exited `0` but `last_message.md` failed JSON or JSON Schema validation when `--structured`/`--schema` was requested. |
| `tdd_red_failed` | `4` | Opt-in `--tdd` Stage RED did not produce a genuine collected assertion failure (rejects `Ran 0 tests`, `collected 0 items`, `ImportError`, `ModuleNotFoundError`, `SyntaxError`, `ERROR collecting`, or exit `0`). |
| `protected_path_violation` / `scope_violation` | `5` | Worker added, modified, deleted, or renamed a file matching `protected_paths` (including frozen TDD tests) or outside non-empty `allowed_paths`. Worktree is preserved. |
| `verification_failed` | `6` | Host-side Proof-of-Work (`--verify-cmd`/`--verify-preset`) or TDD Stage GREEN failed (non-zero exit, tree mismatch, 0 collected tests, or unexpected skips without `--tdd-allow-skips`). |
| `ls_api_unreachable` | `7` | `handoff.sh create` could not reach Antigravity `product.v1.ConversationService` (missing LS env, connection refused/timeout, or HTTP 403 sandbox block). Validated packet, manifest, and fallback prompt remain intact on disk. |
| `paused_quota` | child code or `75` | Child exited **non-zero** with unambiguous quota/rate-limit signals in `stderr` or JSONL `error`/`turn.failed` events (never triggered when `codex_exit_code == 0` or by normal command/prose output). |
| `timeout` | `124` | Hard monotonic wall timeout (`--timeout`) expired and triggered process-tree cleanup. |
| `stall_timeout` | `125` | Opt-in monotonic event inactivity timeout (`--stall-timeout`) expired and triggered process-tree cleanup. |
| `cancelled` | `128 + signal` | Wrapper/supervisor received `SIGTERM`/`SIGINT`, triggered process-tree cleanup, and failed closed even if the child exited `0`. |
| `error` | child exit code / `1` | Child exited non-zero, custody check failed, or fresh matching `summary.json` / `invocation_id` missing. |

* **Immutable Attempt History**: Every run records immutable files under `<out-dir>/attempts/attempt-NNN/` and atomically publishes top-level `summary.json` last (`resume` discovery ignores `attempts/*/`).
* **Bounded Stdout (`--stdout-mode auto|compact|facts|full`)**: `implement` defaults to `facts` for structural counts, severity and verification rollups, and artifact pointers. `auto` compacts responses larger than 4096 bytes. Mid-flight agent messages are available in the failure summary on disk.
* **Self-Hosting Safety**: Both `.sh` shims invoke `exec python3 -I -B` and refuse in-place `implement` targeting `scripts/` without `--worktree`.

## Context Telemetry & Native Session Handoff (`/handoff`, `v3.7`)

* **Dual-Constraint Performance Thresholds (`config/profiles.json` `context_handoff`)**: Resolves Antigravity platform truncation limits (`cpLimit`: Gemini 4 / Orchestrator `512K`, Claude 5.5 / Gemini 3.8 `256K`, Claude 4.6 `160K`, Gemini 3.1 Pro `128K`, GPT-OSS `80K`; Orchestrator dual-model threads take the conservative minimum) and computes `Soft = min(200K, 45% × cpLimit)`, `Hard = min(300K, 65% × cpLimit)`, and `Emergency = 80% × cpLimit` (overridable via `CODEX_HANDOFF_*` env vars).
* **Native Lifecycle Hook (`codex-context-handoff` in `hooks.json`)**: Fail-open `PreInvocation` + `Stop` guard (`hook_context_guard.py`, `timeout: 5s`) shares `$ANTIGRAVITY_APP_DATA_DIR/plugin_data/codex-orchestrator/handoff_state` with `handoff.sh`, skips child subagents, resets on `/compact` (`checkpoint_epoch`), fires each band at most once per epoch (omitting `systemMessage.metadata` by default to avoid Antigravity `v2.35.1`'s `ScrubInternalFields` map panic; opt in via `CODEX_HANDOFF_HOOK_METADATA=1`), guards against post-handoff split-brain edits, and supports `CODEX_HANDOFF_MODE=propose|auto|off` (`Stop` nudge active only in effective `auto` mode with `fullyIdle == true` and `continuationActive == false`).
* **Lossless Handoff CLI (`scripts/handoff.sh`)**: `inspect` reads live transcript telemetry; `validate` & `create` enforce the 8-section Handoff Packet contract (`references/handoff-packet-template.md`), verify referenced paths & Git conflict-free status, redact/block secrets, append deterministic `## 9. Deterministic Runtime Snapshot`, preserve `projectId` / `environmentId` / `conversationGroupId` / active model (with tier fallback), enforce `max_hops` and per-epoch idempotency, and fall back cleanly with exit `7` (`ls_api_unreachable`) if local LS RPC is sandbox-blocked.
* **Noise nudges** use the same transcript scan and fire at most once per checkpoint epoch for each type. Configure thresholds in `context_handoff.noise_nudge` and toggle them with `CODEX_NOISE_NUDGE`. They remain available with `CODEX_HANDOFF_MODE=off`.
* **Chain inheritance** follows explicit `CODEX_HANDOFF_MODE`, then the successor's `handoff-mode:auto|propose` tag, then the profile default. `--resume-mode auto` tags and pre-seeds the successor, which can start the packet's optional parallel subagent track on its first turn. `inspect` reports `effective_mode` and `tool_activity`.

### Antigravity context limits

| Model | cpLimit |
| --- | ---: |
| Gemini 4 / Orchestrator | 512K |
| Claude 5.5 / Gemini 3.8 | 256K |
| Claude 4.6 | 160K |
| Gemini 3.1 Pro | 128K |
| GPT-OSS | 80K |
| Unknown model | 160K |

### Environment overrides

| Variable | Effect |
| --- | --- |
| `CODEX_HANDOFF_MODE` | Select `propose`, `auto`, or `off`. |
| `CODEX_HANDOFF_SOFT_TOKENS`, `CODEX_HANDOFF_HARD_TOKENS` | Override the respective thresholds with positive token counts. |
| `CODEX_HANDOFF_SOFT_RATIO`, `CODEX_HANDOFF_HARD_RATIO` | Set the respective ratios to values in `(0, 1)`. |
| `CODEX_HANDOFF_MAX_HOPS` | Override the default chain limit of five hops with a positive integer. |
| `CODEX_HANDOFF_MODEL` | Set the successor model explicitly. |
| `CODEX_HANDOFF_ARCHIVE_OLD` | Set to `1` to archive the old conversation after success when the user authorized archiving. |
| `CODEX_HANDOFF_HOOK_METADATA` | Set to `1` to opt into hook metadata on compatible hosts. |
| `CODEX_NOISE_NUDGE` | Toggle remote and direct-read notices independently of the handoff mode. |
| `CODEX_READ_GUARD` | Disable with `0`, `off`, `false`, or `no`. |
| `CODEX_READ_GUARD_MAX_BYTES` | Set a positive size threshold in bytes (default 20480). |
| `CODEX_READ_GUARD_MAX_SPAN_LINES` | Set a positive maximum bounded span (default 450 lines). |

The read guard allows handoff packets, media and binary files, child subagent reads, and positive byte offsets. Users can explicitly request “全文读取” to allow a full read. Direct-read notices can fire after a count and model-relative token threshold or a single read-associated spike. A second notice can fire if direct reads continue beyond the rearm threshold, including after a research invocation.

For self-hosted changes, invoke the installed runner with `implement --dir "<plugin-repo>" --worktree`, keeping `--spec` and `--out-dir` outside the target checkout. Inspect the isolated candidate and its verification before applying the patch.

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
