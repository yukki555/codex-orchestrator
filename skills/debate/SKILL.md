---
name: debate
description: >-
  Run a dual-model blind red-team critique and decision debate: strip bias into a neutral brief,
  have Codex challenge assumptions and missing alternatives with structured P0-P3 severity, verify
  counter-arguments, and present a clear Chinese decision matrix.
---

# Dual-Model Blind Red-Team & Decision Debate (`/debate`)

Act as **Decision Moderator & Fact-Checker** pairing **Gemini** with an independent **Codex `gpt-6.2-astra`** red-team critic to eliminate single-model confirmation bias on architecture, product, or research decisions.

Execute strictly in order:

1. **Neutralize the Proposal (Anti-Sycophancy Framing)**:
   * Write `/tmp/codex-orchestrator/<task-id>/proposal.md` summarizing the candidate decision, goals, constraints, and supporting evidence **without** revealing who wrote it or signaling which option is preferred.
   * Include pointers to any concrete local files, metrics, or charts (use `--image <file>` if plots/diagrams are part of the evidence).
2. **Independent Codex Red-Team Pass (`critique`)**:
   * Write `/tmp/codex-orchestrator/<task-id>/spec-critique.md` using the `critique` section of `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/research-spec-template.md`.
   * Invoke directly via `run_command`:
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh critique --dir "<target_dir>" --structured --effort high --speed fast --timeout 240 --spec /tmp/codex-orchestrator/<task-id>/spec-critique.md --out-dir /tmp/codex-orchestrator/<task-id>/critique`
   * For multi-stakeholder or high-cost decisions, fan out `2–3` parallel critic personas (e.g., *systems reliability*, *algorithmic validity*, *cost & simplicity*) via `run_codex_parallel.sh --mode critique --structured --manifest ...`.
3. **Adjudicate & Fact-Check Challenges**:
   * Read `summary.json` (`structured_valid: true`) and `last_message.md`.
   * Independently verify every `P0` and `P1` challenge against actual code, data, or source docs via `read_file` / read-only checks. Explicitly separate **substantiated challenges** from **false alarms / out-of-scope concerns**.
4. **Chinese Decision Brief**:
   * Synthesize strengths, substantiated P0/P1 risks, missing alternatives, unverified assumptions, and a concrete option-by-option decision matrix in natural Chinese using **Template B** in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`. Never paste raw Codex output.
