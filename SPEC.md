# Codex Orchestrator Behavioral Specification (`v3.2`)

This document defines the stable **Session / Hands** behavioral invariants and conformance contract for `codex-orchestrator`. Orchestration policies and prompts live in the disposable **Brain** layer (`rules/`, `skills/`).

## 1. Architecture Boundaries

* **Brain (`rules/`, `skills/`)**: Task routing, Fold-First cost gating, Spec templates, and the Communication Charter (`v3.1`).
* **Session (Artifacts & `SPEC.md`)**: Immutable per-invocation evidence under `--out-dir` (`summary.json`, `last_message.md`, `events.jsonl`, `stderr.log`, `changes.patch`, `parallel-summary.json`).
* **Hands (`scripts/run_codex.sh`, `scripts/run_codex_parallel.sh`, `scripts/codex_orch/`)**: Unopinionated local execution kernel responsible for credential stripping, streaming supervision, schema preflight/validation, and Git patch custody.

## 2. Fail-Closed Execution & Exit Code Contract

Wrapper exit status is strictly separated from the raw child process exit code (`codex_exit_code`):

| Wrapper `status` | Wrapper `exit_code` | Condition |
| :--- | ---: | :--- |
| `ok` | `0` | Child exited `0`, no timeout/quota error, and (when `--structured` or `--schema` was requested) `structured_valid == true`. |
| Usage / Preflight error | `2` | Invalid CLI args, unreadable/unsupported `--schema` keyword, unreadable explicit user config/rules, self-hosting in-place `implement` without `--worktree`, or `--dirty-policy fail` on a dirty checkout. **Must exit before creating `--out-dir`.** |
| `schema_error` | `3` | Child exited `0` but `last_message.md` failed JSON parsing or JSON Schema validation when validation was requested. |
| `paused_quota` | child code or `75` | Child failed with unambiguous quota/rate-limit signals; never blindly retry write tasks. |
| `timeout` | `124` | Hard monotonic wall timeout (`--timeout`) expired; full process group terminated (`SIGTERM` -> `SIGKILL`). |
| `stall_timeout` | `125` | Opt-in monotonic event inactivity timeout (`--stall-timeout`) expired; full process group terminated. |
| `cancelled` | `128 + signal` | Wrapper/supervisor received `SIGTERM` or `SIGINT`; full process group terminated even if child exited `0`. |
| `error` | child exit code / `1` | Child exited non-zero, custody verification failed, or required fresh `summary.json` / `invocation_id` match failed. |

## 3. Private-Index Patch Custody Invariant (`run_codex_parallel.sh`)

1. Resolve `base_oid` (`git rev-parse --verify HEAD`) per selected checkout root (`git_root`), never conflating linked worktrees on different commits.
2. Create detached worktrees explicitly at `base_oid`. Track Git worktree registration explicitly; never fall back to `shutil.rmtree` when `git worktree remove` fails.
3. Export deliverables using a private temporary `GIT_INDEX_FILE` initialized from the worker's final `HEAD`, staging all tracked edits/deletions and non-ignored untracked files under ordinary `.gitignore` rules (`final_tree_oid`).
4. Derive BOTH `changes.patch` and `changed_files` from the exact `(base_oid, final_tree_oid)` pair (capturing commits inside the worktree even when `git status` is clean).
5. Independently apply `changes.patch` to a fresh private index initialized at `base_oid` (`reconstructed_tree_oid`). Delete the worktree **only** when `reconstructed_tree_oid == final_tree_oid`, worker exited `0`, no errors occurred, and `--keep-worktrees` is unset. Preserve the worktree on any failure.
6. Run merge checks in an atomic private temporary directory, reporting `base_reconstruct_ok` (clean `base_oid` sequential apply) separately from `target_apply_ok` (current mutable target checkout).

## 4. Security, Telemetry & Stdout Hygiene

* **Credential Isolation**: Child processes run under `codex_orch.env.build_child_env` (stripping host SSH/cloud/platform tokens while preserving Codex/OpenAI auth and runtime essentials). Artifact directories use `0700` permissions; failed command snippets and event errors are secret-redacted.
* **Self-Hosting Guard**: Refuses in-place `implement` targeting the plugin's own script directory unless `--worktree` is set or executed from an immutable frozen copy.
* **Bounded Stdout (`--stdout-mode auto|compact|full`)**: Default `auto` prints full `last_message.md` up to `4096` bytes and switches to a UTF-8-safe byte-bounded preview with file pointers above `4096` bytes.
* **Parent-Owned Evidence Identity**: `run_codex_parallel.sh` generates a per-task `invocation_id` passed via `--invocation-id` and validates freshness, ID match, status, and conditional `structured_valid` (raw tasks with `structured_valid: null` remain valid).
* **Budget Telemetry**: `summary.json` records `budget_exceeded`, `budget_utilization`, `cmd_per_modified_file` (`null` when zero modified files), `last_message_bytes`, `stdout_truncated`, and `malformed_event_lines`.

## 5. Conformance Matrix (`tests/test_plugin.py`)

Every release must pass zero-dependency `unittest` verification covering:
1. Manifest/hooks/skills/schemas/personal-path hygiene + `agy plugin validate --strict`.
2. Guard `strict` default (`<= 10` lines), stateless `CODEX_GUARD_PROFILE=pragmatic` (`<= 30` lines existing file, new file & `/impl`/`/dag` denied), and tightened path exemptions.
3. Schema keyword preflight rejection (`oneOf`/`minItems`/`$ref`) before `--out-dir` creation and fail-closed exit `3` on invalid output.
4. Child env stripping & secret redaction, wall/stall process-group termination, quota classification, self-hosting guard, and compact stdout byte bounds.
5. Real Git repo patch custody (worker commit + unstaged edit + delete + new file + ignored cache), clean-after-commit non-empty `changed_files`, mixed raw/structured manifests, dirty-policy enforcement, and concurrent merge-check isolation.
