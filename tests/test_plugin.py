"""Verify the Google Antigravity plugin using only the standard library."""

import base64
import errno
import fcntl
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from textwrap import dedent
from unittest import mock


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
        self.assertEqual(plugin["version"], "3.2.0")
        self.assertEqual(plugin["logo"], "assets/logo.svg")
        self.assertTrue((ROOT / plugin["logo"]).is_file())
        self.assertIsInstance(plugin["suggestedPrompts"], list)
        self.assertLessEqual(len(plugin["suggestedPrompts"]), 3)
        self.assertTrue((ROOT / "SPEC.md").read_text(encoding="utf-8").strip())

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
        rules = RULES.read_text(encoding="utf-8")
        self.assertTrue(rules.strip(), RULES)
        self.assertLessEqual(len(rules.splitlines()), 35)

        agent = self._frontmatter(AGENT)
        self.assertEqual(agent["name"], "codex-worker")
        self.assertIs(agent["subagent"], True)
        self.assertIs(agent["mainAgent"], False)
        self.assertIs(agent["inheritMcp"], False)
        self.assertIs(agent["inheritCustomizations"], True)
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
            ROOT / "README.md": ("v3.2.0", "Communication Charter v3.1", charter.name),
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
        modules = sorted((SCRIPTS / "codex_orch").glob("*.py"))
        self.assertTrue(modules)
        with tempfile.TemporaryDirectory() as cache:
            self._run(
                [sys.executable, "-m", "py_compile", str(GUARD), *map(str, modules)],
                env={**os.environ, "PYTHONPYCACHEPREFIX": cache},
            )

        personal_path = re.compile(rb"/Users/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/|\b)")
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
                if path.is_file() and personal_path.search(path.read_bytes()):
                    offending_paths.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(offending_paths), [], "Files containing a personal path")

    def test_pre_tool_use_hook_guard_behavior(self):
        def tool_call(name, path, **args):
            return {"toolCall": {"name": name, "args": {"Path": path, **args}}}

        source = "/workspace/src/app.py"
        lines = [f"value_{index} = {index}" for index in range(40)]
        original = "\n".join(lines)

        def edit(changed, user_input=""):
            replacement = lines.copy()
            for index in range(changed):
                replacement[index] = f"updated_{index} = {index}"
            return {
                **tool_call("edit_file", source, Target=original, Replacement="\n".join(replacement)),
                "lastUserInput": user_input,
            }

        new_code = tool_call("write_file", source)
        scattered = lines.copy()
        scattered[5] = "value_5 = 100"
        scattered[18] = "value_18 = 200"
        cases = [
            ("new source file", new_code, "deny"),
            ("10 changed lines", edit(10), "allow"),
            ("11 changed lines", edit(11), "deny"),
            ("scattered edits", tool_call("edit_file", source, Target=original,
                                          Replacement="\n".join(scattered)), "allow"),
            (
                "temporary code write",
                tool_call("write_file", "/tmp/codex-orchestrator/test/helper.py"),
                "allow",
            ),
            ("outside temp", tool_call("write_file", "/tmp/outside.py"), "deny"),
            ("outside macOS temp", tool_call("write_file", "/var/folders/xx/outside.py"), "deny"),
            ("code in .gemini", tool_call("write_file", "/workspace/.gemini/evil.py"), "deny"),
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
            (None, label, json.dumps(payload, ensure_ascii=False), decision)
            for label, payload, decision in cases
        ]
        inputs.append((None, "malformed JSON", "{bad", "allow"))
        for profile, label, payload, decision in (
            ("pragmatic", "25 changed lines", edit(25), "allow"),
            ("pragmatic", "30 changed lines", edit(30), "allow"),
            ("pragmatic", "31 changed lines", edit(31), "deny"),
            ("pragmatic", "new source file", new_code, "deny"),
            ("pragmatic", "/impl override", edit(11, "/impl Fix the file"), "deny"),
            ("pragmatic", "/dag override", edit(11, "/dag Fix the file"), "deny"),
            ("weird", "11 changed lines", edit(11), "deny"),
        ):
            inputs.append((profile, label, json.dumps(payload), decision))
        for profile, label, payload, decision in inputs:
            with self.subTest(profile=profile, case=label):
                env = os.environ.copy()
                env.pop("CODEX_GUARD_PROFILE", None)
                if profile is not None:
                    env["CODEX_GUARD_PROFILE"] = profile
                output = self._run([sys.executable, "-B", str(GUARD)], input=payload, env=env)
                response = json.loads(output)
                self.assertEqual(response["decision"], decision)
                if decision == "deny":
                    self.assertRegex(response["reason"], r"run_codex\.sh implement.*codex-worker")


