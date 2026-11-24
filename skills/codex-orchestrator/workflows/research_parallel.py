"""Antigravity /run-workflow template for independent, read-only corpus research."""

import json
import shlex


RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["verified", "inferred", "uncertain"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "quote_or_location": {"type": "string"},
                },
                "required": ["statement", "confidence", "source_ids", "quote_or_location"],
            },
        },
        "knowledge_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "claims", "knowledge_gaps"],
}

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_claim": {"type": "string"},
                    "counter_argument": {"type": "string"},
                    "evidence_or_location": {"type": "string"},
                },
                "required": ["target_claim", "counter_argument", "evidence_or_location"],
            },
        },
    },
    "required": ["status", "summary", "challenges"],
}


async def main():
    corpus_dir = args["corpus_dir"]
    slices = args["slices"]
    out_dir = args["out_dir"].rstrip("/")

    def worker_prompt(mode, worker_dir, spec):
        spec_path = f"{worker_dir}/spec.md"
        command = shlex.join([
            "run_codex.sh", mode, "--structured", "--ephemeral", "--dir", corpus_dir,
            "--spec", spec_path, "--out-dir", worker_dir,
        ])
        return (
            f"Create the artifact directory {worker_dir!r} and save this brief at {spec_path!r}:\n"
            f"{spec}\n\n"
            "Use the installed codex-orchestrator scripts/run_codex.sh for this command:\n"
            f"{command}\n"
            "Keep the corpus read-only and write only to the assigned artifact directory. "
            "Return the wrapper status from summary.json and the requested evidence from its "
            "structured last message. If the wrapper fails, summarize the diagnostics."
        )

    async def research_slice(item):
        idx, slice_spec = item
        name = slice_spec["name"]
        brief = (
            f"Research slice {name!r}\n"
            f"{slice_spec['question']}\n\n"
            "Ground conclusions in the corpus. Cite source IDs and precise locations, "
            "distinguish verified claims from inferences, and record evidence gaps."
        )
        return await agent(
            prompt=worker_prompt("research", f"{out_dir}/slice-{idx:03d}", brief),
            schema=RESEARCH_SCHEMA,
            role=f"Research {name}",
            type_name="codex-worker",
        )

    phase("Research corpus slices")
    results = await parallel(list(enumerate(slices, start=1)), research_slice)
    summary = {
        "slices": [{"name": item["name"], "result": result} for item, result in zip(slices, results)],
    }

    if args.get("run_critique", False):
        phase("Blind critique")
        questions = "\n".join(f"- {item['name']}: {item['question']}" for item in slices)
        brief = (
            "Independently critique the framing and evidence for these questions using only "
            "the source corpus and questions below. Identify unsupported assumptions, plausible "
            "counter-evidence, and precise source locations.\n\n"
            f"{questions}"
        )
        summary["critique"] = await agent(
            prompt=worker_prompt("critique", f"{out_dir}/blind-critique", brief),
            schema=CRITIQUE_SCHEMA,
            role="Blind corpus critic",
            type_name="codex-worker",
        )

    log(json.dumps(summary, ensure_ascii=False))


await main()
