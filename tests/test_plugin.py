"""Verify the Google Antigravity plugin using only the standard library."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
CORE_SKILL = SKILLS / "codex-orchestrator"
REFERENCES = CORE_SKILL / "references"
SCRIPTS = CORE_SKILL / "scripts"
GUARD = SCRIPTS / "hook_delegate_guard.py"
RULES = ROOT / "rules" / "AGENTS.md"
AGENT = ROOT / "agents" / "codex-worker.md"
SKILL_NAMES = ("codex-orchestrator", "impl", "review", "dag", "research", "debate", "analyze")


class AntigravityPluginTests(unittest.TestCase):
    def _frontmatter(self, path):
        """Read the flat YAML subset used by the bundled Markdown files."""
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:1], ["---"], path)
        self.assertIn("---", lines[1:], path)
        header = iter(lines[1:lines.index("---", 1)])
        fields = {}
        line = next(header, None)
        while line is not None:
            if not line.strip() or line.lstrip().startswith("#"):
                line = next(header, None)
                continue
            entry = re.fullmatch(r"([A-Za-z][\w-]*):(?:[ \t]+(.*))?", line)
            self.assertIsNotNone(entry, f"{path}: invalid frontmatter entry {line!r}")
            key, raw_value = entry.groups()
            self.assertNotIn(key, fields, path)
            value = (raw_value or "").strip()
            line = next(header, None)
            if re.fullmatch(r"[>|][+-]?", value):
                block = []
                indent = None
                while line is not None and (line.startswith(" ") or not line.strip()):
                    if line.strip():
                        spaces = len(line) - len(line.lstrip(" "))
                        if indent is None:
                            indent = spaces
                        self.assertGreaterEqual(spaces, indent, path)
                        block.append(line.strip())
                    line = next(header, None)
                value = " ".join(block)
            elif value in ("true", "false"):
                value = value == "true"
            elif value.startswith("["):
                self.assertRegex(value, r"^\[\s*(?:[\w-]+(?:\s*,\s*[\w-]+)*\s*,?)?\s*\]$")
                value = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            else:
                self.assertNotRegex(value, r"^[][{},&*!|>'\"%@`#]|:(?:\s|$)|\s#")
            fields[key] = value
        return fields

    def _run(self, command, **kwargs):
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=30, **kwargs
        )
        self.assertEqual(
            result.returncode,
            0,
            f"{command!r}\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )
        return result.stdout

    def test_plugin_and_hooks(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertIsInstance(plugin, dict)
        self.assertEqual(plugin["name"], "codex-orchestrator")
        self.assertEqual(plugin["displayName"], "Codex Orchestrator")
        self.assertEqual(plugin["version"], "3.1.0")

        hooks = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
        pre_tool_use = hooks["codex-delegate-guard"]["PreToolUse"]
        self.assertIsInstance(pre_tool_use, list)
        self.assertTrue(pre_tool_use)
        guard_path = "./" + GUARD.relative_to(ROOT).as_posix()
        for tool in ("write_file", "edit_file"):
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(
                        entry["matcher"].strip()
                        and re.search(entry["matcher"], tool)
                        and any(guard_path in hook["command"].split() for hook in entry["hooks"])
                        for entry in pre_tool_use
                    ),
                    f"No PreToolUse matcher routes {tool} to {guard_path}",
                )
        self.assertTrue(GUARD.is_file(), GUARD)
        self.assertTrue(os.access(GUARD, os.X_OK), GUARD)

    def test_all_output_schemas_are_strict(self):
        required_by_schema = {
            "codex": {
                "verdict", "summary", "root_cause_or_design", "changes",
                "evidence", "verification", "risks_and_decisions",
            },
            "research": {
                "verdict", "summary", "claims", "comparison_matrix", "counter_evidence",
                "knowledge_gaps", "recommended_next_checks",
            },
            "critique": {
                "verdict", "summary", "strengths", "challenges", "missing_alternatives",
                "unverified_assumptions", "decision_matrix",
            },
            "analysis": {
                "verdict", "summary", "key_metrics", "consistency_checks",
                "anomalies_and_root_causes", "verification", "next_experiments",
            },
        }

        def check_node(node, location):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    with self.subTest(node=location):
                        self.assertIs(node.get("additionalProperties"), False)
                        self.assertIsInstance(node.get("required"), list)
                        self.assertIsInstance(node.get("properties"), dict)
                        self.assertEqual(set(node["required"]), set(node["properties"]))
                for key, value in node.items():
                    check_node(value, f"{location}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    check_node(value, f"{location}[{index}]")

        for name, required in required_by_schema.items():
            path = REFERENCES / f"{name}-output-schema.json"
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema["type"], "object")
                self.assertEqual(set(schema["required"]), required)
                check_node(schema, "$")

    def test_rules_and_agent_frontmatter(self):
        self.assertTrue(RULES.is_file(), RULES)
        self.assertTrue(RULES.read_text(encoding="utf-8").strip(), RULES)

        agent = self._frontmatter(AGENT)
        self.assertEqual(agent["name"], "codex-worker")
        self.assertIs(agent["subagent"], True)
        self.assertIs(agent["mainAgent"], False)
        self.assertIsInstance(agent["tools"], list)
        self.assertIn("run_command", agent["tools"])
        self.assertNotIn("edit_file", agent["tools"])
        self.assertNotIn("replace", agent["tools"])

    def test_seven_skills_valid_and_no_stale_templates(self):
        skill_files = [SKILLS / name / "SKILL.md" for name in SKILL_NAMES]
        for path in skill_files:
            with self.subTest(frontmatter=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), path)
                skill = self._frontmatter(path)
                self.assertEqual(skill["name"], path.parent.name)
                self.assertIsInstance(skill["description"], str)
                self.assertTrue(skill["description"].strip(), path)

        for path in [
            *skill_files, RULES, AGENT, ROOT / "README.md",
            REFERENCES / "user-brief-template.md",
            REFERENCES / "research-spec-template.md",
        ]:
            with self.subTest(stale_syntax=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                for marker in ("{{args}}", "!{", "~/.gemini/extensions/"):
                    self.assertNotIn(marker, content, f"{path} contains {marker!r}")

    def test_v31_communication_charter_consistency(self):
        charter = REFERENCES / "user-brief-template.md"
        required_markers = {
            charter: (
                "沟通宪章 v3.1",
                "表达与保真五条铁律",
                "按任务量级动态路由",
                "consult",
                "轻量代码改动",
                "重量级工程交付",
                "深度调研 / 红队辩论 / 实验核查",
            ),
            RULES: ("Communication Charter", "Dynamic Structure Routing", charter.name),
            ROOT / "README.md": ("v3.1.0", "Communication Charter", charter.name),
            SKILLS / "codex-orchestrator" / "SKILL.md": ("Communication Charter v3.1", charter.name),
            SKILLS / "impl" / "SKILL.md": ("Communication Charter v3.1", "lightweight route", charter.name),
            SKILLS / "review" / "SKILL.md": ("Communication Charter v3.1", "review", charter.name),
            SKILLS / "dag" / "SKILL.md": ("Communication Charter v3.1", "heavyweight", charter.name),
            SKILLS / "research" / "SKILL.md": ("Communication Charter v3.1", "deep research", charter.name),
            SKILLS / "debate" / "SKILL.md": ("Communication Charter v3.1", "deep research", charter.name),
            SKILLS / "analyze" / "SKILL.md": ("Communication Charter v3.1", "deep research", charter.name),
        }
        paths = list(required_markers)
        for path in paths:
            content = path.read_text(encoding="utf-8")
            for marker in required_markers.get(path, ()):
                with self.subTest(path=path.relative_to(ROOT), required=marker):
                    self.assertIn(marker, content)
            for marker in ("Template A", "Template B"):
                with self.subTest(path=path.relative_to(ROOT), stale=marker):
                    self.assertNotIn(marker, content)

    def test_scripts_syntax_and_no_personal_paths(self):
        for script in ("run_codex.sh", "run_codex_parallel.sh"):
            with self.subTest(script=script):
                self._run(["bash", "-n", str(SCRIPTS / script)])
        with self.subTest(script=GUARD.name), tempfile.TemporaryDirectory() as cache:
            self._run(
                ["python3", "-m", "py_compile", str(GUARD)],
                env={**os.environ, "PYTHONPYCACHEPREFIX": cache},
            )

        personal_path = b"/Users/" + b"mei"
        offending_paths = []

        def raise_walk_error(error):
            raise error

        for directory, subdirectories, filenames in os.walk(ROOT, onerror=raise_walk_error):
            # Generated bytecode can contain the absolute source path.
            for excluded in (".git", "__pycache__"):
                if excluded in subdirectories:
                    subdirectories.remove(excluded)
            for filename in filenames:
                if filename == ".git":
                    continue
                path = Path(directory) / filename
                if path.is_file() and personal_path in path.read_bytes():
                    offending_paths.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(offending_paths), [], "Files containing a personal path")

    def test_pre_tool_use_hook_guard_behavior(self):
        def tool_call(name, path, **args):
            return {"toolCall": {"name": name, "args": {"Path": path, **args}}}

        source = "/workspace/src/app.py"
        lines = [f"value_{index} = {index}" for index in range(25)]
        original = "\n".join(lines)
        small_edit = lines.copy()
        small_edit[5] = "value_5 = 100"
        small_edit[18] = "value_18 = 200"
        large_edit = [f"updated_{index} = {index}" for index in range(11)] + lines[11:]
        cases = [
            ("source write", tool_call("write_file", source), "deny"),
            (
                "11 changed lines",
                tool_call("edit_file", source, Target=original, Replacement="\n".join(large_edit)),
                "deny",
            ),
            (
                "2 changed lines in a 25-line block",
                tool_call("edit_file", source, Target=original, Replacement="\n".join(small_edit)),
                "allow",
            ),
            (
                "temporary code write",
                tool_call("write_file", "/tmp/codex-orchestrator/test/helper.py"),
                "allow",
            ),
            (
                "plugin configuration write",
                tool_call("write_file", "/workspace/.gemini/config/plugin.json"),
                "allow",
            ),
            ("documentation write", tool_call("write_file", "/workspace/docs/README.md"), "allow"),
            (
                "temporary research source index",
                tool_call("write_file", "/tmp/codex-orchestrator/topic/corpus/sources_index.json"),
                "allow",
            ),
            (
                "workspace research Markdown",
                tool_call("write_file", "/workspace/research/notes.md"),
                "allow",
            ),
            (
                "workspace research JSON",
                tool_call("write_file", "/workspace/research/sources_index.json"),
                "allow",
            ),
            (
                "explicit opt-out",
                {**tool_call("write_file", source), "lastUserInput": "不用codex，你直接改"},
                "allow",
            ),
        ]
        inputs = [
            (label, json.dumps(payload, ensure_ascii=False), decision)
            for label, payload, decision in cases
        ]
        inputs.append(("malformed JSON", "{bad", "allow"))
        for label, payload, decision in inputs:
            with self.subTest(case=label):
                output = self._run([sys.executable, "-B", str(GUARD)], input=payload)
                self.assertEqual(json.loads(output)["decision"], decision)


class CodexWrapperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.corpus = self.temp / "corpus"
        self.corpus.mkdir()
        self.out = self.temp / "output"
        self.stub = self.temp / "codex"
        self.stub.write_text(dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            argv = sys.argv
            last_message = Path(argv[argv.index("-o") + 1])
            last_message.with_name("codex-argv.json").write_text(
                json.dumps(argv), encoding="utf-8"
            )

            payload = {"summary": "Stub response"}
            if "--output-schema" in argv:
                schema_path = Path(argv[argv.index("--output-schema") + 1])
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                samples = {"string": "Stub response", "array": []}
                payload = {
                    key: field["enum"][0] if "enum" in field else samples[field["type"]]
                    for key, field in schema["properties"].items()
                }
            last_message.write_text(
                os.environ.get("TEST_CODEX_LAST_MESSAGE", json.dumps(payload)), encoding="utf-8"
            )
            print(json.dumps({"type": "thread.started", "thread_id": "test-thread"}))
            print(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
            }}))
            """), encoding="utf-8")
        self.stub.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env["PATH"] = os.pathsep.join((str(self.temp), os.environ.get("PATH", os.defpath)))
        self.env["GIT_CEILING_DIRECTORIES"] = str(self.temp)
        self.env.pop("TEST_CODEX_LAST_MESSAGE", None)

    def _image(self):
        image = self.corpus / "figure with spaces.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        alias = self.corpus / "image.png"
        alias.symlink_to(image.name)
        return alias

    def _read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def _run_wrapper(self, script, *args, error=None):
        command = ["bash", str(SCRIPTS / script), *map(str, args), "--out-dir", str(self.out)]
        result = subprocess.run(
            command, cwd=self.temp, env=self.env, capture_output=True, encoding="utf-8", timeout=30
        )
        diagnostics = f"{command!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        if error is None:
            self.assertEqual(result.returncode, 0, diagnostics)
        else:
            self.assertNotEqual(result.returncode, 0, diagnostics)
            self.assertIn(error, result.stderr, diagnostics)
            self.assertFalse(self.out.exists(), diagnostics)
        return result

    def _assert_exec_argv(self, out, *, target=None, schema=None, search=False, image=None):
        argv = self._read_json(out / "codex-argv.json")
        self.assertEqual(Path(argv[0]), self.stub)
        if search:
            self.assertEqual(argv[1:3], ["--search", "exec"])
        else:
            self.assertEqual(argv[1], "exec")
        self.assertEqual(argv.count("--search"), int(search))
        configs = [value for flag, value in zip(argv, argv[1:]) if flag == "-c"]
        self.assertEqual(configs.count('web_search="disabled"'), int(not search), argv)
        for flag, expected in (
            ("-s", "read-only"),
            ("-C", target or self.corpus),
            ("--output-schema", schema),
            ("-i", image),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(argv.count(flag), int(expected is not None), argv)
                if expected is not None:
                    index = argv.index(flag)
                    self.assertEqual(argv[index + 1:index + 2], [str(expected)], argv)
        self.assertEqual(argv.count("--skip-git-repo-check"), int(target is None), argv)

    def test_run_codex_read_only_structured_modes(self):
        image = self._image()
        for mode, schema_name, max_cmds in (
            ("research", "research", 10),
            ("critique", "critique", 8),
            ("analyze", "analysis", 10),
        ):
            with self.subTest(mode=mode):
                self.out = self.temp / mode
                schema = (REFERENCES / f"{schema_name}-output-schema.json").resolve()
                search = mode == "critique"
                extra_args = ["--web-search", "--image", image] if search else []
                self._run_wrapper(
                    "run_codex.sh", mode, "--dir", self.corpus, "--prompt", "Inspect the corpus.",
                    "--structured", *extra_args,
                )
                self._assert_exec_argv(
                    self.out, schema=schema, search=search, image=image.resolve() if search else None
                )
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["status"], "ok")
                self.assertEqual(summary["mode"], mode)
                self.assertEqual(summary["thread_id"], "test-thread")
                self.assertEqual(summary["schema_file"], str(schema))
                self.assertEqual(summary["max_cmds"], max_cmds)
                self.assertIs(summary["structured_valid"], True)
                self.assertIs(summary["web_search"], search)
                self.assertEqual(summary["images"], [str(image.resolve())] if search else [])

    def test_run_codex_offline_default_precedes_extra_config(self):
        self._run_wrapper(
            "run_codex.sh", "consult", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
            "--use-user-config", "-c", 'web_search="live"',
        )
        self._assert_exec_argv(self.out)
        argv = self._read_json(self.out / "codex-argv.json")
        configs = [value for flag, value in zip(argv, argv[1:]) if flag == "-c"]
        self.assertEqual(
            [value for value in configs if value.startswith("web_search=")],
            ['web_search="disabled"', 'web_search="live"'],
        )

    def test_run_codex_structured_validates_last_message(self):
        complete = {
            "verdict": "completed",
            "summary": "Stub response",
            "root_cause_or_design": [],
            "changes": [],
            "evidence": [],
            "verification": [{"command": "stub", "exit_code": 0, "key_output": "ok"}],
            "risks_and_decisions": [],
        }
        mismatch = "does not match the JSON Schema"
        for name, message, warning in (
            ("complete", json.dumps(complete), None),
            ("missing-fields", "{}", mismatch),
            ("invalid-verdict", json.dumps({**complete, "verdict": "ok"}), mismatch),
            ("nested-boolean", json.dumps({**complete, "verification": [
                {**complete["verification"][0], "exit_code": True},
            ]}), mismatch),
            ("extra-field", json.dumps({**complete, "extra": None}), mismatch),
            ("invalid-json", "{", "is not valid JSON"),
        ):
            with self.subTest(name=name):
                self.out = self.temp / name
                self.env["TEST_CODEX_LAST_MESSAGE"] = message
                result = self._run_wrapper(
                    "run_codex.sh", "consult", "--dir", self.corpus,
                    "--prompt", "Inspect the corpus.", "--structured",
                )
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["status"], "ok")
                self.assertIs(summary["structured_valid"], warning is None)
                if warning:
                    self.assertIn(warning, result.stderr)

    def test_run_codex_rejects_schema_directory(self):
        result = self._run_wrapper(
            "run_codex.sh", "consult", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
            "--schema", ".", error="schema file is missing or unreadable: .",
        )
        self.assertEqual(result.returncode, 2)

    def test_run_codex_review_image_selects_read_only_exec(self):
        image = self._image()
        repo = self.temp / "review-repo"
        subprocess.run(
            ["git", "init", "--quiet", str(repo)],
            env=self.env, capture_output=True, check=True, timeout=30,
        )
        self._run_wrapper("run_codex.sh", "review", "--dir", repo, "--image", image)
        self._assert_exec_argv(self.out, target=repo, image=image.resolve())
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["review_engine"], "read-only-exec")

    def test_run_codex_research_rejects_add_dir(self):
        self._run_wrapper(
            "run_codex.sh", "research", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
            "--add-dir", self.temp,
            error="--add-dir requires implement or resume with --sandbox workspace-write",
        )

    def _manifest(self, entries):
        spec = self.corpus / "spec.md"
        spec.write_text("Inspect the corpus.\n", encoding="utf-8")
        path = self.corpus / "manifest.json"
        tasks = [{"spec": spec.name, **entry} for entry in entries]
        path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        return path

    def _custom_schema(self):
        path = self.corpus / "custom_schema.json"
        path.write_text(json.dumps({
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        }), encoding="utf-8")
        return path

    def test_parallel_manifest_forwards_per_task_options(self):
        image = self._image()
        custom_schema = self._custom_schema()
        manifest = self._manifest([
            {
                "name": "task-a", "mode": "research", "structured": True,
                "web_search": True, "images": [image.name],
            },
            {
                "name": "task-b", "mode": "critique", "structured": False,
                "schema": custom_schema.name,
            },
            {"name": "task-c", "mode": "analyze", "structured": True},
        ])
        self._run_wrapper(
            "run_codex_parallel.sh", "--dir", self.corpus, "--manifest", manifest
        )
        summary = self._read_json(self.out / "parallel-summary.json")
        self.assertEqual(summary["status"], "ok")
        expected = [
            ("task-a", "research", REFERENCES / "research-output-schema.json", image.resolve()),
            ("task-b", "critique", custom_schema, None),
            ("task-c", "analyze", REFERENCES / "analysis-output-schema.json", None),
        ]
        self.assertEqual([task["task"] for task in summary["tasks"]], [row[0] for row in expected])
        for task, (name, mode, schema, attachment) in zip(summary["tasks"], expected):
            with self.subTest(task=name):
                self.assertEqual(task["status"], "ok")
                self.assertEqual(task["exit_code"], 0)
                self.assertEqual(task["isolation"], "shared-dir")
                self.assertEqual(task["mode"], mode)
                self.assertIs(task["structured"], name != "task-b")
                self._assert_exec_argv(
                    self.out / name, schema=schema.resolve(), search=name == "task-a", image=attachment
                )

    def test_parallel_manifest_can_disable_cli_web_search_per_task(self):
        manifest = self._manifest([
            {"name": "offline", "mode": "research", "web_search": False},
            {"name": "online", "mode": "research"},
        ])
        self._run_wrapper(
            "run_codex_parallel.sh", "--dir", self.corpus, "--manifest", manifest, "--web-search"
        )
        self._assert_exec_argv(self.out / "offline")
        self._assert_exec_argv(self.out / "online", search=True)

    def test_parallel_manifest_rejects_schema_with_effective_structured(self):
        schema = self._custom_schema()
        task = {"name": "conflict", "mode": "research", "schema": schema.name}
        for inherited in (False, True):
            with self.subTest(inherited=inherited):
                manifest = self._manifest([task if inherited else {**task, "structured": True}])
                cli_flags = ["--structured"] if inherited else []
                self._run_wrapper(
                    "run_codex_parallel.sh", "--dir", self.corpus, "--manifest", manifest,
                    *cli_flags, error="task 'conflict': --structured and --schema are mutually exclusive",
                )


if __name__ == "__main__":
    unittest.main()
