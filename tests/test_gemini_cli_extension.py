import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-orchestrator"
SCRIPTS = SKILL / "scripts"
GUARD = SCRIPTS / "hook_delegate_guard.py"
WORKSPACE_FILE = "/workspace/src/app.py"


def load_command(path):
    source = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(source)

    # The Python 3.9 fallback accepts the two basic string forms used here.
    escape = r'\\(?:[btnfr"\\]|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})'
    assignment = re.compile(
        r'[ \t]*(?P<key>description|prompt)[ \t]*=[ \t]*(?:'
        rf'"""(?P<multiline>(?:[^"\\\x00-\x08\x0b-\x1f\x7f]|"(?!"")|{escape})*)"""'
        rf'|"(?P<single>(?:[^"\\\x00-\x08\x0a-\x1f\x7f]|{escape})*)"'
        r')[ \t]*(?:#[^\n]*)?(?:\n|\Z)'
    )
    blank_line = re.compile(r'[ \t]*(?:#[^\n]*)?(?:\n|\Z)')
    fields = {}
    position = 0
    while position < len(source):
        blank = blank_line.match(source, position)
        if blank and blank.end() > position:
            position = blank.end()
            continue
        match = assignment.match(source, position)
        if match is None:
            raise ValueError(f"{path}: invalid TOML assignment at offset {position}")
        key = match["key"]
        value = match["multiline"] if key == "prompt" else match["single"]
        if value is None or key in fields:
            raise ValueError(f"{path}: invalid or duplicate {key}")
        fields[key] = value
        position = match.end()
    return fields


def load_frontmatter(path):
    # These headers are flat YAML mappings with plain scalars, block text,
    # and lists of plain tool names.
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path}: missing frontmatter delimiters")
    header = "\n".join(lines[1:lines.index("---", 1)])
    field = re.compile(
        r"(?m)^([a-z_][a-z0-9_]*):[ ]*(.*)(?:\n|$)((?:^[ ]+[^\n]*(?:\n|$))*)"
    )
    plain = r"[A-Za-z0-9_-]+"
    fields = {}
    position = 0
    for match in field.finditer(header):
        key, value, block = match.groups()
        if header[position:match.start()].strip() or key in fields:
            raise ValueError(f"{path}: invalid or duplicate frontmatter field {key}")
        position = match.end()
        if value in (">", ">-", ">+", "|", "|-", "|+") and block.strip():
            fields[key] = block
        elif not value and block:
            items = [re.fullmatch(rf"( +)- +({plain}) *", line) for line in block.splitlines()]
            if any(item is None for item in items) or len({item[1] for item in items}) != 1:
                raise ValueError(f"{path}: invalid block list for {key}")
            fields[key] = [item[2] for item in items]
        elif not block and re.fullmatch(rf"\[ *{plain}(?: *, *{plain})* *\]", value):
            fields[key] = [item.strip() for item in value[1:-1].split(",")]
        elif not block and re.fullmatch(plain, value):
            fields[key] = value
        else:
            raise ValueError(f"{path}: invalid frontmatter value for {key}")
    if header[position:].strip() or not fields:
        raise ValueError(f"{path}: invalid frontmatter mapping")
    return fields


