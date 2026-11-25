"""Verify the Google Antigravity plugin using only the standard library."""

import ast
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
PROFILES = CORE_SKILL / "config" / "profiles.json"
REFERENCES = CORE_SKILL / "references"
SCRIPTS = CORE_SKILL / "scripts"
GUARD = SCRIPTS / "hook_delegate_guard.py"
CONTEXT_GUARD = SCRIPTS / "hook_context_guard.py"
HANDOFF_SH = SCRIPTS / "handoff.sh"
RULES = ROOT / "rules" / "AGENTS.md"
AGENT = ROOT / "agents" / "codex-worker.md"
REMOTE_RUNNER = ROOT / "agents" / "remote-runner.md"
SKILL_NAMES = ("codex-orchestrator", "impl", "review", "dag", "research", "debate", "analyze", "handoff")


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
        self.assertEqual(plugin["version"], "3.8.0")
        self.assertEqual(json.loads(PROFILES.read_text(encoding="utf-8"))["version"], plugin["version"])
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
        context_hooks = hooks["codex-context-handoff"]
        self.assertEqual(set(context_hooks), {"PreInvocation", "Stop"})
        context_guard_path = "./" + CONTEXT_GUARD.relative_to(ROOT).as_posix()
        for event, entries in context_hooks.items():
            with self.subTest(event=event):
                self.assertTrue(any(
                    hook["type"] == "command" and context_guard_path in hook["command"].split()
                    for hook in entries
                ))
        for shim in (GUARD, CONTEXT_GUARD, HANDOFF_SH, SCRIPTS / "run_codex.sh", SCRIPTS / "run_codex_parallel.sh"):
            self.assertEqual(stat.S_IMODE(shim.stat().st_mode), 0o755, shim)

    def test_all_output_schemas_are_strict(self):
        required_by_schema = {
            "review": {"verdict", "summary", "findings", "verification", "unverified_areas"},
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
                if node.get("type") == "string" and "enum" not in node:
                    with self.subTest(node=location):
                        self.assertTrue(node.get("description", "").strip())
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
                self.assertTrue(schema.get("description", "").strip())
                self.assertEqual(set(schema["required"]), required)
                check_node(schema, "$")

    def test_rules_and_agent_frontmatter(self):
        self.assertTrue(RULES.is_file(), RULES)
        rules = RULES.read_text(encoding="utf-8")
        self.assertTrue(rules.strip(), RULES)
        self.assertLessEqual(len(rules.splitlines()), 30)
        self.assertIn("remote-runner", rules)
        self.assertIn("research", rules)

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

        remote = self._frontmatter(REMOTE_RUNNER)
        self.assertEqual(remote["name"], "remote-runner")
        self.assertIs(remote["subagent"], True)
        self.assertIs(remote["mainAgent"], False)
        self.assertIs(remote["inheritMcp"], False)
        self.assertIs(remote["inheritCustomizations"], True)
        self.assertIsInstance(remote["tools"], list)
        for tool in ("run_command", "read_file", "manage_task"):
            self.assertIn(tool, remote["tools"])
        for tool in ("edit_file", "write_file", "replace", "schedule"):
            self.assertNotIn(tool, remote["tools"])

    def test_eight_skills_valid_and_no_stale_templates(self):
        skill_files = [SKILLS / name / "SKILL.md" for name in SKILL_NAMES]
        for path in skill_files:
            with self.subTest(frontmatter=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), path)
                skill = self._frontmatter(path)
                self.assertEqual(skill["name"], path.parent.name)
                self.assertIsInstance(skill["description"], str)
                self.assertTrue(skill["description"].strip(), path)

        for path in [
            *skill_files, RULES, AGENT, REMOTE_RUNNER, ROOT / "README.md",
            REFERENCES / "user-brief-template.md",
            REFERENCES / "research-spec-template.md",
            REFERENCES / "handoff-packet-template.md",
        ]:
            with self.subTest(stale_syntax=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                for marker in ("{{args}}", "!{", "~/.gemini/extensions/"):
                    self.assertNotIn(marker, content, f"{path} contains {marker!r}")

    def test_communication_charter_consistency(self):
        charter = REFERENCES / "user-brief-template.md"
        required_markers = {
            charter: (
                "沟通宪章 v3.2",
                "合成防火墙",
                "客观产物优先",
                "反静默跳过核查",
                "表达与保真五条铁律",
                "按任务量级动态路由",
                "consult",
                "轻量代码改动",
                "重量级工程交付",
                "深度调研 / 红队辩论 / 实验核查",
            ),
            RULES: ("Communication Charter v3.2", "Dynamic Structure Routing", charter.name),
            ROOT / "README.md": ("v3.8.0", "Communication Charter v3.2", charter.name),
            SKILLS / "codex-orchestrator" / "SKILL.md": ("Communication Charter v3.2", charter.name),
            SKILLS / "impl" / "SKILL.md": ("Communication Charter v3.2", "lightweight route", charter.name),
            SKILLS / "review" / "SKILL.md": ("Communication Charter v3.2", "review", charter.name),
            SKILLS / "dag" / "SKILL.md": ("Communication Charter v3.2", "heavyweight", charter.name),
            SKILLS / "research" / "SKILL.md": ("Communication Charter v3.2", "deep research", charter.name),
            SKILLS / "debate" / "SKILL.md": ("Communication Charter v3.2", "deep research", charter.name),
            SKILLS / "analyze" / "SKILL.md": ("Communication Charter v3.2", "deep research", charter.name),
            SKILLS / "handoff" / "SKILL.md": ("Communication Charter v3.2", charter.name),
        }
        for template in (REFERENCES / "task-spec-template.md", REFERENCES / "research-spec-template.md"):
            content = template.read_text(encoding="utf-8")
            self.assertNotIn("## Execution Budget", content)
            self.assertNotIn("concise", content.lower())
        self.assertIn("主表优先使用规范中文概念名", charter.read_text(encoding="utf-8"))

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
        for script in ("run_codex.sh", "run_codex_parallel.sh", HANDOFF_SH.name):
            with self.subTest(script=script):
                self._run(["bash", "-n", str(SCRIPTS / script)])
        modules = sorted((SCRIPTS / "codex_orch").glob("*.py"))
        self.assertTrue(modules)
        for path in (GUARD, CONTEXT_GUARD, *modules):
            compile(path.read_bytes(), path.name, "exec")
        for name in ("research_parallel.py", "debate_multi_critic.py"):
            path = CORE_SKILL / "workflows" / name
            compile(path.read_bytes(), path.name, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)

        personal_path = re.compile(rb"/Users/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/|\b)")
        offending_paths = []
        bytecode_dirs = []

        def raise_walk_error(error):
            raise error

        for directory, subdirectories, filenames in os.walk(ROOT, onerror=raise_walk_error):
            if "__pycache__" in subdirectories:
                bytecode_dirs.append((Path(directory) / "__pycache__").relative_to(ROOT).as_posix())
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
        self.assertEqual(sorted(bytecode_dirs), [], "Generated bytecode directories")

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
                env.pop("PYTHONDONTWRITEBYTECODE", None)
                if profile is not None:
                    env["CODEX_GUARD_PROFILE"] = profile
                output = self._run([sys.executable, str(GUARD)], input=payload, env=env)
                response = json.loads(output)
                self.assertEqual(response["decision"], decision)
                if decision == "deny":
                    self.assertRegex(response["reason"], r"run_codex\.sh implement.*codex-worker")


    def test_cross_host_guard_adapter(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch.host_adapter import evaluate_guard_payload

        target = "/project/app.py"
        for tool, args, gemini_tool, gemini_args, decision in (
            ("write_file", {"Content": "print(1)"}, "write_file", {"content": "print(1)"}, "deny"),
            ("edit_file", {"Target": "old", "Replacement": "new"},
             "replace", {"old_string": "old", "new_string": "new"}, "allow"),
            ("edit_file", {"Replacement": "new\n" * 11},
             "replace", {"new_string": "new\n" * 11}, "deny"),
        ):
            antigravity = {"toolCall": {"name": tool, "args": {"Path": target, **args}}}
            gemini = {"tool_name": gemini_tool, "tool_input": {"file_path": target, **gemini_args}}
            with self.subTest(tool=gemini_tool, decision=decision):
                expected = evaluate_guard_payload(antigravity, host="antigravity", environ={})
                self.assertEqual(expected["decision"], decision)
                self.assertEqual(evaluate_guard_payload(gemini, host="gemini-cli", environ={}), expected)


class SupervisorCleanupTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import supervisor
        self.supervisor = supervisor
        self.process = mock.Mock(getpid=lambda: 10, getpgrp=lambda: 10)
        elapsed = 0.0

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        for patcher in (
            mock.patch.object(supervisor, "os", self.process),
            mock.patch.object(supervisor, "time", mock.Mock(monotonic=lambda: elapsed, sleep=sleep)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_signals(self, *, groups, pids=()):
        for sender, expected in ((self.process.killpg, groups), (self.process.kill, pids)):
            self.assertCountEqual([call.args for call in sender.call_args_list if call.args[1]], expected)

    def test_group_cleanup_signals_each_group_once_and_rechecks_start_identity(self):
        initial = {101: (100, 100, "a"), 102: (101, 102, "b"), 103: (102, 102, "c"),
                   104: (100, 10, "d"), 105: (100, 105, "e")}
        final = {**initial, 104: (1, 10, "reused"), 105: (1, 105, "reused"), 103: (1, 106, "c")}
        with mock.patch.object(self.supervisor, "_read_process_table", side_effect=[initial, final]):
            self.supervisor._terminate_group(100, 0, signal.SIGTERM)
        self.assert_signals(groups=[(100, signal.SIGTERM), (102, signal.SIGTERM), (105, signal.SIGTERM),
                                    (100, signal.SIGKILL), (102, signal.SIGKILL), (106, signal.SIGKILL)],
                            pids=[(104, signal.SIGTERM)])

    def test_group_cleanup_only_signals_new_targets_during_grace(self):
        initial = {101: (100, 101, "a"), 102: (100, 10, "b")}
        refreshed = {**initial, 103: (100, 100, "c"), 104: (101, 101, "d"),
                     105: (103, 105, "e"), 106: (102, 10, "f")}
        with mock.patch.object(self.supervisor, "_read_process_table", side_effect=[initial, refreshed, refreshed]):
            self.supervisor._terminate_group(100, 0.3, signal.SIGTERM)
        self.assert_signals(groups=[(100, signal.SIGTERM), (101, signal.SIGTERM), (105, signal.SIGTERM),
                                    (100, signal.SIGKILL), (101, signal.SIGKILL), (105, signal.SIGKILL)],
                            pids=[(102, signal.SIGTERM), (106, signal.SIGTERM),
                                  (102, signal.SIGKILL), (106, signal.SIGKILL)])

    def test_group_cleanup_falls_back_to_root_when_process_table_fails(self):
        start = "Mon Nov 23 19:43:28 2026"
        snapshot = subprocess.CompletedProcess([], 0, stdout=f" 101 100 101 {start}\n")
        for failure in (subprocess.CompletedProcess([], 1), PermissionError()):
            for first in (snapshot, failure):
                with self.subTest(failure=type(failure).__name__, tracked=first is snapshot):
                    self.process.reset_mock()
                    self.process.environ = {"LC_ALL": "different"}
                    with mock.patch.object(subprocess, "run", side_effect=[first, failure]) as run:
                        self.supervisor._terminate_group(100, 0, signal.SIGTERM)
                    groups = [(100, signal.SIGTERM), (100, signal.SIGKILL)]
                    if first is snapshot:
                        groups.append((101, signal.SIGTERM))
                    self.assert_signals(groups=groups)
                    self.assertEqual(run.call_count, 2)
                    run.assert_called_with(["ps", "-Ao", "pid=,ppid=,pgid=,lstart="],
                                           env={"LC_ALL": "C"}, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, timeout=2.0, check=False)

    def test_group_cleanup_uses_verified_prior_samples_after_root_exits(self):
        prior = {101: (101, "a"), 102: (102, "b"), 103: (103, "c")}
        table = {101: (1, 105, "a"), 102: (1, 102, "reused")}
        self.process.killpg.side_effect = [ProcessLookupError(), None]
        self.process.kill.side_effect = ProcessLookupError
        with mock.patch.object(self.supervisor, "_read_process_table", return_value=table) as read:
            self.supervisor._terminate_group(100, 0, signal.SIGTERM, lambda: 0, prior_descendants=prior)
        self.assert_signals(groups=[(105, signal.SIGTERM)])
        read.assert_called_once()


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
            out_dir = last_message.parents[2] if last_message.parent.parent.name == "attempts" else last_message.parent
            (out_dir / "codex-argv.json").write_text(
                json.dumps(argv), encoding="utf-8"
            )
            if keys := os.environ.get("TEST_CODEX_ENV_KEYS"):
                (out_dir / "codex-env.json").write_text(
                    json.dumps({key: os.environ[key] for key in keys.split() if key in os.environ}),
                    encoding="utf-8",
                )
            if action := os.environ.get("TEST_CODEX_ACTION"):
                runpy.run_path(action, init_globals={
                    "target": Path(argv[argv.index("-C") + 1]),
                    "artifacts": out_dir,
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
            if out_dir.name == os.environ.get("TEST_CODEX_BAD_TASK"):
                message = "{broken"
            last_message.write_text(message, encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "test-thread"}))
            for event in json.loads(os.environ.get("TEST_CODEX_EVENTS", "[]")):
                print(event if isinstance(event, str) else json.dumps(event))
            print(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": int(os.environ.get("TEST_CODEX_INPUT_TOKENS", "1")),
                "cached_input_tokens": 0, "output_tokens": 1,
            }}))
            if stderr := os.environ.get("TEST_CODEX_STDERR"):
                print(stderr, file=sys.stderr)
            sys.exit(int(os.environ.get("TEST_CODEX_EXIT", "0")))
            """), encoding="utf-8")
        self.stub.chmod(0o755)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "CODEX_", "TEST_CODEX_", "PYTHON"))
            and key not in ("BASH_ENV", "ENV", "XDG_CONFIG_HOME", "GITHUB_TOKEN", "SSH_AUTH_SOCK")
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

    def _separate_pgid_sleeper(self, *, root_exits=False):
        table = self.temp / "process-table.txt"
        self.addCleanup(self._kill_fixture_grandchild, table)
        try:
            subprocess.run(
                ["ps", "-Ao", "pid=,ppid=,pgid=,lstart="], env=self.env, capture_output=True,
                timeout=2, check=True,
            )
        except PermissionError:
            # Managed sandboxes can block ps. Keep the targets live and fixture its output.
            ps = self.temp / "ps"
            ps.write_text(dedent(f"""\
                #!/usr/bin/env python3
                import sys
                from pathlib import Path

                assert sys.argv[1:] == ["-Ao", "pid=,ppid=,pgid=,lstart="]
                print(Path({str(table)!r}).read_text(), end="")
                """), encoding="utf-8")
            ps.chmod(0o755)
        child_code = dedent("""\
            import signal
            import sys
            import time

            def stop(*_):
                print("detached child stopped", flush=True)
                sys.exit(0)

            signal.signal(signal.SIGTERM, stop)
            time.sleep(30)
            """)
        return table, dedent(f"""\
            import os
            import signal
            import subprocess
            import sys
            import time
            from pathlib import Path

            child = subprocess.Popen(
                [sys.executable, "-B", "-c", {child_code!r}],
                start_new_session=True, stdout=None if {root_exits!r} else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def reap_child(*_):
                child.wait()
                sys.exit(0)

            # Reap the grandchild after the supervisor signals it, even if PID 1 reaps slowly.
            signal.signal(signal.SIGTERM, reap_child)
            table = Path({str(table)!r})
            pending = table.with_suffix(".tmp")
            lstart = "Mon Nov 23 19:43:28 2026"
            pending.write_text(
                f"{{os.getpid()}} {{os.getppid()}} {{os.getpgrp()}} {{lstart}}\\n"
                f"{{child.pid}} {{os.getpid()}} {{os.getpgid(child.pid)}} {{lstart}}\\n"
            )
            pending.replace(table)
            time.sleep({1 if root_exits else 30})
            """)

    def _kill_fixture_grandchild(self, table):
        if table.exists():
            pid = int(table.read_text(encoding="utf-8").splitlines()[1].split()[0])
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def _assert_separate_pgid_child_reclaimed(self, table):
        root, grandchild = [list(map(int, row.split(None, 3)[:3]))
                            for row in table.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(grandchild[1], root[0])
        self.assertNotEqual(grandchild[2], root[2])
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild[0], 0)
        table.unlink()

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
            ("sol-consult", ("consult",), "gpt-6.2-sol", "medium", "ultrafast"),
            ("astra-medium", ("research",), "gpt-6.2-astra", "medium", "fast"),
            ("astra-downgrade", ("research", "--speed", "ultrafast"), "gpt-6.2-astra", "medium", "fast"),
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

    def test_run_codex_verbosity_routing_and_override(self):
        profile_path = SKILLS / "codex-orchestrator" / "config" / "profiles.json"
        profiles = self._read_json(profile_path)
        profiles["models"]["gpt-6.2-sol"]["supports_verbosity"] = False
        unsupported = self.corpus / "unsupported.json"
        unsupported.write_text(json.dumps(profiles), encoding="utf-8")
        del profiles["valid_verbosities"], profiles["valid_stdout_modes"]
        del profiles["defaults"]["verbosity_by_mode"], profiles["defaults"]["stdout_mode_by_mode"]
        for capabilities in (*profiles["models"].values(), profiles["fallback_model_capabilities"]):
            del capabilities["supports_verbosity"]
        legacy = self.corpus / "legacy.json"
        legacy.write_text(json.dumps(profiles), encoding="utf-8")

        for name, mode, flags, expected, config_values in (
            ("implement", "implement", (), "low", ["low"]),
            ("consult", "consult", (), "medium", ["medium"]),
            ("research", "research", (), "medium", ["medium"]),
            ("override", "consult", ("--verbosity", "high"), "high", ["high"]),
            ("config", "consult", ("-c", 'model_verbosity="high"'), "medium", ["medium", "high"]),
            ("inherit", "implement", ("--verbosity", "inherit"), "inherit", []),
            ("profile", "consult", ("--profile", "example"), "inherit", []),
            ("profile-override", "consult", ("--profile", "example", "--verbosity", "low"), "low", ["low"]),
            ("unsupported", "implement", ("--profiles-file", unsupported), "inherit", []),
            ("legacy", "implement", ("--profiles-file", legacy), "inherit", []),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                result = self._run_wrapper("run_codex.sh", mode, "--dir", self.corpus,
                                           "--prompt", "Inspect the corpus.", *flags)
                argv = self._read_json(self.out / "codex-argv.json")
                self.assertEqual(
                    [value for flag, value in zip(argv, argv[1:])
                     if flag == "-c" and value.startswith("model_verbosity=")],
                    [f'model_verbosity="{value}"' for value in config_values],
                )
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["verbosity"], expected)
                self.assertIn(f"verbosity: {expected}\n", result.stdout)
                if name == "unsupported":
                    self.assertIn("does not support model verbosity", result.stderr)
                if name == "legacy":
                    self.assertEqual(summary["stdout_mode"], "auto")

        self.out = self.temp / "invalid-verbosity"
        self._consult("--verbosity", "verbose", exit_code=2, error="invalid --verbosity")

    def test_run_codex_clarity_footer_and_no_concise_trigger(self):
        spec = self.corpus / "spec.md"
        spec.write_text("Inspect the corpus.\n", encoding="utf-8")
        for name, flags, budget, clarity, effective in (
            ("default", (), True, True, True),
            ("no-budget", ("--max-cmds", 0), False, True, True),
            ("no-clarity", ("--no-clarity-footer",), True, False, True),
            ("original", ("--max-cmds", 0, "--no-clarity-footer"), False, False, False),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                self._run_wrapper("run_codex.sh", "consult", "--dir", self.corpus, "--spec", spec, *flags)
                summary = self._read_json(self.out / "summary.json")
                prompt = Path(summary["attempt_dir"]) / "effective_prompt.md"
                self.assertEqual(prompt.exists(), effective)
                body = (prompt if effective else spec).read_text(encoding="utf-8")
                self.assertEqual("## Execution Budget" in body, budget)
                self.assertEqual("## Output Clarity Contract" in body, clarity)
                self.assertNotIn("concise", body)
                self.assertIs(summary["clarity_footer"], clarity)
                if budget and clarity:
                    self.assertLess(body.index("## Execution Budget"), body.index("## Output Clarity Contract"))

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

    def test_supervisor_cleans_sampled_separate_group_after_clean_root_exit(self):
        table, sleeper = self._separate_pgid_sleeper(root_exits=True)
        self._stub_action(sleeper)
        self._consult()
        self.assertIn("detached child stopped", (self.out / "events.jsonl").read_text(encoding="utf-8"))
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual((summary["status"], summary["codex_exit_code"]), ("ok", 0))
        table.unlink()

    def test_run_codex_stall_timeout_stops_silent_child(self):
        self._stub_action("import time\ntime.sleep(10)\n")
        self._consult("--stall-timeout", 1, exit_code=125)
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["status"], "stall_timeout")
        self.assertEqual(summary["exit_code"], 125)
        self.assertEqual(summary["codex_exit_code"], -signal.SIGTERM)
        self.assertIs(summary["stall_timed_out"], True)

    def test_run_codex_stall_timeout_reclaims_separate_pgid_grandchild(self):
        table, sleeper = self._separate_pgid_sleeper()
        self._stub_action(sleeper)
        self._consult("--stall-timeout", 1, exit_code=125)
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual((summary["status"], summary["exit_code"]), ("stall_timeout", 125))
        self._assert_separate_pgid_child_reclaimed(table)

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
            with (artifacts / 'run_count').open('a', encoding='utf-8') as log:
                print('attempt', file=log)
        ''')
        error = {"type": "error", "message": "usage_limit_reached"}
        failed = {"type": "turn.failed", "error": {"message": "HTTP 429"}}
        worker_output = [
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "rate_limit_exceeded"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "rate_limit_exceeded"}},
            "malformed stdout mentioning rate_limit_exceeded",
        ]
        for source, events, stderr, child_exit, status in (
            ("stderr", [], "rate_limit_exceeded", 19, "paused_quota"),
            ("error", [error], "", 20, "paused_quota"),
            ("turn-failed", [failed], "", 21, "paused_quota"),
            ("successful-output", worker_output, "", 0, "ok"),
            ("failed-output", worker_output, "", 22, "error"),
            ("successful-error-marker", [error], "rate_limit_exceeded", 0, "ok"),
        ):
            with self.subTest(source=source):
                self.out = self.temp / source
                self.env.update(TEST_CODEX_EXIT=str(child_exit), TEST_CODEX_STDERR=stderr,
                                TEST_CODEX_EVENTS=json.dumps(events))
                self._run_wrapper(
                    "run_codex.sh", "implement", "--dir", self.corpus, "--prompt", "Update the corpus.",
                    exit_code=child_exit,
                )
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual((self.out / "run_count").read_text(encoding="utf-8"), "attempt\n")
                self.assertEqual(summary["status"], status)
                self.assertIs(summary["paused_quota"], status == "paused_quota")
                self.assertEqual(summary["codex_exit_code"], child_exit)
                self.assertEqual(summary["exit_code"], child_exit)
                self.assertIs(summary["timed_out"], False)

    def test_declarative_profiles_single_and_parallel(self):
        config = self._read_json(PROFILES)
        config["defaults"]["model_by_mode"]["consult"] = "fixture-model"
        config["defaults"]["max_cmds_by_mode"]["consult"] = 3
        config["fallback_model_capabilities"]["low_medium_speed"] = "ultrafast"
        config["defaults"]["schema_by_mode"]["consult"] = "research-output-schema.json"
        custom = self.temp / "profiles.json"
        custom.write_text(json.dumps(config), encoding="utf-8")
        manifest = self._manifest([{"name": "profiled"}])
        for script, options, worker in (
            ("run_codex.sh", ["consult", "--prompt", "Inspect the corpus."], ""),
            ("run_codex_parallel.sh", ["--manifest", manifest], "profiled"),
        ):
            with self.subTest(script=script):
                self.out = self.temp / script
                self._run_wrapper(script, *options, "--dir", self.corpus, "--structured", "--profiles-file", custom)
                worker_out = self.out / worker
                summary = self._read_json(worker_out / "summary.json")
                self.assertEqual((summary["model"], summary["speed"], summary["max_cmds"]),
                                 ("fixture-model", "ultrafast", 3))
                self.assertIs(summary["structured_valid"], True)
                argv = self._read_json(worker_out / "codex-argv.json")
                self.assertEqual(argv[argv.index("-m") + 1], "fixture-model")
                self.assertIn('service_tier="ultrafast"', argv)
                self.assertEqual(Path(argv[argv.index("--output-schema") + 1]).name, "research-output-schema.json")

        custom.write_text("{broken", encoding="utf-8")
        self.out = self.temp / "invalid-profile"
        self._consult("--profiles-file", custom, exit_code=2, error="invalid profiles file")
        self._run_wrapper("run_codex_parallel.sh", "--manifest", manifest, "--profiles-file", custom,
                          exit_code=2, error="invalid profiles file")

    def test_attempt_history_preserves_prior_artifacts(self):
        attempts = self.out / "attempts"
        first, second = (attempts / f"attempt-{number:03}" for number in (1, 2))
        names = ("events.jsonl", "last_message.md", "summary.json")
        self.env.update(TEST_CODEX_LAST_MESSAGE="first response",
                        TEST_CODEX_EVENTS=json.dumps([{"type": "heartbeat", "index": 1}]))
        self._consult()
        self.assertEqual(self._read_json(self.out / "summary.json")["attempt_dir"], str(first))
        original = {name: ((first / name).read_bytes(), (first / name).stat().st_mtime_ns) for name in names}

        self.env.update(TEST_CODEX_LAST_MESSAGE="second response",
                        TEST_CODEX_EVENTS=json.dumps([{"type": "heartbeat", "index": 2}]))
        self._consult()
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual((summary["attempt_num"], summary["attempt_dir"]), (2, str(second)))
        argv = self._read_json(self.out / "codex-argv.json")
        self.assertEqual(Path(argv[argv.index("-o") + 1]), second / "last_message.md")
        for name, (content, timestamp) in original.items():
            self.assertEqual((first / name).read_bytes(), content)
            self.assertEqual((first / name).stat().st_mtime_ns, timestamp)
            self.assertNotEqual((second / name).read_bytes(), content)
        for key, name in (("events", "events.jsonl"), ("last_message", "last_message.md")):
            self.assertEqual(Path(summary["attempt_artifacts"][key]), second / name)
            self.assertEqual((self.out / name).read_bytes(), (second / name).read_bytes())
        self.assertEqual(sorted(path.name for path in attempts.glob("attempt-*")), [first.name, second.name])

    def test_resume_inherits_model_and_dirs_from_deep_and_external_summaries(self):
        runs = self.temp / "runs"
        writable = self.temp / "extra writable dir"
        writable.mkdir()
        for name, prior_dir, resume_dir in (
            ("deep", runs / "job" / "out with space" / "worker" / "round1", runs / "resume"),
            ("external", self.temp / "nested_job" / "round1", self.temp / "nested_job" / "round2"),
        ):
            with self.subTest(location=name):
                thread_id = f"test-thread-{name}"
                prior = prior_dir / "summary.json"
                prior.parent.mkdir(parents=True)
                data = {"thread_id": thread_id, "model": "gpt-6.2-sol", "add_dirs": [str(writable)]}
                prior.write_text(json.dumps(data), encoding="utf-8")
                os.utime(prior, (1, 1))
                nested = prior_dir / "attempts" / "attempt-001" / "summary.json"
                nested.parent.mkdir(parents=True)
                nested.write_text(json.dumps({**data, "model": "gpt-6.2-astra", "add_dirs": []}),
                                  encoding="utf-8")
                self.assertGreater(nested.stat().st_mtime, prior.stat().st_mtime)

                self.out = resume_dir
                args = ("resume", "--id", thread_id, "--dir", str(self.corpus), "--sandbox", "workspace-write",
                        "--speed", "ultrafast", "--prompt", "Continue.")
                if name == "deep":
                    command = [
                        sys.executable, "-I", "-B", "-c",
                        "import sys\nfrom pathlib import Path\n"
                        "sys.path.insert(0, sys.argv.pop(1))\nfrom codex_orch import single_runner\n"
                        "single_runner.RUNS_DIR = Path(sys.argv.pop(1))\nsys.exit(single_runner.main())",
                        str(SCRIPTS), str(runs), *args, "--out-dir", str(self.out),
                    ]
                    result = subprocess.run(command, cwd=self.temp, env=self.env, capture_output=True,
                                            text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    result = self._run_wrapper("run_codex.sh", *args)
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual((summary["model"], summary["speed"], summary["add_dirs"]),
                                 ("gpt-6.2-sol", "ultrafast", [str(writable)]))
                self.assertEqual(summary["prior_session"]["summary_json"], str(prior))
                self.assertNotIn("downgrading speed", result.stderr)
                argv = self._read_json(self.out / "codex-argv.json")
                self.assertIn(thread_id, argv)
                self.assertIn('service_tier="ultrafast"', argv)
                self.assertIn(f"sandbox_workspace_write.writable_roots={json.dumps([str(self.corpus), str(writable)])}",
                              argv)

    def test_prior_session_rejects_excluded_and_symlinked_output_paths(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import single_runner

        external = self.temp / "external"
        actual = external / "actual"
        for directory in (actual, self.temp / "attempts" / "old", self.temp / "_worktrees" / "old"):
            directory.mkdir(parents=True)
            (directory / "summary.json").write_text(json.dumps({
                "thread_id": "test-thread", "model": "gpt-6.2-sol", "add_dirs": [str(self.corpus)],
            }), encoding="utf-8")
        symlink = self.temp / "alias-location" / "linked"
        symlink.parent.mkdir()
        symlink.symlink_to(actual, target_is_directory=True)
        symlink_parent = self.temp / "linked-parent"
        symlink_parent.symlink_to(external, target_is_directory=True)

        with mock.patch.object(single_runner, "RUNS_DIR", self.temp / "runs"):
            for directory in (self.temp / "attempts" / "old", self.temp / "_worktrees" / "old",
                              symlink, symlink_parent / "actual", symlink_parent / "sibling"):
                with self.subTest(directory=directory):
                    self.assertIsNone(single_runner.prior_session(
                        "test-thread", inherit_dirs=True, out_dir=directory,
                    ))

    def test_prior_session_validates_attempt_summary_path(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import single_runner

        summary = self.out / "summary.json"
        attempt_dir = self.out / "attempts" / "attempt-001"
        attempt_dir.mkdir(parents=True)
        immutable = attempt_dir / "summary.json"
        immutable.touch()
        outside = self.temp / "outside.json"
        outside.touch()
        symlink = attempt_dir / "symlink.json"
        symlink.symlink_to(immutable)

        with mock.patch.object(single_runner, "RUNS_DIR", self.temp / "runs"):
            for fields, expected in (
                ({"attempt_dir": str(attempt_dir)}, immutable),
                ({"attempt_artifacts": {"summary_json": str(symlink)}}, summary),
                ({"attempt_artifacts": {"summary_json": str(outside)}}, summary),
            ):
                with self.subTest(fields=fields):
                    summary.write_text(json.dumps({"thread_id": "test-thread", **fields}), encoding="utf-8")
                    prior = single_runner.prior_session("test-thread", inherit_dirs=False, out_dir=self.out)
                    self.assertEqual(prior.summary_path, str(expected))

    def test_resume_reusing_output_links_prior_immutable_summary(self):
        self.env.update(TEST_CODEX_EXIT="17", TEST_CODEX_INPUT_TOKENS="123")
        self._consult(exit_code=17)
        first = self.out / "attempts" / "attempt-001" / "summary.json"
        original = first.read_bytes()

        self.env.update(TEST_CODEX_EXIT="0", TEST_CODEX_INPUT_TOKENS="456")
        self._run_wrapper("run_codex.sh", "resume", "--id", "test-thread", "--dir", self.corpus,
                          "--prompt", "Continue.")
        latest = self._read_json(self.out / "summary.json")
        self.assertEqual((latest["attempt_num"], latest["status"], latest["tokens"]["input"]), (2, "ok", 456))
        self.assertEqual(latest["prior_session"], {
            "summary_json": str(first), "status": "error", "input_tokens": 123,
        })
        self.assertEqual(first.read_bytes(), original)
        prior = self._read_json(Path(latest["prior_session"]["summary_json"]))
        self.assertEqual((prior["attempt_num"], prior["status"], prior["tokens"]["input"]), (1, "error", 123))

    def test_resume_bloat_warning_is_advisory(self):
        for status, tokens, reasons in (
            ("ok", 1_000_000, ["prior input tokens (1,000,000) >= 1,000,000"]),
            ("ok", 1_250_000, ["prior input tokens (1,250,000) >= 1,000,000"]),
            ("stall_timeout", 0, ["prior session ended with status 'stall_timeout'"]),
            ("timeout", 0, ["prior session ended with status 'timeout'"]),
            ("timeout", 1_000_000, ["prior input tokens (1,000,000) >= 1,000,000",
                                    "prior session ended with status 'timeout'"]),
            ("ok", 120_000, []),
        ):
            with self.subTest(status=status, tokens=tokens):
                parent = self.temp / f"{status}-{tokens}"
                self.out = parent / "round2"
                prior = parent / "round1" / "summary.json"
                prior.parent.mkdir(parents=True)
                thread_id = f"test-thread-{status}-{tokens}"
                prior.write_text(json.dumps({
                    "thread_id": thread_id, "status": status, "tokens": {"input": tokens},
                }), encoding="utf-8")

                result = self._run_wrapper("run_codex.sh", "resume", "--id", thread_id, "--dir", self.corpus,
                                           "--prompt", "Continue.")
                summary = self._read_json(self.out / "summary.json")
                self.assertEqual(summary["exit_code"], 0)
                self.assertIs(summary["resume_bloat_warning"], bool(reasons))
                self.assertEqual(summary["prior_session"], {
                    "summary_json": str(prior), "status": status, "input_tokens": tokens,
                })
                if reasons:
                    self.assertEqual(summary["resume_bloat_reasons"], reasons)
                    joined = ", ".join(reasons)
                    self.assertIn(f"warning: resuming thread {thread_id} despite {joined};", result.stderr)
                    self.assertIn(f"tip: resume bloat warning ({joined})", result.stdout)
                else:
                    self.assertNotIn("resume_bloat_reasons", summary)
                    self.assertNotIn("warning: resuming", result.stderr)
                    self.assertNotIn("tip: resume bloat warning", result.stdout)

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

    def test_run_codex_rejects_self_hosting_add_dir_with_worktree(self):
        self._run_wrapper(
            "run_codex.sh", "implement", "--dir", ROOT, "--prompt", "Inspect the target.",
            "--worktree", "--add-dir", SCRIPTS / "codex_orch",
            exit_code=2, error="overlaps the running orchestrator scripts",
        )

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

    def test_run_codex_stdout_facts_mode_quarantines_prose_and_commentary(self):
        commentary = "S1-A mid-flight commentary."
        final_prose = "W23 final free-text report."
        raw = {
            "verdict": "needs_revision", "summary": final_prose,
            "findings": [{"severity": "P1", "issue": final_prose}, {"severity": "P3"}],
            "challenges": [{"severity": "P0"}],
            "verification": [{"exit_code": 0, "key_output": final_prose}, {"exit_code": 2}],
            "consistency_checks": [{"status": status} for status in ("match", "mismatch", "unverified")],
            "evidence": {"example": final_prose},
            "unverified_areas": [final_prose],
        }
        agent_message = {"type": "item.completed", "item": {"type": "agent_message", "text": commentary}}
        failed_command = {"type": "item.completed", "item": {
            "type": "command_execution", "exit_code": 2, "command": "python3 -B check.py",
            "aggregated_output": commentary,
        }}
        for name, message, structural in (
            ("object", json.dumps(raw), True),
            ("non-json", final_prose, False),
            ("scalar-fields", json.dumps({"summary": final_prose, "verdict": final_prose}), True),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                self.env.update(
                    TEST_CODEX_EXIT="17", TEST_CODEX_LAST_MESSAGE=message, TEST_CODEX_STDERR=commentary,
                    TEST_CODEX_EVENTS=json.dumps([
                        agent_message,
                        *([failed_command, {"type": "error", "message": commentary}] if name == "object" else []),
                    ]),
                )
                result = self._consult("--stdout-mode", "facts", exit_code=17)
                summary = self._read_json(self.out / "summary.json")
                self.assertIs(summary["stdout_truncated"], True)
                self.assertEqual(summary["partial_progress"]["last_agent_messages"], [commentary])
                self.assertEqual((self.out / "last_message.md").read_text(encoding="utf-8"), message)
                self.assertNotIn(commentary, result.stdout)
                self.assertNotIn(final_prose, result.stdout)
                self.assertIn(f"[facts preview: {len(message.encode('utf-8'))} bytes", result.stdout)
                self.assertIn(str(self.out / "last_message.md"), result.stdout)
                self.assertIn(str(self.out / "summary.json"), result.stdout)
                self.assertEqual("format: non-json" in result.stdout, not structural)
                if name == "object":
                    for expected in (
                        "verdict: needs_revision", "findings=2 (P1=1, P3=1)", "challenges=1 (P0=1)",
                        "verification=2 (passed=1, failed=1)", "evidence=1", "unverified_areas=1",
                        "consistency_checks=3 (match=1, mismatch=1, unverified=1)",
                        'failed_commands: [{"exit_code": 2, "command": "python3 -B check.py"}]',
                    ):
                        self.assertIn(expected, result.stdout)
                else:
                    self.assertNotIn("--- partial_progress ---", result.stdout)
                if name == "scalar-fields":
                    self.assertIn('keys=["summary", "verdict"]', result.stdout)

        self.out = self.temp / "full-failure"
        result = self._consult("--stdout-mode", "full", exit_code=17)
        self.assertNotIn("--- partial_progress ---", result.stdout)
        self.assertNotIn("last_agent_messages:", result.stdout)

    def test_run_codex_stdout_facts_mode_classifies_non_object_json(self):
        for name, message, expected in (
            ("empty-array", "[]", "format: json-array, items=0"),
            ("nonempty-array", '["secret prose"]', "format: json-array, items=1"),
            ("null", "null", "format: json-null"),
            ("boolean", "true", "format: json-boolean"),
            ("integer", "42", "format: json-number"),
            ("float", "1.5", "format: json-number"),
            ("string", '"secret prose"', "format: json-string"),
            ("invalid", "{broken", "format: non-json"),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                self.env["TEST_CODEX_LAST_MESSAGE"] = message
                result = self._consult("--stdout-mode", "facts")
                _, preview = result.stdout.split("--- last_message ---\n", 1)
                self.assertEqual(preview.splitlines()[0], expected)
                self.assertNotIn("secret prose", result.stdout)
                self.assertNotIn("{broken", result.stdout)

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

    def test_run_codex_review_uses_dedicated_review_schema(self):
        repo = self.temp / "review-repo"
        subprocess.run(["git", "init", "--quiet", str(repo)], env=self.env, check=True, timeout=15)
        review_schema = REFERENCES / "review-output-schema.json"
        receipt = {
            "verdict": "needs_revision", "summary": "The preview exposes implementation prose.",
            "findings": [{
                "severity": "P1", "category": "Output contract", "location": "runner.py:12",
                "issue": "The preview prints the complete report.",
                "impact": "The caller receives internal shorthand.",
                "suggested_fix": "Print the collection count and the report path.",
            }],
            "verification": [{"command": "git diff --check", "exit_code": 0, "key_output": "The check passed."}],
            "unverified_areas": ["Live model output was unavailable."],
        }
        self.env["TEST_CODEX_LAST_MESSAGE"] = json.dumps(receipt)
        result = self._run_wrapper("run_codex.sh", "review", "--dir", repo, "--structured", "--stdout-mode", "compact")
        self._assert_exec_argv(self.out, target=repo, schema=review_schema.resolve())
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["review_engine"], "read-only-exec")
        self.assertIs(summary["structured_valid"], True)
        self.assertIs(summary["clarity_footer"], True)
        self.assertIn("findings=1 (P1=1)", result.stdout)
        prompt = (Path(summary["attempt_dir"]) / "review_prompt.md").read_text(encoding="utf-8")
        self.assertIn("## Output Clarity Contract", prompt)

        self.out = self.temp / "invalid-review"
        self.env["TEST_CODEX_LAST_MESSAGE"] = json.dumps({**receipt, "findings": [
            {**receipt["findings"][0], "severity": "P4"},
        ]})
        self._run_wrapper("run_codex.sh", "review", "--dir", repo, "--structured", exit_code=3)

        self.out = self.temp / "native-review"
        del self.env["TEST_CODEX_LAST_MESSAGE"]
        self._run_wrapper("run_codex.sh", "review", "--dir", repo, "--uncommitted")
        summary = self._read_json(self.out / "summary.json")
        self.assertEqual(summary["review_engine"], "native-review")
        self.assertIs(summary["clarity_footer"], False)
        self.assertFalse((Path(summary["attempt_dir"]) / "review_prompt.md").exists())

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

    def test_parallel_forwards_verbosity_clarity_and_stdout_mode(self):
        config = self._read_json(PROFILES)
        config["defaults"]["stdout_mode_by_mode"]["consult"] = "full"
        custom = self.temp / "profiles.json"
        custom.write_text(json.dumps(config), encoding="utf-8")
        manifest = self._manifest([
            {"name": "inherits"},
            {"name": "overrides", "verbosity": "low", "no_clarity_footer": False, "stdout_mode": "compact"},
        ])

        # Record the flags at the worker-wrapper boundary, then run the real wrapper.
        scripts = self.temp / "scripts"
        scripts.mkdir()
        parallel = shutil.copy2(SCRIPTS / "run_codex_parallel.sh", scripts)
        (scripts / "codex_orch").symlink_to(SCRIPTS / "codex_orch", target_is_directory=True)
        worker = scripts / "run_codex.sh"
        worker.write_text(dedent(f'''\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            out = Path(args[args.index("--out-dir") + 1])
            (out / "worker-argv.json").write_text(json.dumps(args), encoding="utf-8")
            os.execv("/bin/bash", ["bash", {str(SCRIPTS / "run_codex.sh")!r}, *args])
            '''), encoding="utf-8")
        worker.chmod(0o755)
        self._run_wrapper(
            parallel, "--dir", self.corpus, "--manifest", manifest, "--profiles-file", custom,
            "--verbosity", "high", "--no-clarity-footer",
        )

        summary = self._read_json(self.out / "parallel-summary.json")
        self.assertEqual(summary["status"], "ok")
        self.assertEqual([(task["task"], task["verbosity"]) for task in summary["tasks"]], [
            ("inherits", "high"), ("overrides", "low"),
        ])
        for name, verbosity, footer, stdout_mode in (
            ("inherits", "high", False, "full"),
            ("overrides", "low", True, "compact"),
        ):
            with self.subTest(task=name):
                argv = self._read_json(self.out / name / "worker-argv.json")
                self.assertEqual(argv[argv.index("--profiles-file") + 1], str(custom))
                self.assertEqual(argv[argv.index("--verbosity") + 1], verbosity)
                self.assertEqual("--no-clarity-footer" in argv, not footer)
                if name == "inherits":
                    self.assertNotIn("--stdout-mode", argv)
                else:
                    self.assertEqual(argv[argv.index("--stdout-mode") + 1], "compact")
                worker_summary = self._read_json(self.out / name / "summary.json")
                self.assertIs(worker_summary["clarity_footer"], footer)
                self.assertEqual(worker_summary["stdout_mode"], stdout_mode)

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


class CodexGitTestCase(CodexTestCase):
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

    def _run_batch(self, entries, *options, script="run_codex_parallel.sh", **kwargs):
        self._run_wrapper(
            script, "--dir", self.repo, "--manifest", self._manifest(entries), *options, **kwargs,
        )
        if self.out.is_dir():
            summary = self._read_json(self.out / "parallel-summary.json")
            self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
            return summary


class CodexParallelGitTests(CodexGitTestCase):

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
                if (case == 'custody' and os.environ.get('GIT_INDEX_FILE')
                        and args[4:6] == ['apply', '--cached']):
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


class CodexV350ConformanceTests(CodexGitTestCase):
    UNIT_ARGV = ["python3", "-m", "unittest", "discover", "-s", "tests"]
    UNIT_TEST = """\
import os
import unittest
from src.app import value


class AppTests(unittest.TestCase):
    def test_value(self):
        for key in ("GITHUB_TOKEN", "SSH_AUTH_SOCK"):
            self.assertFalse(key in os.environ, f"{key} leaked into verification")
        print("trusted unit command ran with credentials stripped")
        result = value()
        if result is None:
            self.skipTest("implementation disabled the test")
        self.assertEqual(result, 2)
"""

    def _write(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _commit_fixture(self):
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "--quiet", "-m", "Commit verification fixture")
        self.base_oid = self._git(self.repo, "rev-parse", "HEAD").decode().strip()

    def _unit_fixture(self, *, contract=None, test=None):
        self._write(self.repo, "src/app.py", "def value():\n    return 1\n")
        if test is not None:
            self._write(self.repo, "tests/test_app.py", test)
        if contract is not None:
            self._write(self.repo, ".codex-orchestrator.json", json.dumps(contract))
        self._commit_fixture()

    def _implement(self, *options, **kwargs):
        summary_path = self.out / "summary.json"
        try:
            self._run_wrapper("run_codex.sh", "implement", "--dir", self.repo, "--prompt", "Fix app.py.",
                              *options, **kwargs)
        finally:
            if summary_path.is_file():
                summary = self._read_json(summary_path)
                if worker := summary.get("worktree_dir"):
                    path = Path(worker)
                    if path.is_dir():
                        self.addCleanup(self._remove_worker, path)
        return self._read_json(summary_path) if summary_path.is_file() else None

    def _remove_worker(self, path):
        self._git(self.repo, "worktree", "remove", "--force", path)
        path.parent.rmdir()

    def _assert_proof(self, record, argv, exit_code):
        self.assertEqual(record["base_oid"], self.base_oid)
        self.assertEqual(record["argv"], argv)
        self.assertEqual(record["exit_code"], exit_code)
        self.assertIs(record["ok"], exit_code == 0)
        self.assertTrue(record["candidate_tree_oid"])
        self.assertEqual(record["candidate_tree_oid"], record["reconstructed_tree_oid"])

    def _implement_value(self, value):
        self._stub_action(f"(target / 'src/app.py').write_text('def value():\\n    return {value}\\n')\n")

    def test_run_codex_implement_announces_host_verification_first(self):
        self._stub_action("(target / 'A.txt').write_text('verified change\\n', encoding='utf-8')\n")
        verify_argv = [
            "python3", "-B", "-c",
            "from pathlib import Path; assert Path('A.txt').read_text() == 'verified change\\n'",
        ]
        result = self._run_wrapper(
            "run_codex.sh", "implement", "--dir", self.repo, "--prompt", "Update A.txt.",
            "--worktree", "--structured", "--verify-cmd", json.dumps(verify_argv),
        )
        summary = self._read_json(self.out / "summary.json")
        self.addCleanup(self._remove_worker, Path(summary["worktree_dir"]))
        self.assertEqual(summary["stdout_mode"], "facts")
        self.assertIs(summary["structured_valid"], True)
        receipt = self._read_json(self.out / "last_message.md")
        self.assertEqual(receipt["summary"], "Stub response")
        host, preview = result.stdout.split("--- last_message ---\n", 1)
        self.assertIn('host_changed_files: 1 ["A.txt"]\n', host)
        self.assertRegex(host, r"(?m)^patch_file: .+\n")
        self.assertRegex(host, r"(?m)^patch_bytes: [1-9]\d*\n")
        self.assertIn("host_verification: ok=true exit_code=0 ", host)
        self.assertIn("[facts preview:", preview)
        self.assertNotIn(receipt["summary"], result.stdout)

    def test_trusted_contract_command_is_pinned_to_base_oid(self):
        self._unit_fixture(contract={"verify_commands": {"unit": self.UNIT_ARGV}, "protected_paths": ["tests/**"]},
                           test=self.UNIT_TEST)
        self._stub_action('''\
            import json
            contract = target / '.codex-orchestrator.json'
            contract.write_text(json.dumps({'verify_commands': {'unit': ['python3', '-c', 'pass']}}))
            (target / 'src/app.py').write_text('def value():\\n    return 2\\n')
        ''')
        summary = self._implement("--worktree", "--verify-preset", "unit")
        proof = self._read_json(self.out / "verification.json")
        self._assert_proof(proof, self.UNIT_ARGV, 0)
        self.assertIn("trusted unit command ran", proof["stdout_tail"])
        self.assertEqual(summary["contract_source"], f"git:{self.base_oid}:.codex-orchestrator.json")
        self.assertCountEqual(summary["patch"]["changed_files"], [".codex-orchestrator.json", "src/app.py"])

    def test_contract_forbids_hooks_and_invalid_argv_before_artifacts(self):
        for name, contract, reason in (
            ("hook", {"before_run": ["python3", "-c", "pass"]}, "before_run"),
            ("empty-command", {"verify_commands": {"unit": [""]}}, "non-empty argv"),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                self._unit_fixture(contract=contract)
                self._implement("--verify-preset", "unit", exit_code=2, error=reason)
                self._run_batch([{"name": "worker", "mode": "implement"}], "--verify-preset", "unit",
                                exit_code=2, error=reason)

    def test_contract_protects_paths_and_preserves_candidate(self):
        self._unit_fixture(contract={"protected_paths": ["tests/**"]}, test=self.UNIT_TEST)
        self._stub_action("(target / 'tests/test_app.py').write_text('# changed by worker\\n')\n")
        summary = self._implement("--worktree", exit_code=5)
        self.assertEqual((summary["status"], summary["exit_code"]), ("protected_path_violation", 5))
        self.assertEqual(summary["path_policy"]["protected_violations"], ["tests/test_app.py"])
        self.assertIs(summary["worktree_preserved"], True)
        self.assertEqual((self.repo / "tests/test_app.py").read_text(encoding="utf-8"), self.UNIT_TEST)

    def test_dirty_policy_only_contract_rejects_dirty_in_place_implementation(self):
        self._unit_fixture(contract={"dirty_policy": "fail"})
        self._write(self.repo, "A.txt", "dirty change\n")
        self._implement(exit_code=2, error="requires a clean checkout")

    def test_host_verification_failure_binds_tree_and_strips_credentials(self):
        self._unit_fixture(contract={"verify_commands": {"unit": self.UNIT_ARGV}}, test=self.UNIT_TEST)
        self.env.update(GITHUB_TOKEN="fixture-token", SSH_AUTH_SOCK="fixture-agent-socket")
        self._stub_action("(target / 'A.txt').write_text('candidate change\\n')\n")
        summary = self._implement("--worktree", "--verify-preset", "unit", exit_code=6)
        self.assertEqual((summary["status"], summary["exit_code"]), ("verification_failed", 6))
        proof = self._read_json(self.out / "verification.json")
        self._assert_proof(proof, self.UNIT_ARGV, 1)
        self.assertEqual(proof["candidate_tree_oid"], summary["patch"]["final_tree_oid"])
        self.assertIn("credentials stripped", proof["stdout_tail"])
        self.assertIn("AssertionError: 1 != 2", proof["stderr_tail"])

    def test_host_verification_cleans_up_background_grandchild_after_success(self):
        lock_path = self.temp / "verify-grandchild.lock"
        argv = ["python3", "-c", dedent('''\
            import fcntl
            import os
            import sys
            import time
            from pathlib import Path

            assert os.environ["GIT_CEILING_DIRECTORIES"] == str(Path.cwd().parent)
            ready_read, ready_write = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(ready_read)
                if os.fork() != 0:
                    os._exit(0)
                with open(os.devnull, "r+b") as devnull:
                    for fd in (0, 1, 2):
                        os.dup2(devnull.fileno(), fd)
                with open(sys.argv[1], "w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    print(os.getpid(), file=lock, flush=True)
                    os.write(ready_write, b"1")
                    os.close(ready_write)
                    time.sleep(20)
                os._exit(0)
            os.close(ready_write)
            assert os.read(ready_read, 1) == b"1"
            os.waitpid(child, 0)
        '''), str(lock_path)]
        summary = self._implement("--worktree", "--verify-cmd", json.dumps(argv))
        self.assertEqual(summary["status"], "ok")
        self._assert_proof(self._read_json(self.out / "verification.json"), argv, 0)
        # A reparented zombie can retain its PID, but it cannot hold the lock.
        with lock_path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with suppress(ProcessLookupError):
                    os.kill(int(lock.readline()), signal.SIGKILL)
                self.fail("the verification grandchild still holds the lock")

    def test_host_verification_timeout_reclaims_separate_pgid_grandchild(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import verify

        table, sleeper = self._separate_pgid_sleeper()
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):
            proof = verify.run_host_verification(
                self.repo, base_oid=self.base_oid, patches=[], expected_tree_oid=None,
                verify_argv=[sys.executable, "-B", "-c", sleeper],
                out_path=self.temp / "verification.json", worktrees_parent=self.temp / "verify-worktrees",
                timeout_s=1,
            )
        self.assertIs(proof["timed_out"], True, proof)
        self.assertEqual(proof["exit_code"], 124)
        self._assert_separate_pgid_child_reclaimed(table)

    def test_host_verification_reports_cleanup_error_after_success(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            from codex_orch import verify

        git = verify._git

        def fail_prune(repo, *args, **kwargs):
            if args == ("worktree", "prune"):
                raise OSError("fixture cleanup error")
            return git(repo, *args, **kwargs)

        out_path = self.temp / "verification.json"
        with mock.patch.object(verify, "_git", side_effect=fail_prune):
            proof = verify.run_host_verification(
                self.repo, base_oid=self.base_oid, patches=[], expected_tree_oid=None,
                verify_argv=["python3", "-c", "print('command completed')"],
                out_path=out_path, worktrees_parent=self.temp / "verify-worktrees",
            )
        self.assertEqual(proof, self._read_json(out_path))
        self.assertEqual((proof["ok"], proof["exit_code"]), (False, 125))
        self.assertIn("command completed", proof["stdout_tail"])
        self.assertIn("fixture cleanup error", proof["stderr_tail"])

    def test_tdd_requires_test_source_before_artifacts(self):
        self._implement(
            "--tdd", "--verify-cmd", json.dumps(self.UNIT_ARGV),
            exit_code=2, error="--tdd requires --tdd-test-dir or --tdd-test-path to freeze protected tests",
        )

    def test_tdd_rejects_empty_overlay_before_codex(self):
        self._unit_fixture(test=self.UNIT_TEST)
        overlay = self.temp / "empty-overlay"
        overlay.mkdir()
        summary = self._implement("--tdd", "--tdd-test-dir", overlay,
                                  "--verify-cmd", json.dumps(self.UNIT_ARGV), exit_code=4)
        self.assertEqual(summary["status"], "tdd_red_failed")
        self.assertFalse((self.out / "codex-argv.json").exists())
        proof = self._read_json(self.out / "verification.json")
        self.assertIn("at least one frozen file", proof["error"])
        self.assertIsNone(proof["red"])

    def test_tdd_rejects_non_assertion_red_before_codex(self):
        self._unit_fixture()
        for name, test, code, collection_error in (
            ("already-green", self.UNIT_TEST.replace("self.assertEqual(result, 2)", "self.assertEqual(result, 1)"),
             0, False),
            ("import-error", "raise ImportError('fixture')\n", 1, True),
            ("syntax-error", "def invalid(\n", 1, True),
            ("no-tests", "import unittest\n", 0, True),
            ("runtime-error", self.UNIT_TEST.replace("self.assertEqual(result, 2)", "raise RuntimeError('fixture')"),
             1, True),
        ):
            with self.subTest(case=name):
                self.out = self.temp / name
                overlay = self.temp / f"overlay-{name}"
                self._write(overlay, "tests/test_app.py", test)
                summary = self._implement("--tdd", "--tdd-test-dir", overlay,
                                          "--verify-cmd", json.dumps(self.UNIT_ARGV), exit_code=4)
                self.assertEqual((summary["status"], summary["exit_code"]), ("tdd_red_failed", 4))
                self.assertFalse((self.out / "codex-argv.json").exists())
                self.assertIsNone(summary["codex_exit_code"])
                proof = self._read_json(self.out / "verification.json")
                self.assertIs(proof["red_ok"], False)
                self.assertEqual(proof["red"]["exit_code"], code)
                self.assertIs(proof["red"]["outcome"]["collection_error"], collection_error)
                self.assertIsNone(proof["green"])

    def test_tdd_rejects_tampering_with_frozen_overlay(self):
        self._unit_fixture(test=self.UNIT_TEST)
        overlay = self.temp / "overlay"
        # Overlay entries already identical to HEAD are frozen too.
        self._write(overlay, "tests/test_app.py", self.UNIT_TEST)
        self._stub_action('''\
            test = target / 'tests/test_app.py'
            test.write_text(test.read_text().replace('self.assertEqual(result, 2)', 'self.assertEqual(result, 1)'))
        ''')
        summary = self._implement("--tdd", "--tdd-test-dir", overlay,
                                  "--verify-cmd", json.dumps(self.UNIT_ARGV), exit_code=5)
        self.assertEqual((summary["status"], summary["exit_code"]), ("protected_path_violation", 5))
        self.assertEqual(summary["path_policy"]["protected_violations"], ["tests/test_app.py"])
        self.assertIs(summary["worktree_preserved"], True)
        self.assertIn("self.assertEqual(result, 1)", (Path(summary["worktree_dir"]) / "tests/test_app.py").read_text())
        self.assertEqual((self.repo / "tests/test_app.py").read_text(encoding="utf-8"), self.UNIT_TEST)
        proof = self._read_json(self.out / "verification.json")
        self.assertIs(proof["red_ok"], True)
        self.assertIsNone(proof["green"])

    def test_tdd_protects_frozen_overlay_path_with_brackets(self):
        self._unit_fixture(test=self.UNIT_TEST)
        overlay = self.temp / "overlay"
        frozen_file = "tests/[id]_test.py"
        self._write(overlay, frozen_file, "# frozen test fixture\n")
        self._stub_action(f"(target / {frozen_file!r}).write_text('# changed by worker\\n')\n")
        summary = self._implement("--tdd", "--tdd-test-dir", overlay, "--tdd-test-path", "tests/test_app.py",
                                  "--verify-cmd", json.dumps(self.UNIT_ARGV), exit_code=5)
        self.assertEqual(summary["status"], "protected_path_violation")
        self.assertEqual(summary["path_policy"]["protected_violations"], [frozen_file])
        self.assertEqual(summary["tdd"]["protected_test_exact"], [frozen_file])
        self.assertEqual(summary["tdd"]["protected_test_globs"], ["tests/test_app.py"])
        self.assertEqual(summary["tdd"]["protected_test_paths"], [frozen_file, "tests/test_app.py"])

    def test_tdd_green_requires_reported_test_count(self):
        self._unit_fixture(test=self.UNIT_TEST)
        self._implement_value(2)
        argv = ["python3", "-c", dedent('''\
            import unittest
            from src.app import value

            if value() == 1:
                unittest.main(module=None, argv=["unittest", "discover", "-s", "tests"])
        ''')]
        summary = self._implement("--tdd", "--tdd-test-path", "tests/**",
                                  "--verify-cmd", json.dumps(argv), exit_code=6)
        self.assertEqual(summary["status"], "verification_failed")
        proof = self._read_json(self.out / "verification.json")
        self.assertIs(proof["red_ok"], True)
        self.assertIs(proof["green_ok"], False)
        self.assertEqual(proof["green"]["exit_code"], 0)
        self.assertIsNone(proof["green"]["outcome"]["collected"])

    def test_tdd_skipped_green_requires_opt_in(self):
        self._unit_fixture(test=self.UNIT_TEST)
        self._implement_value(None)
        for allow in (False, True):
            with self.subTest(allow_skips=allow):
                self.out = self.temp / f"allow-skips-{allow}"
                flags = ("--tdd-allow-skips",) if allow else ()
                summary = self._implement("--tdd", "--tdd-test-path", "tests/**",
                                          "--verify-cmd", json.dumps(self.UNIT_ARGV), *flags,
                                          exit_code=0 if allow else 6)
                self.assertEqual(summary["status"], "ok" if allow else "verification_failed")
                proof = self._read_json(self.out / "verification.json")
                self.assertIs(proof["red_ok"], True)
                self.assertIs(proof["green_ok"], allow)
                self.assertEqual(proof["green"]["exit_code"], 0)
                self.assertEqual(proof["green"]["outcome"]["skipped"], 1)

    def test_tdd_overlay_true_red_then_reconstructed_green(self):
        self._unit_fixture()
        overlay = self.temp / "overlay"
        self._write(overlay, "tests/test_app.py", self.UNIT_TEST)
        self._implement_value(2)
        summary = self._implement("--tdd", "--tdd-test-dir", overlay, "--verify-cmd", json.dumps(self.UNIT_ARGV))
        self.assertEqual((summary["status"], summary["exit_code"]), ("ok", 0))
        proof = self._read_json(self.out / "verification.json")
        self.assertIs(proof["red_ok"], True)
        self.assertIs(proof["green_ok"], True)
        self.assertIn("AssertionError: 1 != 2", proof["red"]["stderr_tail"])
        self.assertEqual(proof["red"]["base_oid"], summary["tdd"]["test_snapshot_commit"])
        self._assert_proof(proof["green"], self.UNIT_ARGV, 0)
        self.assertEqual(summary["tdd"]["impl_changed_files"], ["src/app.py"])
        self.assertFalse(Path(summary["worktree_dir"]).exists())
        self.assertEqual(self._git(self.repo, "worktree", "list", "--porcelain").count(b"worktree "), 1)
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), b"")
        self._git(self.repo, "apply", self.out / "changes.patch")
        self.assertEqual((self.repo / "tests/test_app.py").read_text(encoding="utf-8"), self.UNIT_TEST)
        result = subprocess.run(self.UNIT_ARGV, cwd=self.repo, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_tdd_working_tree_files_are_staged_and_original_index_is_untouched(self):
        self._unit_fixture(test=self.UNIT_TEST.replace("self.assertEqual(result, 2)", "self.assertEqual(result, 1)"))
        self._write(self.repo, "tests/test_app.py", self.UNIT_TEST)
        self._write(self.repo, "tests/extra input.txt", "untracked test fixture\n")
        self._write(self.repo, "C.txt", "unrelated dirty change\n")
        status = self._git(self.repo, "status", "--porcelain")
        self._implement_value(2)
        summary = self._implement("--tdd", "--tdd-from-working-tree", "--tdd-test-path", "tests/**",
                                  "--verify-cmd", json.dumps(self.UNIT_ARGV))
        self.assertCountEqual(summary["patch"]["changed_files"], ["src/app.py", "tests/test_app.py", "tests/extra input.txt"])
        self.assertEqual(summary["tdd"]["impl_changed_files"], ["src/app.py"])
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), status)
        self.assertEqual(self._git(self.repo, "diff", "--cached", "--name-only"), b"")
        proof = self._read_json(self.out / "verification.json")
        self.assertIs(proof["red_ok"], True)
        self.assertIs(proof["green_ok"], True)

    def test_parallel_ownership_and_combined_verification(self):
        argv = ["python3", "-c", dedent('''\
            from pathlib import Path
            assert Path('A.txt').read_text() == 'alpha\\n'
            assert Path('B.txt').read_text() == 'beta\\n'
            print('both worker patches applied')
        ''')]
        self._write(self.repo, ".codex-orchestrator.json", json.dumps({"verify_commands": {"combined": argv}}))
        self._commit_fixture()
        self._stub_action('''\
            name = artifacts.name
            filename = {'alpha': 'A.txt', 'beta': 'B.txt'}[name]
            (target / filename).write_text(name + '\\n')
        ''')
        entries = [{"name": "alpha", "allowed_paths": ["A.txt"]}, {"name": "beta", "allowed_paths": ["B.txt"]}]
        failing = [argv[0], argv[1], argv[2] + "raise SystemExit(23)\n"]
        for flag, value, command, command_exit in (
            ("--verify-preset", "combined", argv, 0),
            ("--verify-cmd", json.dumps(failing), failing, 23),
        ):
            with self.subTest(flag=flag):
                self.out = self.temp / flag.removeprefix("--")
                summary = self._run_batch(entries, "--mode", "implement", flag, value,
                                          exit_code=6 if command_exit else 0)
                self.assertEqual(summary["status"], "verification_failed" if command_exit else "ok")
                self.assertIs(summary["ownership_table_injected"], True)
                self.assertIs(summary["verification_ok"], command_exit == 0)
                self.assertEqual((self.corpus / "spec.md").read_text(encoding="utf-8"), "Inspect the corpus.\n")
                for entry in entries:
                    effective = self.out / entry["name"] / "effective_spec.md"
                    text = effective.read_text(encoding="utf-8")
                    self.assertIn("## Parallel Ownership Table", text)
                    self.assertIn("| alpha | A.txt |", text)
                    self.assertIn("| beta | B.txt |", text)
                    self.assertIn(text, (effective.parent / "effective_prompt.md").read_text(encoding="utf-8"))
                proof = self._read_json(self.out / "verification.json")
                self._assert_proof(proof, command, command_exit)
                self.assertIn("both worker patches applied", proof["stdout_tail"])
                self.assertEqual(len(summary["merge_check"][0]["patches"]), 2)
                self.assertEqual(self._git(self.repo, "status", "--porcelain"), b"")

    def test_parallel_out_of_scope_edit_fails_and_preserves_worktree(self):
        self._stub_action("(target / 'B.txt').write_text('outside scope\\n')\n")
        entries = [{"name": "scoped", "mode": "implement", "allowed_paths": ["A.txt"]}]
        self._run_batch(entries, "--shared-dir", exit_code=2, error="require a managed Git worktree")
        summary = self._run_batch(entries, exit_code=5)
        task = summary["tasks"][0]
        self.assertEqual(summary["status"], "scope_violation")
        self.assertEqual((task["status"], task["exit_code"]), ("scope_violation", 5))
        self.assertEqual(task["allowed_violations"], ["B.txt"])
        worktree = Path(task["worktree_dir"])
        self.assertEqual((worktree / "B.txt").read_text(encoding="utf-8"), "outside scope\n")
        self.assertIn(os.fsencode(worktree), self._git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual((self.repo / "B.txt").read_text(encoding="utf-8"), "base B.txt\n")


if __name__ == "__main__":
    unittest.main()
