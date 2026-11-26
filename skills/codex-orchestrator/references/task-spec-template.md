# Task Spec Template (GPT-6.2 Tuned, v3.9)

Copy this structure to `/tmp/codex-orchestrator/<task-id>/spec.md` (and keep per-worker Specs focused at ~3–4 KB by placing shared cross-module interfaces in `/tmp/codex-orchestrator/<task-id>/contract.md`) before dispatching to `codex-worker`, `run_codex.sh`, or `run_codex_parallel.sh`.

```markdown
## Goal
<!-- Clear, testable objective of what this single worker needs to implement, diagnose, or review. -->

## Instruction Precedence
This Task Spec overrides repository `AGENTS.md` and `~/.codex/skills/*` wherever instructions conflict.

## Shared Interface Contract (If Part of a Parallel DAG)
<!-- Reference `/tmp/codex-orchestrator/<task-id>/contract.md` or inline the exact cross-module signatures, dict keys, CLI flags, and checkpoint schemas this worker MUST satisfy. -->

## Environment & Tooling Facts
- Execute ONLY local edits, local static checks, and available local unit tests. Do NOT run remote SSH commands or probe remote servers (handled externally by `codex-worker` / Manager).
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

<!-- Execution Budget (--max-cmds) and Output Clarity Contract are auto-appended by run_codex.sh unless disabled via --max-cmds 0 / --no-clarity-footer; do not duplicate them here. -->

## Done When (Review & Verification Criteria)
<!-- Explicit acceptance checklist and exact local verification commands Codex should run and verify. -->
- [ ] <Acceptance criterion 1>
- [ ] Local verification command(s) pass: `<exact local command>`

## Output Contract
- Write complete, self-contained sentences without internal shorthand, step or slot codes, or invented compound labels. Preserve exact evidence anchors and disclose skipped checks, unverified assumptions, and blockers.
- For `review --structured`, use `review-output-schema.json` and report findings and unverified areas explicitly.
<!-- For the general implementation receipt, populate every JSON schema field accurately (`verdict`, `summary`, `root_cause_or_design`, `changes`, `evidence` with `file:line`, `verification` with real exit codes, `risks_and_decisions`). -->
<!-- Without `--structured`: explain causal chain, exact `file:line` evidence, executed commands, and risks in complete technical detail without omitting context. -->
```