class CodexTestCase(unittest.TestCase):
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
            import runpy
            import sys
            import time
            from pathlib import Path

            argv = sys.argv
            last_message = Path(argv[argv.index("-o") + 1])
            last_message.with_name("codex-argv.json").write_text(
                json.dumps(argv), encoding="utf-8"
            )
            if keys := os.environ.get("TEST_CODEX_ENV_KEYS"):
                last_message.with_name("codex-env.json").write_text(
                    json.dumps({key: os.environ[key] for key in keys.split() if key in os.environ}),
                    encoding="utf-8",
                )
            if action := os.environ.get("TEST_CODEX_ACTION"):
                runpy.run_path(action, init_globals={
                    "target": Path(argv[argv.index("-C") + 1]),
                    "artifacts": last_message.parent,
                })
            for index in range(int(os.environ.get("TEST_CODEX_HEARTBEATS", "0"))):
                time.sleep(0.3)
                print(json.dumps({"type": "heartbeat", "index": index}), flush=True)

            payload = {"summary": "Stub response"}
            if "--output-schema" in argv:
                schema_path = Path(argv[argv.index("--output-schema") + 1])
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                samples = {"string": "Stub response", "array": []}
                payload = {
                    key: field["enum"][0] if "enum" in field else samples[field["type"]]
                    for key, field in schema["properties"].items()
                }
            message = os.environ.get("TEST_CODEX_LAST_MESSAGE", json.dumps(payload))
            if last_message.parent.name == os.environ.get("TEST_CODEX_BAD_TASK"):
                message = "{broken"
            last_message.write_text(message, encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "test-thread"}))
            for event in json.loads(os.environ.get("TEST_CODEX_EVENTS", "[]")):
                print(event if isinstance(event, str) else json.dumps(event))
            print(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
            }}))
            if stderr := os.environ.get("TEST_CODEX_STDERR"):
                print(stderr, file=sys.stderr)
            sys.exit(int(os.environ.get("TEST_CODEX_EXIT", "0")))
            """), encoding="utf-8")
        self.stub.chmod(0o755)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "CODEX_", "TEST_CODEX_", "PYTHON"))
            and key not in ("BASH_ENV", "ENV", "XDG_CONFIG_HOME")
        }
        home = self.temp / "home"
        home.mkdir()
        self.env.update(
            HOME=str(home),
            PATH=os.pathsep.join((str(self.temp), os.environ.get("PATH", os.defpath))),
            GIT_CEILING_DIRECTORIES=str(self.temp),
            GIT_CONFIG_NOSYSTEM="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1",
        )

    def _stub_action(self, source):
        action = self.temp / "codex_action.py"
        action.write_text(dedent(source), encoding="utf-8")
        self.env["TEST_CODEX_ACTION"] = str(action)

    def _read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def _wrapper_command(self, script, *args):
        return ["bash", str(SCRIPTS / script), *map(str, args), "--out-dir", str(self.out)]

    def _run_wrapper(self, script, *args, exit_code=0, error=None, **kwargs):
        command = self._wrapper_command(script, *args)
        result = subprocess.run(
            command, cwd=self.temp, env=self.env, capture_output=True, encoding="utf-8",
            timeout=30, **kwargs,
        )
        diagnostics = f"{command!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        self.assertEqual(result.returncode, exit_code, diagnostics)
        if error is not None:
            self.assertIn(error, result.stderr, diagnostics)
            self.assertFalse(self.out.exists(), diagnostics)
        return result

    def _manifest(self, entries):
        spec = self.corpus / "spec.md"
        spec.write_text("Inspect the corpus.\n", encoding="utf-8")
        path = self.corpus / "manifest.json"
        tasks = [{"spec": spec.name, **entry} for entry in entries]
        path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        return path


class CodexWrapperTests(CodexTestCase):

    def _image(self):
        image = self.corpus / "figure with spaces.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        alias = self.corpus / "image.png"
        alias.symlink_to(image.name)
        return alias

    def _consult(self, *options, **kwargs):
        return self._run_wrapper(
            "run_codex.sh", "consult", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
            *options, **kwargs,
        )

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

    def test_run_codex_sol_defaults_to_ultrafast_and_astra_stays_fast(self):
        for name, args, model, effort, speed in (
            ("sol-medium", ("implement",), "gpt-6.2-sol", "medium", "ultrafast"),
            ("sol-fast-preset", ("implement", "--fast"), "gpt-6.2-sol", "low", "ultrafast"),
            ("sol-high", ("implement", "--effort", "high"), "gpt-6.2-sol", "high", "default"),
            ("sol-high-override", ("implement", "--effort", "high", "--speed", "ultrafast"),
             "gpt-6.2-sol", "high", "ultrafast"),
            ("sol-fast-override", ("implement", "--speed", "fast"), "gpt-6.2-sol", "medium", "fast"),
            ("astra-medium", ("consult",), "gpt-6.2-astra", "medium", "fast"),
            ("astra-downgrade", ("consult", "--speed", "ultrafast"), "gpt-6.2-astra", "medium", "fast"),
        ):
            with self.subTest(name=name):
                self.out = self.temp / name
                result = self._run_wrapper(
                    "run_codex.sh", *args, "--dir", self.corpus, "--prompt", "Inspect the corpus.",
                )
                argv = self._read_json(self.out / "codex-argv.json")
                tiers = [value for flag, value in zip(argv, argv[1:])
                         if flag == "-c" and value.startswith("service_tier=")]
                self.assertEqual(tiers, [] if speed == "default" else [f'service_tier="{speed}"'])

                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["model"], model)
                self.assertEqual(summary["effort"], effort)
                self.assertEqual(summary["speed"], speed)
                self.assertIs(summary["fast"], speed in ("fast", "ultrafast"))
                if name == "astra-downgrade":
                    self.assertIn("gpt-6.2-astra does not support ultrafast", result.stderr)

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
            ("invalid-json", "{", "invalid JSON last message"),
        ):
            with self.subTest(name=name):
                self.out = self.temp / name
                self.env["TEST_CODEX_LAST_MESSAGE"] = message
                exit_code = 3 if warning else 0
                result = self._consult("--structured", exit_code=exit_code)
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["status"], "schema_error" if warning else "ok")
                self.assertEqual(summary["exit_code"], exit_code)
                self.assertEqual(summary["codex_exit_code"], 0)
                self.assertIs(summary["structured_valid"], warning is None)
                if warning:
                    self.assertIn(warning, result.stderr)

    def test_run_codex_rejects_schema_directory(self):
        self._consult("--schema", ".", exit_code=2, error="schema file is missing or unreadable: .")

    def test_run_codex_rejects_unsupported_schema_keywords_before_artifacts(self):
        path = self.corpus / "unsupported.json"
        for keyword, value in (("oneOf", [{"type": "object"}]), ("minItems", 1), ("$ref", "#")):
            with self.subTest(keyword=keyword):
                path.write_text(json.dumps({"type": "object", keyword: value}), encoding="utf-8")
                self._consult("--schema", path, exit_code=2, error=f"unsupported JSON Schema keyword {keyword!r}")

    def test_parallel_rejects_unsupported_schema_before_artifacts(self):
        schema = self.corpus / "unsupported.json"
        schema.write_text('{"oneOf": [{"type": "object"}]}', encoding="utf-8")
        for extra, flags in (({}, ("--schema", schema)), ({"schema": schema.name}, ())):
            with self.subTest(source="manifest" if extra else "cli"):
                manifest = self._manifest([{"name": "task", **extra}])
                self._run_wrapper(
                    "run_codex_parallel.sh", "--dir", self.corpus, "--manifest", manifest,
                    *flags, exit_code=2, error="unsupported JSON Schema keyword 'oneOf'",
                )

    def test_run_codex_rejects_invalid_arguments_before_artifacts(self):
        self._consult("--unknown-option", exit_code=2, error="unknown option")

    def test_run_codex_rejects_unreadable_explicit_config_and_rules(self):
        script = "run_codex.sh"
        options = {}
        if os.geteuid() == 0:
            # Root can read mode-000 files. Run only these preflight checks unprivileged.
            script = self.temp / script
            shutil.copy2(SCRIPTS / script.name, script)
            self.temp.chmod(0o755)
            options["user"] = pwd.getpwnam("nobody").pw_uid
        user_config = Path(self.env["HOME"]) / ".codex"
        user_config.mkdir()
        for flag, name in (("--use-user-config", "config.toml"), ("--use-rules", "rules")):
            with self.subTest(flag=flag):
                unreadable = user_config / name
                if name == "rules":
                    unreadable.mkdir()
                else:
                    unreadable.touch()
                unreadable.chmod(0)
                self.addCleanup(unreadable.chmod, 0o700)
                self._run_wrapper(
                    script, "consult", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
                    flag, exit_code=2, error=f"{flag} requires read access", **options,
                )

    def test_run_codex_isolates_child_environment_and_artifacts(self):
        forbidden = dict.fromkeys((
            "SSH_AUTH_SOCK", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTIGRAVITY_CSRF_TOKEN",
        ), "fixture-secret")
        preserved = {
            "OPENAI_API_KEY": "fixture-openai-key",
            "CODEX_TEST_FLAG": "codex-flag",
            "TEST_CODEX_PROBE": "test-flag",
            "PATH": self.env["PATH"],
            "HOME": self.env["HOME"],
        }
        self.env.update(forbidden, **preserved, TEST_CODEX_ENV_KEYS=" ".join(forbidden | preserved))
        self.out.mkdir(mode=0o755)
        self._consult()
        self.assertEqual(self._read_json(self.out / "codex-env.json"), preserved)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)

    def test_runners_import_only_trusted_plugin_modules(self):
        marker = self.temp / "untrusted-import"
        payload = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('shadow import')\n"
        shadow = self.temp / "codex_orch"
        shadow.mkdir()
        (shadow / "__init__.py").write_text(payload, encoding="utf-8")
        pythonpath = self.temp / "pythonpath"
        pythonpath.mkdir()
        (pythonpath / "uuid.py").write_text(payload, encoding="utf-8")
        self.env["PYTHONPATH"] = str(pythonpath)

        self._consult("--structured")
        self.assertIs(self._read_json(self.out / "summary.json")["structured_valid"], True)
        self.assertFalse(marker.exists())

        self.out = self.temp / "parallel"
        self._run_wrapper(
            "run_codex_parallel.sh", "--dir", self.corpus, "--structured",
            "--manifest", self._manifest([{"name": "isolated"}]),
        )
        self.assertIs(self._read_json(self.out / "isolated" / "summary.json")["structured_valid"], True)
        self.assertFalse(marker.exists())

    def test_ensure_private_dir_rejects_unsafe_directories(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch.env import ensure_private_dir

        directory = self.temp / "private"
        directory.mkdir(mode=0o700)
        directory.chmod(0o750)
        alias = self.temp / "symlink"
        for target in (directory, self.temp / "missing"):
            with self.subTest(symlink_target=target.name):
                alias.symlink_to(target, target_is_directory=True)
                with self.assertRaises(PermissionError):
                    ensure_private_dir(alias)
                alias.unlink()
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)

        with mock.patch.object(os, "geteuid", return_value=directory.stat().st_uid + 1):
            with self.assertRaises(PermissionError):
                ensure_private_dir(directory)
            with mock.patch.object(Path, "chmod", side_effect=PermissionError(errno.EPERM, "fixture")):
                with self.assertRaises(PermissionError):
                    ensure_private_dir(directory)

        with mock.patch.object(Path, "chmod", side_effect=OSError(errno.ENOTSUP, "fixture")):
            self.assertEqual(ensure_private_dir(directory), directory)
        directory.chmod(0o750)
        with mock.patch.object(Path, "chmod", side_effect=OSError(errno.ENOTSUP, "fixture")):
            with self.assertRaises(PermissionError):
                ensure_private_dir(directory)

    def test_run_codex_redacts_failure_telemetry_and_preserves_child_code(self):
        ghp = "ghp_fixtureToken1234"
        bearer = "Bearer fixtureBearer1234"
        sk = "sk-fixtureOpenAIToken1234"
        self.env.update(
            TEST_CODEX_EXIT="17",
            TEST_CODEX_STDERR=f"stderr fixture {sk}",
            TEST_CODEX_EVENTS=json.dumps([
                {"type": "item.completed", "item": {
                    "type": "command_execution", "command": f"git command {ghp}",
                    "aggregated_output": f"failure with {bearer} and {sk}", "exit_code": 7,
                }},
                {"type": "error", "message": f"event fixture {ghp} {bearer} {sk}"},
                {"type": "turn.failed", "error": {"message": f"turn fixture {sk}"}},
            ]),
        )
        result = self._consult(exit_code=17)
        summary_path = self.out / "summary.json"
        summary = self._read_json(summary_path)
        self.assertEqual((summary["status"], summary["exit_code"], summary["codex_exit_code"]), ("error", 17, 17))
        self.assertEqual(summary["partial_progress"]["failed_commands"], [{
            "command": "git command [REDACTED]",
            "output_tail": "failure with [REDACTED] and [REDACTED]",
            "exit_code": 7,
        }])
        self.assertIn("--- event_errors ---\nerror: event fixture [REDACTED] [REDACTED] [REDACTED]", result.stdout)
        self.assertIn("turn.failed: turn fixture [REDACTED]", result.stdout)
        self.assertIn("stderr fixture [REDACTED]", result.stdout)
        for secret in (ghp, bearer, sk):
            self.assertNotIn(secret, result.stdout + result.stderr + summary_path.read_text(encoding="utf-8"))

    def test_run_codex_wall_timeout_kills_child_and_grandchild(self):
        self._stub_action('''
            import signal
            import subprocess
            import sys
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            code = """
            import fcntl
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            with open(sys.argv[1], 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                print('grandchild ready', flush=True)
                time.sleep(15)
            """
            child = subprocess.Popen(
                [sys.executable, '-c', code, str(artifacts / 'grandchild.lock')],
                stdout=subprocess.PIPE, text=True,
            )
            assert child.stdout.readline().strip() == 'grandchild ready'
            (artifacts / 'grandchild-ready').touch()
            time.sleep(15)
        ''')
        self._consult("--timeout", 1, exit_code=124)
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["status"], "timeout")
        self.assertEqual(summary["exit_code"], 124)
        self.assertEqual(summary["codex_exit_code"], -signal.SIGKILL)
        self.assertIs(summary["timed_out"], True)
        self.assertIs(summary["stall_timed_out"], False)
        self.assertTrue((self.out / "grandchild-ready").is_file())
        # A reparented zombie may retain its PID, but only a live process can hold this lock.
        with (self.out / "grandchild.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _stub_with_background_grandchild(self, *, wait_for_signal):
        self.env["TEST_CODEX_WAIT_FOR_SIGNAL"] = str(int(wait_for_signal))
        self._stub_action('''
            import json
            import os
            import signal
            import subprocess
            import sys
            import time

            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
            code = """
            import fcntl
            import sys
            import time
            from pathlib import Path

            artifacts = Path(sys.argv[1])
            with (artifacts / 'grandchild.lock').open('wb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                (artifacts / 'grandchild-ready').touch()
                time.sleep(15)
            """
            child = subprocess.Popen(
                [sys.executable, '-c', code, str(artifacts)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            while not (artifacts / 'grandchild-ready').exists():
                assert child.poll() is None
                time.sleep(0.01)
            (artifacts / 'processes.json').write_text(json.dumps([os.getppid(), os.getpid()]))
            (artifacts / 'child-ready').touch()
            if os.environ['TEST_CODEX_WAIT_FOR_SIGNAL'] == '1':
                time.sleep(15)
        ''')

    def _assert_grandchild_stopped(self):
        with (self.out / "grandchild.lock").open("rb") as lock:
            # A reparented zombie can retain a PID, but it cannot hold the lock.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_run_codex_forwards_signals_and_reports_cancellation_after_clean_exit(self):
        self._stub_with_background_grandchild(wait_for_signal=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                self.out = self.temp / signum.name
                command = self._wrapper_command(
                    "run_codex.sh", "consult", "--dir", self.corpus, "--prompt", "Inspect the corpus.",
                )
                ready = self.out / "child-ready"
                with subprocess.Popen(
                    command, cwd=self.temp, env=self.env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ) as wrapper:
                    try:
                        deadline = time.monotonic() + 5
                        while not ready.exists() and wrapper.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(ready.exists(), "Codex fixture never signaled readiness")
                        supervisor_pid, codex_pid = self._read_json(self.out / "processes.json")
                        wrapper.send_signal(signum)
                        self.assertEqual(wrapper.wait(timeout=8), 128 + signum)
                        for pid in (supervisor_pid, codex_pid):
                            with self.assertRaises(ProcessLookupError):
                                os.kill(pid, 0)
                        self._assert_grandchild_stopped()
                        summary = self._read_json(self.out / "summary.json")
                        self.assertEqual(
                            (summary["status"], summary["exit_code"]), ("cancelled", 128 + signum),
                        )
                        for evidence in (summary, self._read_json(self.out / "supervisor_meta.json")):
                            self.assertEqual(evidence["codex_exit_code"], 0)
                            self.assertIs(evidence["cancelled"], True)
                            self.assertEqual(evidence["signal"], signum)
                    except Exception:
                        if wrapper.poll() is None:
                            wrapper.kill()
                        if ready.exists():
                            supervisor_pid, codex_pid = self._read_json(self.out / "processes.json")
                            with suppress(ProcessLookupError):
                                os.killpg(codex_pid, signal.SIGKILL)
                            with suppress(ProcessLookupError):
                                os.kill(supervisor_pid, signal.SIGTERM)
                        raise

    def test_supervisor_cli_returns_signal_exit_after_clean_child_exit(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import supervisor

        metadata = {
            "cancelled": True, "signal": signal.SIGTERM, "codex_exit_code": 0,
            "timed_out": False, "stall_timed_out": False,
        }
        meta_path = self.temp / "meta.json"
        argv = [
            "supervisor", "--events", "unused", "--stderr", "unused",
            "--meta-out", str(meta_path), "--", "unused",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(supervisor, "run_supervised", return_value=metadata):
            self.assertEqual(supervisor._main(), 128 + signal.SIGTERM)
        self.assertEqual(self._read_json(meta_path), metadata)

    def test_supervisor_stops_background_grandchild_after_clean_child_exit(self):
        self._stub_with_background_grandchild(wait_for_signal=False)
        self._consult()
        self._assert_grandchild_stopped()
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual((summary["status"], summary["codex_exit_code"]), ("ok", 0))
        self.assertIs(summary["cancelled"], False)
        self.assertIsNone(summary["signal"])

    def test_run_codex_stall_timeout_stops_silent_child(self):
        self._stub_action("import time\ntime.sleep(10)\n")
        self._consult("--stall-timeout", 1, exit_code=125)
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["status"], "stall_timeout")
        self.assertEqual(summary["exit_code"], 125)
        self.assertEqual(summary["codex_exit_code"], -signal.SIGTERM)
        self.assertIs(summary["stall_timed_out"], True)

    def test_run_codex_streaming_activity_resets_stall_timeout(self):
        self.env["TEST_CODEX_HEARTBEATS"] = "5"
        self._consult("--stall-timeout", 1)
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["status"], "ok")
        self.assertIs(summary["stall_timed_out"], False)
        self.assertGreaterEqual(summary["duration_s"], 1.5)
        events = (self.out / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([event["index"] for event in map(json.loads, events) if event["type"] == "heartbeat"], list(range(5)))

    def test_run_codex_classifies_quota_errors_from_either_stream(self):
        self._stub_action('''
            with (artifacts / 'attempts').open('a', encoding='utf-8') as log:
                print('attempt', file=log)
        ''')
        for source, events, stderr, child_exit, wrapper_exit in (
            ("stderr", [], "rate_limit_exceeded", 19, 19),
            ("event", [{"type": "error", "message": "usage_limit_reached"}], "", 20, 20),
            ("zero-exit", [{"type": "error", "message": "usage_limit_reached"}], "", 0, 75),
        ):
            with self.subTest(source=source):
                self.out = self.temp / source
                self.env.update(TEST_CODEX_EXIT=str(child_exit), TEST_CODEX_STDERR=stderr,
                                TEST_CODEX_EVENTS=json.dumps(events))
                self._run_wrapper(
                    "run_codex.sh", "implement", "--dir", self.corpus, "--prompt", "Update the corpus.",
                    exit_code=wrapper_exit,
                )
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual((self.out / "attempts").read_text(encoding="utf-8"), "attempt\n")
                self.assertEqual(summary["status"], "paused_quota")
                self.assertIs(summary["paused_quota"], True)
                self.assertEqual(summary["codex_exit_code"], child_exit)
                self.assertEqual(summary["exit_code"], wrapper_exit)
                self.assertIs(summary["timed_out"], False)

    def test_run_codex_rejects_in_place_self_hosting_before_artifacts(self):
        for target, flags in (
            (ROOT, ()), (SCRIPTS / "codex_orch", ()), (self.corpus, ("--add-dir", ROOT)),
        ):
            with self.subTest(target=target.name):
                self._run_wrapper(
                    "run_codex.sh", "implement", "--dir", target, "--prompt", "Inspect the target.",
                    *flags, exit_code=2, error="overlaps the running orchestrator scripts",
                )

    def test_run_codex_allows_self_hosting_with_worktree_flag(self):
        self._run_wrapper(
            "run_codex.sh", "implement", "--dir", ROOT, "--prompt", "Inspect the target.", "--worktree",
        )
        self.assertIn("--worktree", self._read_json(self.out / "codex-argv.json"))
        self.assertIs(self._read_json(self.out / "summary.json")["worktree"], True)

    def test_run_codex_stdout_byte_threshold_and_modes(self):
        multibyte_key = "字段" * 720
        oversized_message = json.dumps({multibyte_key: "报告", "challenges": [{"severity": ["custom"]}]},
                                       ensure_ascii=False)
        schema = self.corpus / "preview-schema.json"
        schema.write_text(json.dumps({
            "type": "object", "required": [multibyte_key, "challenges"], "additionalProperties": False,
            "properties": {multibyte_key: {"type": "string"}, "challenges": {"type": "array"}},
        }), encoding="utf-8")
        self.assertLess(len(oversized_message), 4096)
        self.assertGreater(len(oversized_message.encode("utf-8")), 4096)
        for name, message, flags, truncated in (
            ("short", "Short raw response", (), False),
            ("at-limit", "a" * 4096, (), False),
            ("over-limit", "b" * 4097, (), True),
            ("custom-schema", oversized_message, ("--schema", schema), True),
            ("full", oversized_message, ("--schema", schema, "--stdout-mode", "full"), False),
            ("compact", "Short raw response", ("--stdout-mode", "compact"), True),
        ):
            with self.subTest(mode=name):
                self.out = self.temp / name
                self.env["TEST_CODEX_LAST_MESSAGE"] = message
                result = self._consult(*flags)
                summary_path = self.out / "summary.json"
                full_path = self.out / "last_message.md"
                summary = self._read_json(summary_path)
                self.assertIs(summary["stdout_truncated"], truncated)
                self.assertIs(summary["structured_valid"], True if "--schema" in flags else None)
                self.assertEqual(summary["last_message_bytes"], len(message.encode("utf-8")))
                self.assertEqual(full_path.read_text(encoding="utf-8"), message)
                header, separator, preview = result.stdout.partition("--- last_message ---\n")
                self.assertTrue(separator, header)
                if truncated:
                    self.assertLessEqual(len(preview.removesuffix("\n").encode("utf-8")), 2048)
                    self.assertIn(str(full_path), preview)
                    self.assertIn(str(summary_path), preview)
                    self.assertNotIn("\ufffd", preview)
                    if name == "custom-schema":
                        self.assertIn('keys=["字段', preview)
                else:
                    self.assertEqual(preview, message + "\n")

    def test_run_codex_command_budget_and_malformed_event_telemetry(self):
        commands = [
            {"type": "item.completed", "item": {
                "type": "command_execution", "id": str(index), "exit_code": 0,
            }} for index in range(3)
        ]
        for changed in ([], ["a.py", "b.py"]):
            with self.subTest(modified_files=changed):
                self.out = self.temp / ("changed" if changed else "unchanged")
                events = [*commands, "{bad json", "[]"]
                if changed:
                    events.append({"type": "item.completed", "item": {
                        "type": "file_change", "changes": [{"path": path} for path in [*changed, changed[0]]],
                    }})
                self.env["TEST_CODEX_EVENTS"] = json.dumps(events)
                self._consult("--max-cmds", 2)
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["commands"], 3)
                self.assertEqual(summary["modified_files"], changed)
                self.assertIs(summary["budget_exceeded"], True)
                self.assertEqual(summary["budget_utilization"], 1.5)
                self.assertEqual(summary["malformed_event_lines"], 2)
                if changed:
                    self.assertIsInstance(summary["cmd_per_modified_file"], float)
                    self.assertEqual(summary["cmd_per_modified_file"], 1.5)
                else:
                    self.assertIsNone(summary["cmd_per_modified_file"])

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
            "--add-dir", self.temp, exit_code=2,
            error="--add-dir requires implement or resume with --sandbox workspace-write",
        )

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
                    *cli_flags, exit_code=2,
                    error="task 'conflict': --structured and --schema are mutually exclusive",
                )


class CodexParallelGitTests(CodexTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        self._git(self.repo, "init", "--quiet")
        self._git(self.repo, "config", "user.name", "Codex Fixture")
        self._git(self.repo, "config", "user.email", "codex@example.invalid")
        self._git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        for name in ("A.txt", "B.txt", "C.txt"):
            (self.repo / name).write_text(f"base {name}\n", encoding="utf-8")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "--quiet", "-m", "Fixture base")
        self.base_oid = self._git(self.repo, "rev-parse", "HEAD").decode().strip()

    def _git(self, repo, *args):
        command = ["git", "-C", str(repo), *map(str, args)]
        result = subprocess.run(command, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, f"{command!r}\n{result.stderr.decode(errors='replace')}")
        return result.stdout

    def _install_git_shim(self, source):
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        self.env["TEST_CODEX_REAL_GIT"] = real_git
        shim = self.temp / "git"
        shim.write_text(dedent('''\
            #!/usr/bin/env python3
            import os
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            real_git = os.environ['TEST_CODEX_REAL_GIT']
            ''') + dedent(source) + "\nos.execv(real_git, [real_git, *args])\n", encoding="utf-8")
        shim.chmod(0o755)

    def _run_batch(self, entries, *options, script="run_codex_parallel.sh", **kwargs):
        self._run_wrapper(
            script, "--dir", self.repo, "--manifest", self._manifest(entries), *options, **kwargs,
        )
        if self.out.is_dir():
            summary = self._read_json(self.out / "parallel-summary.json")
            self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
            return summary

    def _assert_custody(self, task, changed_files, base_oid=None):
        self.assertEqual(task["isolation"], "managed-worktree")
        self.assertEqual(task["base_oid"], base_oid or self.base_oid)
        self.assertIs(task["custody_verified"], True)
        self.assertTrue(task["final_tree_oid"])
        self.assertEqual(task["reconstructed_tree_oid"], task["final_tree_oid"])
        self.assertCountEqual(task["changed_files"], changed_files)
        patch = Path(task["patch_file"])
        self.assertEqual(patch, self.out / task["task"] / "changes.patch")
        self.assertGreater(task["patch_bytes"], 0)
        self.assertEqual(task["patch_bytes"], patch.stat().st_size)
        return patch

    def test_patch_custody_combines_commit_unstaged_edit_delete_and_untracked_file(self):
        self._stub_action(r'''
            import subprocess

            (target / 'A.txt').write_text('committed A\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'A.txt'], cwd=target, check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'Worker commit'], cwd=target, check=True)
            (target / 'B.txt').write_text('unstaged B\n', encoding='utf-8')
            (target / 'C.txt').unlink()
            (target / 'D.bin').write_bytes(bytes([0, 1, 255]))
            (target / '__pycache__').mkdir()
            (target / '__pycache__' / 'noise.pyc').write_bytes(b'ignored')
        ''')
        summary = self._run_batch([{"name": "changes", "mode": "implement"}])
        self.assertEqual(summary["status"], "ok")
        self.assertIs(summary["merge_ok"], True)
        task, = summary["tasks"]
        self.assertEqual(task["status"], "ok")
        patch = self._assert_custody(task, ["A.txt", "B.txt", "C.txt", "D.bin"])
        self.assertIsNone(task["worktree_dir"])
        self.assertEqual(list((self.out / "_worktrees").iterdir()), [])
        self.assertNotIn(b"__pycache__/noise.pyc", patch.read_bytes())
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), b"")

        clean = self.temp / "replay"
        self._git(self.repo, "clone", "--quiet", "--no-local", self.repo, clean)
        self.assertEqual(self._git(clean, "rev-parse", "HEAD").decode().strip(), self.base_oid)
        self._git(clean, "apply", patch)
        self.assertEqual((clean / "A.txt").read_text(encoding="utf-8"), "committed A\n")
        self.assertEqual((clean / "B.txt").read_text(encoding="utf-8"), "unstaged B\n")
        self.assertFalse((clean / "C.txt").exists())
        self.assertEqual((clean / "D.bin").read_bytes(), bytes([0, 1, 255]))
        self._git(clean, "add", "-A")
        self.assertEqual(self._git(clean, "write-tree").decode().strip(), task["final_tree_oid"])

    def test_patch_custody_preserves_force_staged_ignored_file(self):
        self._stub_action(r'''
            import subprocess

            ignored = target / '__pycache__'
            ignored.mkdir()
            forced = ignored / 'forced.txt'
            forced.write_text('staged ignored file\n', encoding='utf-8')
            subprocess.run(['git', 'add', '-f', '--', '__pycache__/forced.txt'], cwd=target, check=True)
            forced.write_text('final ignored file\n', encoding='utf-8')
            (ignored / 'untracked.txt').touch()
        ''')
        summary = self._run_batch([{"name": "forced", "mode": "implement"}], "--keep-worktrees")
        self.assertEqual(summary["status"], "ok")
        task, = summary["tasks"]
        filename = "__pycache__/forced.txt"
        patch = self._assert_custody(task, [filename]).read_bytes()
        self.assertIn(f"+++ b/{filename}\n".encode(), patch)
        self.assertIn(b"+final ignored file\n", patch)
        worktree = Path(task["worktree_dir"])
        self.assertEqual(self._git(worktree, "show", f":{filename}"), b"staged ignored file\n")

    def test_patch_custody_rejects_unmerged_index_and_preserves_worktree(self):
        self._stub_action(r'''
            import subprocess

            mode, oid, _ = subprocess.check_output(
                ['git', 'ls-files', '-s', 'A.txt'], cwd=target,
            ).split(maxsplit=2)
            entries = b'0 ' + b'0' * len(oid) + b'\tA.txt\n'
            entries += mode + b' ' + oid + b' 1\tA.txt\n'
            entries += mode + b' ' + oid + b' 2\tA.txt\n'
            subprocess.run(['git', 'update-index', '--index-info'], cwd=target, input=entries, check=True)
            unmerged = subprocess.check_output(['git', 'ls-files', '-u'], cwd=target)
            assert unmerged
            (artifacts / 'unmerged-index').write_bytes(unmerged)
        ''')
        summary = self._run_batch([{"name": "conflict", "mode": "implement"}], exit_code=1)
        task, = summary["tasks"]
        self.assertEqual(task["status"], "error")
        self.assertIn("worker index contains unmerged entries", task["error"])
        self.assertIs(task["custody_verified"], False)
        self.assertIsNone(task["patch_file"])
        task_out = self.out / "conflict"
        self.assertFalse((task_out / "changes.patch").exists())
        worktree = Path(task["worktree_dir"])
        self.assertIn(os.fsencode(worktree), self._git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self._git(worktree, "ls-files", "-u"), (task_out / "unmerged-index").read_bytes())

    def test_patch_custody_exports_clean_after_commit_and_keeps_requested_worktree(self):
        self._stub_action(r'''
            import subprocess

            (target / 'A.txt').write_text('committed A\n', encoding='utf-8')
            subprocess.run(['git', 'commit', '--quiet', '-am', 'Worker commit'], cwd=target, check=True)
            assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=target)
        ''')
        summary = self._run_batch([{"name": "committed", "mode": "implement"}], "--keep-worktrees")
        self.assertIs(summary["merge_ok"], True)
        task, = summary["tasks"]
        patch = self._assert_custody(task, ["A.txt"])
        self.assertIn(b"+committed A", patch.read_bytes())
        worktree = Path(task["worktree_dir"])
        self.assertEqual(self._git(worktree, "status", "--porcelain"), b"")
        self.assertEqual(self._git(worktree, "rev-parse", "HEAD^{tree}").decode().strip(), task["final_tree_oid"])
        self.assertIn(os.fsencode(worktree), self._git(self.repo, "worktree", "list", "--porcelain"))

    def test_failed_worker_preserves_worktree_and_its_index(self):
        self._stub_action(r'''
            import subprocess

            (target / 'A.txt').write_text('staged\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'A.txt'], cwd=target, check=True)
            (target / 'B.txt').write_text('unstaged\n', encoding='utf-8')
        ''')
        self.env["TEST_CODEX_EXIT"] = "17"
        summary = self._run_batch([{"name": "failed", "mode": "implement"}], exit_code=1)
        self.assertEqual(summary["status"], "error")
        task, = summary["tasks"]
        self.assertEqual(task["exit_code"], 17)
        self._assert_custody(task, ["A.txt", "B.txt"])
        worktree = Path(task["worktree_dir"])
        self.assertEqual(self._git(worktree, "diff", "--cached", "--name-only"), b"A.txt\n")
        self.assertEqual(self._git(worktree, "diff", "--name-only"), b"B.txt\n")
        self.assertIn(os.fsencode(worktree), self._git(self.repo, "worktree", "list", "--porcelain"))

    def test_registered_worktree_survives_custody_or_git_cleanup_failure(self):
        self._stub_action(r"(target / 'A.txt').write_text('worker change\n', encoding='utf-8')")
        self._install_git_shim('''
            case = os.environ['TEST_CODEX_GIT_FAILURE']
            if args[:1] == ['-C']:
                if case == 'custody' and args[2:4] == ['apply', '--cached']:
                    sys.exit('fixture: private-index replay rejected')
                if case == 'cleanup' and args[2:5] == ['worktree', 'remove', '--force']:
                    if Path(args[5]).name == 'worker':
                        sys.exit('fixture: Git worktree removal rejected')
        ''')
        for case, reason in (("custody", "private-index replay rejected"),
                             ("cleanup", "Git worktree removal rejected")):
            with self.subTest(case=case):
                self.out = self.temp / case
                self.env["TEST_CODEX_GIT_FAILURE"] = case
                summary = self._run_batch([{"name": "worker", "mode": "implement"}], exit_code=1)
                task, = summary["tasks"]
                self.assertEqual(task["status"], "error")
                self.assertIn(reason, task["error"])
                self.assertIs(task["custody_verified"], case == "cleanup")
                worktree = Path(task["worktree_dir"])
                self.assertEqual((worktree / "A.txt").read_text(encoding="utf-8"), "worker change\n")
                self.assertIn(os.fsencode(worktree), self._git(self.repo, "worktree", "list", "--porcelain"))

    def test_mixed_raw_and_structured_manifest_accepts_both(self):
        summary = self._run_batch([
            {"name": "raw", "structured": False}, {"name": "structured", "structured": True},
        ])
        self.assertEqual(summary["status"], "ok")
        raw, structured = summary["tasks"]
        self.assertEqual([raw["task"], structured["task"]], ["raw", "structured"])
        ids = set()
        for task, validity in ((raw, None), (structured, True)):
            with self.subTest(task=task["task"]):
                self.assertEqual(task["status"], "ok")
                self.assertEqual(task["exit_code"], 0)
                self.assertIs(task["structured_valid"], validity)
                self.assertTrue(task["invocation_id"])
                self.assertEqual(task["invocation_id"], self._read_json(
                    self.out / task["task"] / "summary.json"
                )["invocation_id"])
                ids.add(task["invocation_id"])
        self.assertEqual(len(ids), 2)

    def test_mixed_manifest_fails_closed_for_invalid_structured_output(self):
        self.env["TEST_CODEX_BAD_TASK"] = "structured"
        summary = self._run_batch([
            {"name": "raw", "structured": False}, {"name": "structured", "structured": True},
        ], exit_code=1)
        self.assertEqual(summary["status"], "error")
        raw, structured = summary["tasks"]
        self.assertEqual(raw["status"], "ok")
        self.assertIsNone(raw["structured_valid"])
        self.assertEqual(structured["status"], "error")
        self.assertEqual(structured["exit_code"], 3)
        self.assertIs(structured["structured_valid"], False)
        self.assertIn("schema_error", structured["error"])

    def test_parallel_fails_closed_for_missing_stale_or_mismatched_worker_evidence(self):
        # Exercise the real parent with a controlled worker-wrapper boundary. A normal CLI
        # stub cannot omit or replace the summary written by the real single-run wrapper.
        scripts = self.temp / "scripts"
        scripts.mkdir()
        parallel = shutil.copy2(SCRIPTS / "run_codex_parallel.sh", scripts)
        (scripts / "codex_orch").symlink_to(SCRIPTS / "codex_orch", target_is_directory=True)
        worker = scripts / "run_codex.sh"
        worker.write_text(dedent('''\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            case = os.environ['TEST_CODEX_EVIDENCE']
            if case in ('wrong-id', 'invalid-structured'):
                args = sys.argv
                output = Path(args[args.index('--out-dir') + 1])
                invocation = args[args.index('--invocation-id') + 1]
                (output / 'summary.json').write_text(json.dumps({
                    'status': 'ok', 'exit_code': 0, 'structured_valid': case != 'invalid-structured',
                    'invocation_id': 'other-invocation' if case == 'wrong-id' else invocation,
                }), encoding='utf-8')
            '''), encoding="utf-8")
        worker.chmod(0o755)
        for case, expected in (
            ("missing", "summary.json is missing"), ("stale", "summary.json was not refreshed"),
            ("wrong-id", "invocation_id does not match"),
            ("invalid-structured", "must report structured_valid=true"),
        ):
            with self.subTest(case=case):
                self.out = self.temp / case
                self.env["TEST_CODEX_EVIDENCE"] = case
                if case == "stale":
                    (self.out / "worker").mkdir(parents=True)
                    (self.out / "worker" / "summary.json").write_text(
                        '{"status":"ok","exit_code":0,"structured_valid":true}', encoding="utf-8",
                    )
                summary = self._run_batch([{"name": "worker", "structured": True}], script=parallel, exit_code=1)
                self.assertEqual(summary["status"], "error")
                task, = summary["tasks"]
                self.assertEqual((task["status"], task["exit_code"]), ("error", 1))
                self.assertIn(expected, task["error"])

    def test_dirty_policy_distinguishes_base_reconstruction_from_target_apply(self):
        (self.repo / "A.txt").write_text("conflicting local A\n", encoding="utf-8")
        entries = [{"name": "dirty", "mode": "implement"}]
        self._run_batch(entries, exit_code=2, error="Git checkout has uncommitted changes")
        self._stub_action(r'''
            import os
            import subprocess

            assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=target)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=target).decode().strip()
            assert head == os.environ['TEST_CODEX_BASE']
            assert (target / 'A.txt').read_text(encoding='utf-8') == 'base A.txt\n'
            (target / 'A.txt').write_text('worker A\n', encoding='utf-8')
        ''')
        self.env["TEST_CODEX_BASE"] = self.base_oid
        summary = self._run_batch(entries, "--dirty-policy", "head", exit_code=1)
        self.assertEqual(summary["status"], "error")
        self.assertIs(summary["merge_ok"], False)
        task, = summary["tasks"]
        self.assertEqual(task["status"], "ok")
        self.assertIs(task["base_dirty_warning"], True)
        self._assert_custody(task, ["A.txt"])
        check, = summary["merge_check"]
        self.assertEqual(check["base_oid"], self.base_oid)
        self.assertIs(check["base_reconstruct_ok"], True)
        self.assertIs(check["target_apply_ok"], False)
        self.assertEqual((self.repo / "A.txt").read_text(encoding="utf-8"), "conflicting local A\n")

    def test_linked_checkouts_use_their_own_base_oids(self):
        linked = self.temp / "linked"
        self._git(self.repo, "worktree", "add", "--quiet", "--detach", linked, self.base_oid)
        (self.repo / "A.txt").write_text("advanced A\n", encoding="utf-8")
        self._git(self.repo, "commit", "--quiet", "-am", "Advance main checkout")
        new_base = self._git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.assertNotEqual(new_base, self.base_oid)
        self._stub_action('''
            source = (target / 'A.txt').read_text(encoding='utf-8')
            (target / 'B.txt').write_text(source, encoding='utf-8')
        ''')
        summary = self._run_batch([
            {"name": "main", "mode": "implement"},
            {"name": "linked", "mode": "implement", "dir": str(linked)},
        ])
        self.assertIs(summary["merge_ok"], True)
        main, other = summary["tasks"]
        self.assertEqual(main["git_common_dir"], other["git_common_dir"])
        for task, repo, oid, patch_line in (
            (main, self.repo, new_base, b"+advanced A"), (other, linked, self.base_oid, b"+base A.txt"),
        ):
            with self.subTest(checkout=task["task"]):
                self.assertEqual(task["git_root"], str(repo))
                self.assertIn(patch_line, self._assert_custody(task, ["B.txt"], oid).read_bytes())
        self.assertEqual({check["base_oid"] for check in summary["merge_check"]}, {new_base, self.base_oid})

    def test_concurrent_merge_checks_share_worktrees_without_colliding(self):
        shared = self.temp / "_worktrees"
        shared.mkdir()
        barrier = self.temp / "merge-barrier"
        barrier.mkdir()
        self.env["TEST_CODEX_MERGE_BARRIER"] = str(barrier)
        self._install_git_shim('''
            import time

            if args[:1] == ['-C'] and args[2:4] == ['apply', '--binary']:
                verify = Path(args[1]).resolve()
                barrier = Path(os.environ['TEST_CODEX_MERGE_BARRIER'])
                (barrier / verify.parent.name).write_text(str(verify), encoding='utf-8')
                deadline = time.monotonic() + 12
                while len(list(barrier.iterdir())) < 2:
                    assert time.monotonic() < deadline, 'verify worktrees did not coexist'
                    time.sleep(0.02)
        ''')
        self._stub_action(r'''
            (target / 'A.txt').write_text(f'worker {artifacts.name}\n', encoding='utf-8')
        ''')
        spec = self.corpus / "spec.md"
        spec.write_text("Change A.\n", encoding="utf-8")
        batches = []
        try:
            for task_name in ("first", "second"):
                self.out = self.temp / task_name
                self.out.mkdir()
                (self.out / "_worktrees").symlink_to(shared, target_is_directory=True)
                command = self._wrapper_command(
                    "run_codex_parallel.sh", "--dir", self.repo, "--mode", "implement",
                    "--task", f"{task_name}:{spec}",
                )
                proc = subprocess.Popen(command, cwd=self.temp, env=self.env, text=True,
                                        encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                batches.append((self.out, proc))
            for self.out, proc in batches:
                stdout, stderr = proc.communicate(timeout=25)
                summary_path = self.out / "parallel-summary.json"
                if not summary_path.is_file():
                    self.fail(stdout + stderr)
                summary = self._read_json(summary_path)
                diagnostics = [task.get("error") for task in summary["tasks"]]
                diagnostics += [check["error"] for check in summary["merge_check"]]
                self.assertEqual(proc.returncode, 0, f"{stdout}{stderr}\n{diagnostics}")
                self.assertEqual(summary["status"], "ok")
                self.assertIs(summary["merge_ok"], True)
                check, = summary["merge_check"]
                self.assertIs(check["base_reconstruct_ok"], True)
                self.assertIs(check["target_apply_ok"], True)
                task, = summary["tasks"]
                patch = self._assert_custody(task, ["A.txt"])
                self.assertIn(f"+worker {self.out.name}".encode(), patch.read_bytes())
        finally:
            for _, proc in batches:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
        verify_paths = {Path(marker.read_text(encoding="utf-8")) for marker in barrier.iterdir()}
        self.assertEqual(len(verify_paths), 2)
        self.assertTrue(all(path.parent.parent == shared for path in verify_paths))
        self.assertEqual(list(shared.iterdir()), [])
        registered = self._git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(registered.count(b"worktree "), 1)


if __name__ == "__main__":
    unittest.main()
