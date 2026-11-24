# Codex Orchestrator Behavioral Specification (`v3.5`)

This document defines the stable **Session / Hands** behavioral invariants and conformance contract for `codex-orchestrator`. Orchestration policies and prompts live in the disposable **Brain** layer (`rules/`, `skills/`).

## 1. Architecture Boundaries (Brain / Session / Hands)

* **Brain (`rules/`, `skills/`, `workflows/`)**: Lean (`<= 30` lines) always-on Fold-First ladder, 1-hop priority, Spec templates, `/swarm` & `/run-workflow` composition guides, and Communication Charter (`v3.1`).
* **Session (Immutable Artifacts & `SPEC.md`)**: Numbered append-only attempt history under `<out-dir>/attempts/attempt-NNN/` with atomic top-level latest publication (`summary.json` written last), plus `changes.patch`, `verification.json`, and `parallel-summary.json`. `resume` discovery ignores `attempts/*/` duplicates.
* **Hands (`scripts/codex_orch/`, `config/profiles.json`, thin `.sh` shims)**: Zero-dependency Python 3 stdlib execution kernel invoked via `exec python3 -I -B` (immune to CWD shadowing and Bash offset self-edit crashes), separating shared core logic from thin host adapters (`host_adapter.py`).

For `resume --id`, discovery checks the run root (`RUNS_DIR`) and directories up to eight levels below it. An explicit `--out-dir` also supplies a direct summary candidate and enables a three-level search under its parent when the parent is outside `RUNS_DIR` and is neither the filesystem root, a system temp directory, nor the home directory. Discovery prunes symlinks and attempt, worktree, Git, Python cache, and Node dependency directories. The newest matching top-level summary supplies the prior model and eligible writable directories.

Resume summaries record `resume_bloat_warning` and any `resume_bloat_reasons`, plus `prior_session` metadata when a match is available. The prior summary link uses the immutable attempt summary when available, including when `resume` reuses `--out-dir`. A prior run with at least 1,000,000 input tokens or status `timeout` or `stall_timeout` emits an advisory warning without changing the exit code.

## 2. Fail-Closed Exit Code & Status Contract

Wrapper status is strictly separated from child `codex_exit_code`:

| Wrapper `status` | Wrapper `exit_code` | Condition |
| :--- | ---: | :--- |
| `ok` | `0` | Child exited `0`, no cancellation/timeout/quota failure, requested schema valid, path policies clean, custody verified, and host verification (if requested) passed. |
| Usage / Preflight error | `2` | Invalid CLI args, unreadable/unsupported `--schema` keyword, invalid trusted `.codex-orchestrator.json` (e.g. forbidden `before_run` hook), unreadable explicit config/rules, bidirectional self-hosting guard hit, or `--dirty-policy fail` on dirty checkout. **Must exit before creating `--out-dir`.** |
| `schema_error` | `3` | Child exited `0` but `last_message.md` failed JSON or JSON Schema validation when `--structured`/`--schema` was requested. |
| `tdd_red_failed` | `4` | Opt-in `--tdd` Stage RED did not produce a genuine collected assertion failure (rejects `Ran 0 tests`, `collected 0 items`, `ImportError`, `ModuleNotFoundError`, `SyntaxError`, `ERROR collecting`, or exit `0`). |
| `protected_path_violation` / `scope_violation` | `5` | Worker added, modified, deleted, or renamed a file matching `protected_paths` (including frozen TDD tests) or outside non-empty `allowed_paths`. Worktree is preserved. |
| `verification_failed` | `6` | Host-side Proof-of-Work (`--verify-cmd`/`--verify-preset`) or TDD Stage GREEN failed (non-zero exit, tree mismatch, 0 collected tests, or unexpected skips without `--tdd-allow-skips`). |
| `paused_quota` | child code or `75` | Child exited **non-zero** with unambiguous quota/rate-limit signals in `stderr` or JSONL `error`/`turn.failed` events (never triggered when `codex_exit_code == 0` or by normal command/prose output). |
| `timeout` | `124` | Hard monotonic wall timeout (`--timeout`) expired and triggered process-tree cleanup. |
| `stall_timeout` | `125` | Opt-in monotonic event inactivity timeout (`--stall-timeout`) expired and triggered process-tree cleanup. |
| `cancelled` | `128 + signal` | Wrapper/supervisor received `SIGTERM`/`SIGINT`, triggered process-tree cleanup, and failed closed even if the child exited `0`. |
| `error` | child exit code / `1` | Child exited non-zero, custody check failed, or fresh matching `summary.json` / `invocation_id` missing. |

