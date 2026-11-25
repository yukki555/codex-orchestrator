---
name: debate
description: >-
  Run a dual-model blind red-team critique and decision debate (CLI or /run-workflow multi-critic
  template): strip bias into a neutral brief, challenge assumptions with P0-P3 severity, verify
  counter-arguments, and present a clear Chinese decision matrix.
---

# Dual-Model Blind Red-Team & Decision Debate (`/debate`)

Act as **Decision Moderator & Fact-Checker** in Google Antigravity pairing **Gemini** with an independent **Codex `gpt-6.2-astra`** red-team critic to eliminate single-model confirmation bias on architecture, product, or research decisions.

Execute strictly in order:

1. **Neutralize the Proposal (Anti-Sycophancy Framing)**:
   * Write `/tmp/codex-orchestrator/<task-id>/proposal.md` summarizing the candidate decision, goals, constraints, and supporting evidence **without** revealing who wrote it or signaling which option is preferred.
   * Include pointers to concrete local files, metrics, or charts (use `--image <file>` when visual evidence matters).
2. **Independent Codex Red-Team Pass (`critique`)**:
   * **Single critic (1-hop default)**: Write `/tmp/codex-orchestrator/<task-id>/spec-critique.md` using `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/research-spec-template.md` and invoke:
     `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/scripts/run_codex.sh critique --dir "<target_dir>" --structured --effort high --speed fast --ephemeral --timeout 240 --spec /tmp/codex-orchestrator/<task-id>/spec-critique.md --out-dir /tmp/codex-orchestrator/<task-id>/critique`
   * **Multi-perspective critics (high-stakes decisions)**: Fan out `2–3` critic personas via `run_codex_parallel.sh --mode critique --structured --manifest ...` OR run the bundled Antigravity `/run-workflow` template `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/workflows/debate_multi_critic.py` via `run_workflow(ScriptPath=..., Args={"proposal_path": ..., "target_dir": ..., "out_dir": ...})`.
3. **Adjudicate & Fact-Check Challenges**:
   * Read `summary.json` (`structured_valid: true`) and `last_message.md`.
   * Independently verify every `P0` and `P1` challenge against actual code, data, or source docs via `read_file` / read-only checks. Separate **substantiated challenges** from **false alarms / out-of-scope concerns**.
4. **Chinese Decision Brief**:
   * Synthesize strengths, substantiated P0/P1 risks, missing alternatives, verified vs derived vs unverified boundaries, and an option-by-option decision matrix in calm, precise natural Chinese following the **deep research / red-team / analysis route** of the Communication Charter v3.2 in `~/.gemini/config/plugins/codex-orchestrator/skills/codex-orchestrator/references/user-brief-template.md`. Never paste raw Codex output.

Apply the Charter’s Synthesis Firewall. Preserve exact evidence anchors and measured values, rewrite working shorthand and internal codes as self-contained Chinese, and disclose gaps. Routine `consult` defaults to `gpt-6.2-sol`. Deep `research`, `critique`, and `analyze` default to `gpt-6.2-astra`.
