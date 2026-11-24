# Codex Orchestrator (`v3.5.0`)

A **Dual-Brain** orchestration plugin for **Google Antigravity** pairing **Gemini** (Principal Manager, Corpus Collector & Verifier) with **local OpenAI Codex CLI** (High-Execution Staff Implementer, Independent Reviewer & Red-Team Analyst) across software engineering (`/impl`, `/review`, `/dag`) and deep research / red-team debate / experiment analysis (`/research`, `/debate`, `/analyze`).

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
| `plugin.json`, `assets/logo.svg` | Antigravity plugin manifest (`v3.5.0`) and icon (`agy plugin validate --strict` clean) |
| `SPEC.md` | Behavioral specification (`v3.5`) for Session/Hands exit codes, patch custody, Proof-of-Work, and TDD |
| `hooks.json` | Root `PreToolUse` hook registration (`codex-delegate-guard`, `strict` default + `pragmatic` opt-in) |
| `rules/AGENTS.md` | Lean (`<= 30` lines) always-on Fold-First ladder, guardrails, and Communication Charter v3.1 pointer |
| `agents/codex-worker.md` | Context-compression subagent (`inheritMcp: false`) for multi-round repair, SSH/GPU checks, and `/run-workflow` |
| `skills/codex-orchestrator/config/profiles.json` | Declarative model, effort, speed, command-budget, and schema routing table |
| `skills/codex-orchestrator/scripts/` | Thin shims (`run_codex.sh`, `run_codex_parallel.sh`, `hook_delegate_guard.py`) + zero-dependency `codex_orch/` Python package |
| `skills/codex-orchestrator/workflows/` | Reusable Antigravity `/run-workflow` read-only templates (`research_parallel.py`, `debate_multi_critic.py`) |
| `skills/codex-orchestrator/references/` | 4 strict JSON schemas, Spec templates, and Communication Charter (`user-brief-template.md`) |
| `tests/test_plugin.py` | Zero-dependency conformance suite covering `v3.2` + `v3.5` contracts |
