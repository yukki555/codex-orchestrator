"""Antigravity /run-workflow template for independent, read-only proposal critique."""

import json
import shlex


CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "target_claim": {"type": "string"},
                    "flaw_type": {"type": "string"},
                    "counter_argument": {"type": "string"},
                    "evidence_or_location": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": [
                    "severity", "target_claim", "flaw_type", "counter_argument",
                    "evidence_or_location", "suggested_fix",
                ],
            },
        },
    },
    "required": ["status", "summary", "challenges"],
}


async def main():
    proposal_path = args["proposal_path"]
    target_dir = args["target_dir"]
    out_dir = args["out_dir"].rstrip("/")
    perspectives = args.get("perspectives", [
        "systems-reliability", "correctness-edge-cases", "cost-simplicity",
    ])

    async def critique(item):
        idx, perspective = item
        worker_dir = f"{out_dir}/critic-{idx:03d}"
        spec_path = f"{worker_dir}/spec.md"
        command = shlex.join([
            "run_codex.sh", "critique", "--structured", "--ephemeral", "--dir", target_dir,
            "--spec", spec_path, "--out-dir", worker_dir,
        ])
        return await agent(
            prompt=(
                f"Create the artifact directory {worker_dir!r} and save this brief at {spec_path!r}:\n"
                f"Read the proposal at {proposal_path!r} and critique it independently from "
                f"the {perspective!r} perspective. Inspect supporting evidence in the target directory. "
                "Give each challenge a severity, target claim, counterargument, precise evidence "
                "location, and suggested fix. Use P0 for blockers and P1 for major issues.\n\n"
                "## Output Clarity Contract\n"
                "Write complete, self-contained sentences with exact evidence anchors. Omit internal "
                "shorthand, step codes, and telegraphic fragments. Disclose gaps, skipped checks, "
                "unverified assumptions, and blockers.\n\n"
                "Use the installed codex-orchestrator scripts/run_codex.sh for this command:\n"
                f"{command}\n"
                "Keep the proposal and target read-only and write only to the assigned artifact "
                "directory. Return the wrapper status from summary.json and the requested evidence "
                "from its structured last message."
                " Normalize findings into complete sentences without carrying over Codex shorthand. "
                "Preserve exact anchors, include artifact pointers, and state gaps. "
                "Summarize diagnostics if the wrapper fails."
            ),
            schema=CRITIC_SCHEMA,
            role=f"Critic {perspective}",
            type_name="codex-worker",
        )

    phase("Critique proposal in parallel")
    results = await parallel(list(enumerate(perspectives, start=1)), critique)
    prioritized = [
        {**challenge, "perspective": perspective}
        for severity in ("P0", "P1")
        for perspective, result in zip(perspectives, results)
        for challenge in (result or {}).get("challenges", [])
        if challenge["severity"] == severity
    ]
    log(json.dumps({
        "prioritized_challenges": prioritized,
        "critics": [{"perspective": name, "result": result} for name, result in zip(perspectives, results)],
    }, ensure_ascii=False))


await main()
