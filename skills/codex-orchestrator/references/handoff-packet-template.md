# Session Handoff Packet Template (`v3.7`)

Write your draft Handoff Packet (`1.5K–3.5K` tokens; hard bounds `400 B – 16 KB`, with a `<= 16 KB` draft budget (16,384 bytes)) following all 8 sections below before invoking `handoff.sh create`. Compress prior-hop history into current facts, decisions, and pitfalls before adding current evidence. Remove every `<!-- ... -->` comment before submission (`handoff.sh` fails closed on unresolved placeholders, missing sections, nonexistent paths in Sections 6–7, or unredacted secrets). Section `## 9. Deterministic Runtime Snapshot` is collected and appended automatically by `handoff.sh`.

```markdown
## 1. Objective & Immutable Constraints
- **User Goal**: State the verbatim overarching objective and definition of done.
- **Non-Negotiable Constraints**: Record coding standards, dependency limits, language/communication rules, and remote/host safety boundaries.
- **Standing Approvals**: List explicit approvals already granted by the user (e.g., approved plan artifact path, authorized remote hosts, opt-outs).

## 2. Current Phase & Remaining Checklist
- [x] Phase 1 — Completed item (verified via `<command / artifact>`)
- [~] Phase 2 — Current milestone state
- [ ] Phase 3 — Remaining milestone
- **Immediate Next Atomic Action**. State the first actions after cold-start verification:
  - **Main Track**. Specify the primary manager decision or one-hop `run_codex.sh` action.
  - **Parallel Subagent Track (Optional)**. Specify a short atomic wave to launch in parallel on Turn 1. Use `remote-runner` for sync or Launch-Verify-Return. Dispatch built-in `research` on Turn 1 when comprehension requires >= 3 code or data files.

## 3. Verified Facts & Evidence Anchors
- Separate confirmed facts from hypotheses. Cite exact `file:line`, commit SHAs, command exit codes, and measured metrics.

## 4. Architecture & Decisions Locked
- Record settled design decisions and *why* each was chosen so the successor does not re-litigate closed tradeoffs.

## 5. Dead Ends & Do-Not-Repeat Pitfalls
- List failed approaches, root causes of resolved bugs, and environment quirks (e.g., missing local packages, non-default SSH ports, API limitations) so the successor never repeats them.

## 6. Active Working Set
- **Immediate Touch**. Give at most 1–2 exact paths with target line ranges for the next atomic action (`/absolute/path/to/primary_file.py:1-120`). Read only those fragments when needed.
- **Reference Map (Do NOT Pre-Read)**. List remaining paths and their roles for on-demand bounded reads or the built-in `research` subagent.

## 7. Codex Orchestrator & Artifact State
- Active `/tmp/codex-orchestrator/<task-id>/` paths (`spec.md`, `contract.md`, `summary.json`, `parallel-summary.json`, `verification.json`, `changes.patch`, `corpus/`).
- Active Codex `thread_id`s and whether each is healthy for `resume` (`< 1,000,000` input tokens, non-timeout) or requires a fresh run.
- Approved conversation artifacts (e.g., `implementation_plan.md`).

## 8. Open Risks, Unverified Boundaries & Pending User Decisions
- Explicitly list what has NOT been verified yet (e.g., remote GPU run, live integration check) and any unresolved decisions requiring user input.
```

## Anti-Patterns to Avoid

- Keep each subagent assignment to one atomic wave. Multi-phase assignments and multi-hour waits delay the Quiescence Gate before `/handoff`.
- Do **not** paste raw build logs, long stack traces, or full source files (link exact paths and summarize verified root causes instead).
- Do **not** omit rejected dead ends or implicit user preferences stated early in the session.
- Do **not** include API keys, SSH passwords, or `ANTIGRAVITY_CSRF_TOKEN`.
