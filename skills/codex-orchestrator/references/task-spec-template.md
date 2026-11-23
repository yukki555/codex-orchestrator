# Task Spec Template (GPT-6.2 Tuned, v2.1)

Copy this structure to `/tmp/codex-orchestrator/<task-id>/spec.md` (and keep per-worker Specs focused at ~3–4 KB by placing shared cross-module interfaces in `/tmp/codex-orchestrator/<task-id>/contract.md`) before dispatching via `run_codex.sh`, `run_codex_parallel.sh`, or `codex-worker`.

```markdown
## Goal
<!-- Clear, testable objective of what this single worker needs to implement, diagnose, or review. -->

## Instruction Precedence
This Task Spec overrides repository `AGENTS.md` and generic skills wherever instructions conflict.

## Shared Interface Contract (If Part of a Parallel DAG)
<!-- Reference `/tmp/codex-orchestrator/<task-id>/contract.md` or inline the exact cross-module signatures, dict keys, CLI flags, and checkpoint schemas this worker MUST satisfy. -->

## Environment & Tooling Facts
- Execute ONLY local edits, local static checks, and available local unit tests. Do NOT run remote SSH commands or probe remote servers (handled externally by `agy` Manager / `codex-worker`).
- Do NOT read `~/.codex/skills/*` or scan host directories (`rg --files /...`) for missing runtimes when environment availability is stated below.
- Local runtime facts: <!-- e.g., "macOS host has no PyTorch/zarr; use python3 -m py_compile / ast.parse / bash -n locally." -->

## Context & Relevant Files
<!-- Key files, symbols, architecture notes, and verified error snippets Codex should read first. -->
- `<path/to/file>`: <role in task>

## Allowed Scope
<!-- For `consult`: state "Read-only inspection; do not modify any files." -->
<!-- For `implement`: list exact files this worker is allowed to create or modify (must not overlap sibling parallel workers). -->

## Constraints & Non-Goals
- Reuse existing repository helpers, types, and patterns; avoid unnecessary abstractions or unrelated refactors.
- Only add or modify tests explicitly requested below or needed to verify the targeted change/bug fix.
<!-- Add task-specific invariants, backward-compatibility rules, or files not to touch. -->

## Autonomy & Hard Stop Criteria
- Carry authorized work through editing and local verification; do not stop at a proposed plan when you can proceed.
- Make reasonable, reversible assumptions for routine implementation details and record them in your output.
- Stop early and report `blocked` only when missing information materially alters correctness/scope or you hit an unresolvable blocker.

## Execution Budget
- Batch multi-file reads and searches into 1–2 concise `python3` or shell commands instead of sequential per-file calls.
- Skip broad repo scans (`rg --files`, `find`, `ls -R`) when exact paths are named above.
- Treat verified inline snapshots/metrics in this Spec as ground truth; do not re-read them unless checking a discrepancy.
- Keep command stdout concise (`< 20 KB`) and stay within budget (`<= 8` commands for `consult`/`review`/`resume`, `<= 12` for scoped `implement`; pass `--max-cmds 0` only when unrestricted exploration is needed).

## Done When (Review & Verification Criteria)
<!-- Explicit acceptance checklist and exact local verification commands Codex should run and verify. -->
- [ ] <Acceptance criterion 1>
- [ ] Local verification command(s) pass: `<exact local command>`

## Output Contract
<!-- With `--structured`: populate every JSON schema field accurately (`verdict`, `summary`, `root_cause_or_design`, `changes`, `evidence` with `file:line`, `verification` with real exit codes, `risks_and_decisions`). -->
<!-- Without `--structured`: explain causal chain, exact `file:line` evidence, executed commands, and risks in complete technical detail without omitting context. -->
```
