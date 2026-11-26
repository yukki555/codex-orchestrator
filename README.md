# Codex Orchestrator (`v3.9.0`)

A **Dual-Brain** orchestration plugin for **Google Antigravity** pairing **Gemini** (Principal Manager, Corpus Collector & Verifier) with **local OpenAI Codex CLI** (High-Execution Staff Implementer, Independent Reviewer & Red-Team Analyst) across software engineering (`/impl`, `/review`, `/dag`), long-horizon session continuity (`/handoff`), and deep research / red-team debate / experiment analysis (`/research`, `/debate`, `/analyze`).

## What's New in v3.9.0

* The slim cold start loads a short always-on policy and concise orchestrator and handoff skills. Advanced flags, profiles, contracts, and examples live in [`cli-reference.md`](./skills/codex-orchestrator/references/cli-reference.md).
* Two-tier `read_noise` first suggests built-in `research` and bounded reads. If reads continue or accumulate again after a research dispatch, it urges another delegation or targeted extraction.
* The `codex-read-guard` `PreToolUse` hook blocks oversized main-loop `read_file` requests and directs the caller to a tighter range, targeted extraction, or `research`.
* Denied and otherwise errored `read_file` calls are excluded from read telemetry, including the prior-turn signal used for token spike nudges.

## Historical release v3.8.0

* `consult` now defaults to `gpt-6.2-sol`. Set `--verbosity low|medium|high|inherit` to override the mode default, or use `--no-clarity-footer` to omit the Output Clarity Contract from exec prompts.
* `review --structured` uses `review-output-schema.json` for severity-ranked findings and explicit unverified areas. All five schemas guide workers to use exact evidence references and self-contained prose.
* `implement` defaults to `--stdout-mode facts`, which prints structural counts and artifact pointers. Host patch and verification metadata precede the preview. Mid-flight Codex commentary stays in failure summaries on disk.
* Communication Charter v3.2 requires diff and host verification checks against the Spec before reading the model’s implementation report. Its Synthesis Firewall preserves exact evidence and rewrites internal shorthand.

## What's New in v3.7.0

* Route cross-host sync, multi-turn remote probing, and job Launch-Verify-Return to `remote-runner`. Use the built-in `research` subagent for broad read-only exploration across at least five unfamiliar files or log directories.
* `PreInvocation` suggests delegation from checkpoint-scoped tool counts, using the existing transcript scan and no extra process. Disable these suggestions with `CODEX_NOISE_NUDGE=off`.
* A dual-track Handoff Packet can pair the main action with an optional subagent wave on the successor's first turn. `--resume-mode auto` tags the successor `handoff-mode:auto` so later hops inherit auto-handoff unless `CODEX_HANDOFF_MODE` explicitly overrides it.

## What's New in `v3.6.0`

* **Native Antigravity Session Handoff (`/handoff`, `handoff.sh`)**: Adds zero-dependency context telemetry (`context_telemetry.py`), native `PreInvocation` + `Stop` watermark hooks (`codex-context-handoff` via `hook_context_guard.py`), and a lossless 9-section Handoff Packet + `product.v1.ConversationService` relay engine (`handoff.py`) with dual-constraint model-aware thresholds (`Soft = min(200K, 45% × cpLimit)`, `Hard = min(300K, 65% × cpLimit)`), `projectId`/`environmentId`/`conversationGroupId`/model preservation with tier fallback, anti-split-brain & `max_hops` circuit breakers, and exit `7` graceful fallback when local RPC is sandbox-blocked.

## What's New in `v3.5.1`

The Headwind Reliability Patch cleans up descendant process groups on timeouts and cancellation, finds prior `resume` summaries in deep and custom output directories, and warns before resuming large or timed-out sessions.

## What's New in `v3.5.0` (Building on `v3.2.0`)

