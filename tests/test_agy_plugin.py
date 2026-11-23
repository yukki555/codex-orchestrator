"""Verify the native Antigravity CLI plugin using only the standard library."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
CORE_SKILL = SKILLS / "codex-orchestrator"
SCRIPTS = CORE_SKILL / "scripts"
GUARD = SCRIPTS / "hook_delegate_guard.py"
RULES = ROOT / "rules" / "AGENTS.md"
AGENT = ROOT / "agents" / "codex-worker.md"
SKILL_NAMES = ("codex-orchestrator", "impl", "review", "dag")


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

    def test_plugin_hooks_and_schema(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertIsInstance(plugin, dict)
        self.assertEqual(plugin["name"], "codex-orchestrator")
        self.assertEqual(plugin["version"], "2.1.0")

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

        schema_path = CORE_SKILL / "references" / "codex-output-schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = {
            "verdict", "summary", "root_cause_or_design", "changes",
            "evidence", "verification", "risks_and_decisions",
        }
        self.assertIsInstance(schema["required"], list)
        self.assertEqual(required - set(schema["required"]), set())

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

    def test_four_skills_valid_and_no_stale_templates(self):
        skill_files = [SKILLS / name / "SKILL.md" for name in SKILL_NAMES]
        for path in skill_files:
            with self.subTest(frontmatter=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), path)
                skill = self._frontmatter(path)
                self.assertEqual(skill["name"], path.parent.name)
                self.assertIsInstance(skill["description"], str)
                self.assertTrue(skill["description"].strip(), path)

        for path in [*skill_files, RULES, AGENT, ROOT / "README.md"]:
            with self.subTest(stale_syntax=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                for marker in ("{{args}}", "!{", "~/.gemini/extensions/"):
                    self.assertNotIn(marker, content, f"{path} contains {marker!r}")

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
            if ".git" in subdirectories:
                subdirectories.remove(".git")
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
                tool_call("write_file", "/Users/example/.gemini/config/plugin.json"),
                "allow",
            ),
            ("documentation write", tool_call("write_file", "/workspace/docs/README.md"), "allow"),
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


if __name__ == "__main__":
    unittest.main()