The Codex supervisor and host verification snapshot descendant PIDs and PGIDs before signaling the root group, including tools that started separate process groups. Cleanup tracks new descendants during the grace period and verifies each tracked PID's start time before using it to target `SIGKILL`. A finished root process with a surviving root group still triggers cleanup. The supervisor also samples descendants every 0.5 seconds while the root is running. On POSIX systems such as macOS, without OS subreapers or cgroups, a descendant that detaches into a separate PGID and outlives a parent that exits before the first sample can escape cleanup.

## 3. Declarative Profiles (`config/profiles.json`)

Model defaults, effort defaults, speed tier capabilities (`supports_ultrafast`, `downgrade_ultrafast_to`), `max_cmds` defaults, and bundled schema mappings live declaratively in `skills/codex-orchestrator/config/profiles.json` (overridable via `--profiles-file` or `CODEX_ORCH_PROFILES_FILE`).

## 4. Private-Index Patch Custody & Ownership Table (`parallel_runner.py`)

1. Resolve `base_oid` (`git rev-parse --verify HEAD`) per selected checkout root (`git_root`).
2. Reject unmerged index entries (`git ls-files -u`). Snapshot final Git-visible tree via racy-git-safe private `GIT_INDEX_FILE` (`copy real index -> add -A -> write-tree -> unlink -> read-tree staged_tree -> add -A -> write-tree`) capturing commits, force-staged ignored files, unstaged edits, deletions, and new non-ignored files while ignoring ordinary `.gitignore` caches.
3. Derive `changes.patch` and `changed_files` from the exact `(base_oid, final_tree_oid)` pair and verify independent reconstruction (`reconstructed_tree_oid == final_tree_oid`) before removing any worktree. Never fall back to `shutil.rmtree` on Git cleanup failure.
4. Run merge checks in atomic private temp dirs, reporting `base_reconstruct_ok` separately from `target_apply_ok`.
5. For multi-task `/dag` runs, auto-inject a public **Parallel Ownership Table** into per-task `effective_spec.md` (without mutating caller specs) and enforce `allowed_paths`/`protected_paths` fail-closed.

## 5. Trusted Repo Contract, Proof-of-Work & Opt-In Independent TDD (`verify.py`)

* **Trusted Policy Provenance**: Optional `.codex-orchestrator.json` is loaded strictly from `git show <base_oid>:.codex-orchestrator.json` or explicit `--contract <FILE>` — NEVER from the Codex-modified candidate tree. Only declarative keys (`version`, `allowed_paths`, `protected_paths`, `dirty_policy`, `max_timeout_s`, `verify_commands` as argv lists) are permitted; arbitrary shell hooks (`before_run`, `after_run`) are rejected at preflight.
* **Host-Side Proof-of-Work (`verification.json`)**: Runs `verify_argv` with `shell=False` and `build_child_env()` (credentials stripped, `GIT_CEILING_DIRECTORIES` scoped) inside a clean detached verify worktree reconstructed from `base_oid` + verified patches, recording `(base_oid, test_snapshot_tree, candidate_tree_oid, reconstructed_tree_oid, argv, cwd_rel, policy_source, exit_code, redacted tails)`. *Trust boundary*: verification executes under the local OS user account rather than a kernel/VM sandbox; run inside an OS/container sandbox when evaluating untrusted third-party repositories.
* **Opt-In Cross-Model TDD (`--tdd`)**: Two-snapshot protocol (`base_oid -> test_snapshot_commit`, requiring a non-empty frozen test set via `--tdd-test-dir` or `--tdd-test-path`): verify true RED before launching Codex -> run Codex relative to `test_snapshot_commit` with exact-path + glob `protected_paths` enforcement -> verify true GREEN requiring positive collected test execution count (`collected > 0`) and zero unexpected skips (unless `--tdd-allow-skips`).

## 6. Security, Stdout Hygiene & Native Composition

* **Credential Isolation**: `codex_orch.env.build_child_env` strips host SSH/cloud/platform tokens while preserving Codex auth and runtime essentials; `ensure_private_dir` rejects symlinks and verifies POSIX owner + `0700` mode; secrets are redacted from diagnostics.
* **Bounded Stdout (`--stdout-mode auto|compact|full`)**: Default `auto` compacts `last_message.md > 4096` bytes into a UTF-8-safe preview with file pointers.
* **Loose Antigravity Composition**: Use native `/swarm` (`land-before-kill`) + `/review` for interactive collaborative coding with external Codex final audit; use reusable read-only `/run-workflow` templates (`workflows/research_parallel.py`, `workflows/debate_multi_critic.py`) for card-based multi-slice research/critique.