* **Pure-Python `codex_orch/` Execution Kernel**: Consolidated both `run_codex.sh` (~12-line shim) and `run_codex_parallel.sh` (~11-line shim) onto a zero-dependency Python 3 stdlib package (`single_runner.py`, `parallel_runner.py`, `profiles.py`, `verify.py`, `host_adapter.py`, `supervisor.py`, `schema_check.py`, `env.py`, `compact_stdout.py`) invoked via `exec python3 -I -B` for direct signal delivery, zero CWD import shadowing, and zero Bash self-edit crashes.
* **Declarative Profiles & Immutable Attempt History**: Externalized model defaults, reasoning efforts, speed capabilities (`ultrafast` downgrade rules), `max_cmds`, and schema mappings to [`config/profiles.json`](./skills/codex-orchestrator/config/profiles.json). Each invocation preserves immutable per-attempt artifacts under `<out-dir>/attempts/attempt-NNN/` with atomic top-level `summary.json` publication.
* **Trusted `.codex-orchestrator.json` & Host-Side Proof-of-Work**: Resolves optional repo contracts strictly from pre-run `base_oid` or explicit `--contract` (never from Codex-modified candidate trees; rejects arbitrary `before_run` hooks), enforces hard `allowed_paths`/`protected_paths` gates (exit `5`), and runs `verify_argv` (`shell=False`, stripped child env) on the reconstructed tree to emit `verification.json` (exit `6` on failure).
* **Opt-In Cross-Model Independent TDD (`--tdd`)**: Two-snapshot (`base_oid -> test_snapshot`) protocol validating genuine RED before launching Codex (exit `4` on fake RED / import crash), enforcing protected test paths (exit `5`), and validating genuine GREEN with zero unexpected skips (exit `6`).
* **DAG Public Ownership Table & Native Composition**: `run_codex_parallel.sh` auto-injects a sibling ownership table into per-task `effective_spec.md` without mutating caller specs; ships reusable read-only Antigravity `/run-workflow` templates ([`research_parallel.py`](./skills/codex-orchestrator/workflows/research_parallel.py), [`debate_multi_critic.py`](./skills/codex-orchestrator/workflows/debate_multi_critic.py)) and documents `/swarm` -> `/review` cross-model final auditing.
* **Cross-Host Guard Boundary**: Factored `hook_delegate_guard.py` (`strict <= 10` lines default + stateless `CODEX_GUARD_PROFILE=pragmatic <= 30` lines existing code edits) behind `codex_orch.host_adapter` for clean future `gemini-cli` convergence.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s ~/.gemini/config/plugins/codex-orchestrator/tests -p 'test_*.py' -v
agy plugin validate --strict ~/.gemini/config/plugins/codex-orchestrator
```

## Repository Structure

| Path | Role |
| :--- | :--- |
| `plugin.json`, `assets/logo.svg` | Antigravity plugin manifest (`v3.9.0`) and icon |
| `SPEC.md` | Behavioral specification (`v3.9`) for exit codes, patch custody, Proof-of-Work, TDD, denoising, and Session Handoff |
| `hooks.json` | Root `PreToolUse` (`codex-delegate-guard`, `codex-read-guard`) and `PreInvocation` + `Stop` (`codex-context-handoff`) lifecycle hook registrations |
| `rules/AGENTS.md` | Lean (`<= 30` lines) always-on Fold-First ladder, guardrails, handoff policy, and Communication Charter v3.2 pointer |
| `agents/codex-worker.md` | Context-compression subagent for multi-round Codex repair, local build/test logs, and read-only `/run-workflow` slices |
| `agents/remote-runner.md` | Remote SSH, file sync, cluster operations, and job Launch-Verify-Return |
| `skills/codex-orchestrator/config/profiles.json` | Declarative model, effort, speed, command-budget, schema, and `context_handoff` threshold routing table |
| `skills/codex-orchestrator/scripts/` | Thin shims (`run_codex.sh`, `run_codex_parallel.sh`, `handoff.sh`, `hook_delegate_guard.py`, `hook_read_guard.py`, `hook_context_guard.py`) + zero-dependency `codex_orch/` Python package |
| `skills/codex-orchestrator/workflows/` | Reusable Antigravity `/run-workflow` read-only templates (`research_parallel.py`, `debate_multi_critic.py`) |
| `skills/codex-orchestrator/references/` | 5 strict JSON schemas, Spec & Handoff Packet templates, and Communication Charter (`user-brief-template.md`) |
| `tests/` | Zero-dependency conformance suites (`test_plugin.py`, `test_handoff.py`) covering `v3.9` and earlier contracts |
