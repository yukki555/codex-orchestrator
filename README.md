# Codex Orchestrator — Antigravity CLI (`agy`) Edition (`v3.5.1`)

A **Dual-Brain** orchestration plugin for **Antigravity CLI (`agy`) & App** sharing the exact `v3.5.1` pure-Python `codex_orch/` kernel with `main`. It pairs **Gemini** (Principal Manager, Corpus Collector & Verifier) with **local OpenAI Codex CLI** (High-Execution Staff Implementer, Independent Reviewer & Red-Team Analyst) across software engineering (`/impl`, `/review`, `/dag`) and deep research / red-team debate / experiment analysis (`/research`, `/debate`, `/analyze`).

## What's New in `v3.5.1` (Converged Shared Kernel)

* **Thin Cross-Host Branch Convergence**: Shares 100% of `codex_orch/`, `config/profiles.json`, `SPEC.md`, `workflows/`, `rules/AGENTS.md`, `skills/`, and `agents/codex-worker.md` with `main`, keeping only a minimal `agy` CLI edition adapter layer (`plugin.json`, `README.md`, `hook_delegate_guard.py` configured with `host="gemini-cli"` to accept both `agy` `toolCall` and legacy Gemini CLI payload shapes).
* **Headwind Reliability (`v3.5.1`)**: Cleans up separate-PGID descendant process trees on timeouts/cancellation with `lstart` identity verification before `SIGKILL`, discovers prior `resume` summaries in deep (`<= 8` levels) and sibling custom output directories with immutable `attempts/` traceability, and emits non-blocking `resume_bloat_warning` alerts for prior `>= 1,000,000` input-token or timed-out sessions.
* **Pure-Python Kernel, Profiles, Proof-of-Work & `--tdd` (`v3.5.0`)**: Zero-dependency Python 3 stdlib execution kernel (`exec python3 -I -B`), declarative [`config/profiles.json`](./skills/codex-orchestrator/config/profiles.json), trusted `.codex-orchestrator.json` contracts (`base_oid` only), host-side Proof-of-Work (`verification.json`), opt-in cross-model `--tdd`, DAG public ownership tables, Communication Charter v3.1 (`user-brief-template.md`), and reusable `/run-workflow` templates.

## Prerequisites & Installation (`agy` CLI)

1. Install **Antigravity CLI (`agy`)** and ensure **OpenAI Codex CLI** (`codex` >= `0.171.0`) is signed in (`~/.codex`).
2. Clone this branch into your global Antigravity plugins directory:
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone -b gemini-cli https://github.com/yukki555/codex-orchestrator.git ~/.gemini/config/plugins/codex-orchestrator
   ```
3. Verify discovery and strict manifest compliance:
   ```bash
   agy plugin list
   agy plugin validate --strict ~/.gemini/config/plugins/codex-orchestrator
   PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s ~/.gemini/config/plugins/codex-orchestrator/tests -p 'test_*.py' -v
   ```

## Recommended `agy` CLI Settings (Turbo / Auto)

To avoid repetitive approval prompts when `agy` runs `run_codex.sh`, `run_codex_parallel.sh`, or remote SSH verification commands, configure `~/.gemini/antigravity-cli/settings.json`:

* **Turbo equivalent** (fast SSH & cross-dir workflows):
  ```json
  {
    "toolPermission": "always-proceed",
    "agentMode": "accept-edits",
    "allowNonWorkspaceAccess": true,
    "enableTerminalSandbox": false,
    "artifactReviewPolicy": "always-proceed"
  }
  ```
* **Balanced `auto` mode** (sandboxed commands auto-run; risky host ops evaluated automatically):
  ```json
  {
    "agentMode": "auto",
    "enableTerminalSandbox": true,
    "allowNonWorkspaceAccess": true
  }
  ```

## Repository Structure

| Path | Role |
| :--- | :--- |
| `plugin.json`, `assets/logo.svg` | `agy` CLI plugin manifest (`v3.5.1`) and icon (`agy plugin validate --strict` clean) |
| `SPEC.md` | Shared behavioral specification (`v3.5`) for Session/Hands exit codes, patch custody, Proof-of-Work, and TDD |
| `hooks.json` | Root `PreToolUse` hook registration (`codex-delegate-guard`, `strict` default + `pragmatic` opt-in) |
| `rules/AGENTS.md` | Lean (`<= 30` lines) always-on Fold-First ladder, guardrails, and Communication Charter v3.1 pointer |
| `agents/codex-worker.md` | Context-compression subagent (`inheritMcp: false`) for multi-round repair, SSH/GPU checks, and `/run-workflow` |
| `skills/codex-orchestrator/config/profiles.json` | Declarative model, effort, speed, command-budget, and schema routing table |
| `skills/codex-orchestrator/scripts/` | Thin shims (`run_codex.sh`, `run_codex_parallel.sh`, `hook_delegate_guard.py` with `host="gemini-cli"`) + shared `codex_orch/` kernel |
| `skills/codex-orchestrator/workflows/` | Reusable Antigravity `/run-workflow` read-only templates (`research_parallel.py`, `debate_multi_critic.py`) |
| `skills/codex-orchestrator/references/` | 4 strict JSON schemas, Spec templates, and Communication Charter (`user-brief-template.md`) |
| `tests/test_plugin.py` | Zero-dependency conformance suite covering `v3.2` + `v3.5` + `v3.5.1` contracts and `gemini-cli` guard payloads |