class GeminiCliExtensionTests(unittest.TestCase):
    def setUp(self):
        session_dir = tempfile.TemporaryDirectory(prefix="test_sess_xyz_")
        self.addCleanup(session_dir.cleanup)
        self.session_id = Path(session_dir.name).name

    def tearDown(self):
        marker = Path("/tmp/codex-orchestrator") / f".gemini_cli_skip_{self.session_id}"
        marker.unlink(missing_ok=True)

    def test_manifest_and_hooks_json(self):
        manifest = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "codex-orchestrator")
        self.assertEqual(manifest["version"], "2.1.0")
        self.assertEqual(manifest["contextFileName"], "GEMINI.md")
        self.assertTrue((ROOT / manifest["contextFileName"]).is_file())

        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("BeforeAgent", "BeforeTool"):
            with self.subTest(event=event):
                commands = [
                    hook["command"]
                    for group in hooks[event]
                    for hook in group["hooks"]
                    if hook["type"] == "command"
                ]
                targets = []
                for command in commands:
                    match = re.fullmatch(r'python3\s+"(\$\{extensionPath\}/[^"]+)"', command)
                    if match:
                        targets.append(Path(match[1].replace("${extensionPath}", str(ROOT))))
                self.assertIn(GUARD, targets)
                self.assertTrue(GUARD.is_file())
                self.assertTrue(os.access(GUARD, os.X_OK), f"{GUARD} is not executable")

        schema = SKILL / "references" / "codex-output-schema.json"
        json.loads(schema.read_text(encoding="utf-8"))

    def test_slash_commands_toml(self):
        for name in ("impl", "review", "dag"):
            with self.subTest(command=name):
                fields = load_command(ROOT / "commands" / "codex" / f"{name}.toml")
                for field in ("description", "prompt"):
                    self.assertIsInstance(fields[field], str)
                    self.assertTrue(fields[field].strip(), f"{field} is empty")

    def test_agents_and_skill_frontmatter(self):
        agent = load_frontmatter(ROOT / "agents" / "codex-worker.md")
        self.assertEqual(agent["name"], "codex-worker")
        self.assertEqual(agent["kind"], "local")
        self.assertIsInstance(agent["tools"], list)
        self.assertIn("run_shell_command", agent["tools"])
        self.assertNotIn("replace", agent["tools"])

        skill = load_frontmatter(SKILL / "SKILL.md")
        self.assertEqual(skill["name"], "codex-orchestrator")

    def test_scripts_syntax_and_no_personal_paths(self):
        for name in ("run_codex.sh", "run_codex_parallel.sh"):
            with self.subTest(script=name):
                self.run_checked(["bash", "-n", str(SCRIPTS / name)])
        with tempfile.TemporaryDirectory() as cache:
            self.run_checked(
                ["python3", "-m", "py_compile", str(GUARD)],
                env={**os.environ, "PYTHONPYCACHEPREFIX": cache},
            )

        personal_path = b"".join((b"/Users/", b"mei"))
        matches = []

        def raise_walk_error(error):
            raise error

        for directory, dirs, files in os.walk(ROOT, onerror=raise_walk_error):
            if ".git" in dirs:
                dirs.remove(".git")
            for name in files:
                path = Path(directory) / name
                if name != ".git" and path.is_file() and personal_path in path.read_bytes():
                    matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(sorted(matches), [], "Files containing the personal home path")

    def run_checked(self, command, **kwargs):
        result = subprocess.run(
            command, capture_output=True, encoding="utf-8", timeout=10, **kwargs
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def assert_hook_decision(self, payload, expected):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        stdout = self.run_checked([sys.executable, str(GUARD)], input=raw)
        response = json.loads(stdout)
        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("decision"), expected, response)

    def before_tool(self, tool_name, file_path, **tool_input):
        return {
            "hook_event_name": "BeforeTool",
            "session_id": self.session_id,
            "cwd": "/workspace",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path, **tool_input},
        }

    def test_before_tool_write_file_workspace_py_denied(self):
        self.assert_hook_decision(
            self.before_tool("write_file", WORKSPACE_FILE, content="value = 1\n"), "deny"
        )

    def test_before_tool_replace_over_10_lines_denied(self):
        old = "\n".join(f"value_{i} = 0" for i in range(12))
        new = "\n".join(f"value_{i} = 1" for i in range(12))
        self.assert_hook_decision(
            self.before_tool("replace", WORKSPACE_FILE, old_string=old, new_string=new), "deny"
        )

    def test_before_tool_replace_small_edit_allowed(self):
        old = [f"value_{i} = 0" for i in range(25)]
        new = old.copy()
        new[11:13] = ["value_11 = 1", "value_12 = 1"]
        self.assert_hook_decision(
            self.before_tool(
                "replace", WORKSPACE_FILE, old_string="\n".join(old), new_string="\n".join(new)
            ),
            "allow",
        )

    def test_before_tool_tmp_and_non_code_relative_allowed(self):
        for path in ("/tmp/codex-orchestrator/test/helper.py", "docs/README.md"):
            with self.subTest(path=path):
                self.assert_hook_decision(self.before_tool("write_file", path, content="x"), "allow")

    def test_before_agent_per_turn_opt_out_and_reset(self):
        agent = {"hook_event_name": "BeforeAgent", "session_id": self.session_id}
        write = self.before_tool("write_file", WORKSPACE_FILE, content="value = 1\n")
        self.assert_hook_decision({**agent, "prompt": "不用codex，你直接写"}, "allow")
        self.assert_hook_decision(write, "allow")
        self.assert_hook_decision({**agent, "prompt": "继续正常开发"}, "allow")
        self.assert_hook_decision(write, "deny")

    def test_antigravity_pre_tool_use_compat(self):
        payload = {"toolCall": {"name": "write_file", "args": {"Path": "/workspace/foo.py"}}}
        self.assert_hook_decision(payload, "deny")
        self.assert_hook_decision({**payload, "lastUserInput": "skip codex"}, "allow")

    def test_malformed_json_fails_open(self):
        self.assert_hook_decision("{not-json", "allow")


if __name__ == "__main__":
    unittest.main()
