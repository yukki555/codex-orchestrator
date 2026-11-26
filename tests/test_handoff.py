"""Exercise context handoff through local fixtures and a mock ConversationService."""

import errno
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from codex_orch import context_telemetry as telemetry
from codex_orch import handoff
from codex_orch.context_hook import evaluate_context_hook
from codex_orch.env import build_child_env, redact_secrets
from codex_orch.host_adapter import is_safe_temp_or_config_path
from codex_orch.profiles import DEFAULT_CONTEXT_HANDOFF, load_profiles, resolve_context_handoff_config
sys.path.pop(0)

SERVICE = "/product.v1.ConversationService/"
MODEL = "claude-opus-5-5-high"
MOCK_CSRF = "fixture-secret-for-conversation-rpc"
SECTION_NAMES = (
    "Goal & Constraints", "Immediate Next Atomic Action", "Progress",
    "Decisions", "Verification", "Active Working Set", "Artifacts", "Pending Decisions",
)


def planner(tokens, *, index=1, model=MODEL, cache=0, output=0, calls=None):
    row = {
        "type": "PLANNER_RESPONSE", "stepIndex": index, "model": model,
        "usage": {"inputTokens": tokens, "cacheReadTokens": cache, "outputTokens": output},
    }
    if calls is not None:
        row["tool_calls"] = calls
    return row


def tool_call(name, **args):
    return {"name": name, "args": args}


def checkpoint(index):
    return {"type": "CHECKPOINT", "stepIndex": index}


def subagent_bootstrap():
    return {
        "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "step_index": 0,
        "content": "[Message] timestamp=2026-01-01T00:00:00Z sender=123e4567-e89b-12d3-a456-426614174000",
    }


class ServiceHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers, body))
        status, result = self.server.dispatch(self.path, body)
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if status == 302:
            self.send_header("Location", result["location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.calls.append((self.path, self.headers, {}))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        pass


class ConversationServer(ThreadingHTTPServer):
    def __init__(self, dispatch):
        super().__init__(("127.0.0.1", 0), ServiceHandler, bind_and_activate=False)
        self.dispatch = dispatch
        self.calls = []
        self.uses_socketpair = False

    def open(self, test):
        test.addCleanup(self.server_close)
        try:
            self.server_bind()
            self.server_activate()
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES):
                raise
            # Restricted runners can still run the HTTP client and server over a socket pair.
            self.uses_socketpair = True
            self.server_address = ("127.0.0.1", 43123)
            original_connect = http.client.HTTPConnection.connect

            def connect(connection):
                if (connection.host, connection.port) != self.server_address:
                    return original_connect(connection)
                client, server = socket.socketpair()
                client.settimeout(connection.timeout)
                connection.sock = client
                self.process_request(server, self.server_address)

            patch = mock.patch.object(http.client.HTTPConnection, "connect", connect)
            patch.start()
            test.addCleanup(patch.stop)
        else:
            thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            test.addCleanup(thread.join, 2)
            test.addCleanup(self.shutdown)
        return "http://%s:%d" % self.server_address


class HandoffFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-handoff-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = "test" + uuid.uuid4().hex
        self.chain = "chain-" + self.cid
        self.pointer = Path("/tmp/codex-orchestrator/handoffs") / self.chain / "latest.json"
        self.addCleanup(shutil.rmtree, self.pointer.parent, ignore_errors=True)
        self.app = self.root / "app"
        self.transcript = self.app / "brain" / self.cid / "transcript.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.packet = self.root / "draft.md"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.environ = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("ANTIGRAVITY_", "CODEX_HANDOFF_"))
            and key not in ("CODEX_ORCH_PROFILES_FILE", "CODEX_NOISE_NUDGE")
        }
        self.environ.update({
            "PLUGIN_DATA": str(self.root / "plugin"),
            "ANTIGRAVITY_APP_DATA_DIR": str(self.app),
            "ANTIGRAVITY_CONVERSATION_ID": self.cid,
            "ANTIGRAVITY_CSRF_TOKEN": MOCK_CSRF,
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })

    def write_transcript(self, *rows):
        self.transcript.write_text(
            "".join(row + "\n" if isinstance(row, str) else json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def packet_text(self, *, omit=None, **replacements):
        return "# Session Handoff Packet\n\n" + "\n\n".join(
            f"## {n}. {name}\n\n" + replacements.get(
                f"section{n}", f"Record the verified implementation state and deterministic evidence for section {n}."
            )
            for n, name in enumerate(SECTION_NAMES, 1) if n != omit
        )

    def invoke(self, command, *args, exit_code=0, shim=False, extra_env=None):
        environ = {**self.environ, **(extra_env or {})}
        argv = [command, *map(str, args)]
        if shim:
            result = subprocess.run(
                ["bash", str(SCRIPTS / "handoff.sh"), *argv], capture_output=True,
                text=True, encoding="utf-8", env=environ, cwd=self.root, timeout=10,
            )
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        else:
            with mock.patch.dict(os.environ, environ, clear=True), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = handoff.main(argv)
            stdout, stderr = out.getvalue(), err.getvalue()
        self.assertNotIn(MOCK_CSRF, stdout + stderr)
        self.assertEqual(code, exit_code, stdout + stderr)
        result = json.loads(stdout)
        self.assertEqual(result["exit_code"], code)
        return result

    def create(self, *args, **kwargs):
        self.packet.write_text(self.packet_text(), encoding="utf-8")
        return self.invoke("create", "--packet", self.packet, *args, **kwargs)

    def git(self, *args, input_data=None):
        result = subprocess.run(
            ["git", "-C", str(self.workspace), *args], input=input_data,
            capture_output=True, text=True, encoding="utf-8", env=self.environ, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip("\n")

    def init_git(self):
        self.git("init", "-q")
        file = self.workspace / "tracked.txt"
        file.write_text("baseline\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("-c", "user.name=Test Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "baseline")
        return file

    def state(self):
        return telemetry.load_conversation_state(self.cid, environ=self.environ)

    def save_state(self, **values):
        telemetry.save_conversation_state(self.cid, values, environ=self.environ)

    def start_service(self, dispatch):
        server = ConversationServer(dispatch)
        self.environ["ANTIGRAVITY_LS_ADDRESS"] = server.open(self)
        self.service_uses_socketpair = server.uses_socketpair
        # Requests would hit an unusable proxy if either RPC client honored proxy settings.
        self.environ.update(http_proxy="http://127.0.0.1:1", no_proxy="")
        return server.calls

    def assert_rpc_headers(self, calls):
        self.assertTrue(calls)
        for path, headers, _ in calls:
            with self.subTest(path=path):
                self.assertEqual(headers.get("Content-Type"), "application/json")
                self.assertEqual(headers.get("Connect-Protocol-Version"), "1")
                self.assertEqual(headers.get("x-antigravity-csrf-token"), MOCK_CSRF)

    def hook(self, event="PreInvocation", **fields):
        payload = {
            "hookEventName": event, "conversationId": self.cid,
            "transcriptPath": str(self.transcript), "modelName": MODEL,
        }
        if event == "PreInvocation":
            payload.update(lastUserInput="继续当前任务")
        else:
            payload.update(terminationReason="NO_TOOL_CALL", fullyIdle=True, continuationActive=False)
        payload.update(fields)
        with mock.patch.dict(os.environ, self.environ, clear=True):
            return evaluate_context_hook(payload, environ=self.environ)


class TelemetryTests(HandoffFixture):
    def test_fast_skip_keeps_unicode_escaped_early_rows(self):
        escaped_checkpoint = json.dumps(checkpoint(2)).replace("CHECKPOINT", r"\u0043HECKPOINT")
        escaped_planner = json.dumps(planner(1000, calls=[tool_call("read_file", Path="/fragment")]))
        escaped_planner = escaped_planner.replace("PLANNER_RESPONSE", r"\u0050LANNER_RESPONSE")
        self.write_transcript(planner(500, calls=[tool_call("read_file", Path="/old")]),
                              escaped_checkpoint, escaped_planner, planner(26000))
        result = telemetry.read_transcript_telemetry(self.transcript, tail_bytes=1)
        self.assertEqual(result.checkpoint_epoch, 1)
        self.assertEqual(result.read_file_count, 1)
        self.assertTrue(result.last_turn_had_read_file)
        self.assertEqual(result.last_turn_delta_tokens, 25000)

    def test_read_hygiene_counts_bytes_duplicates_and_plugin_docs_per_epoch(self):
        path = self.root / "data.txt"
        path.write_bytes(b"x" * 100)
        calls = [
            tool_call("read_file", Path=str(path), StartLine=1),
            tool_call("read_file", file_path=str(path.parent) + "/./data.txt", start_line=2, end_line=4),
            tool_call("read_file", AbsolutePath=str(path), offset=1, limit=2),
            tool_call("read_file", path="/work/.gemini/config/plugins/example/rules/AGENTS.md"),
            tool_call("read_file", path="/work/.gemini/antigravity/builtin/skills/a/SKILL.md"),
            tool_call("read_file", path="/work/codex-orchestrator/skills/a/references/example.md"),
            tool_call("read_file", path="/work/handoff/packet.md"),
            tool_call("read_file", path="/tmp/codex-orchestrator/draft.md"),
            tool_call("invoke_subagent", TypeName="rEsEaRcH"),
            tool_call("read_file", path="/missing.txt"),
        ]
        rows = (planner(500, cache=500, calls=calls), planner(16000, cache=10000))
        self.write_transcript(*rows)
        result = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual(result.read_file_count, 4)
        self.assertEqual(result.read_file_unbounded_count, 2)
        self.assertEqual(result.read_file_duplicate_count, 2)
        self.assertEqual(result.read_file_est_bytes, 400)
        self.assertEqual(result.plugin_doc_read_count, 3)
        self.assertEqual(result.last_turn_delta_tokens, 25000)
        self.assertTrue(result.last_turn_had_read_file)
        self.assertEqual(result.reads_since_last_research, 1)
        self.write_transcript(*rows, checkpoint(3), planner(900, calls=calls[:1]))
        result = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual(result.read_file_count, 1)
        self.assertEqual(result.read_file_unbounded_count, 1)
        self.assertEqual(result.read_file_duplicate_count, 0)
        self.assertEqual(result.read_file_est_bytes, 100)
        self.assertEqual(result.plugin_doc_read_count, 0)
        self.assertEqual(result.last_turn_delta_tokens, 0)
        self.assertFalse(result.last_turn_had_read_file)
        self.assertEqual(result.reads_since_last_research, 1)
        self.assertEqual(result.invoked_subagents, ())

    def setUp(self):
        super().setUp()
        self.config = resolve_context_handoff_config(load_profiles(), environ=self.environ)

    def assessment(self):
        return telemetry.evaluate_context_band(telemetry.read_transcript_telemetry(self.transcript), self.config)

    def test_handoff_state_dir_resolution(self):
        for environ, expected in (
            ({"PLUGIN_DATA": " ~/plugin ", "ANTIGRAVITY_APP_DATA_DIR": str(self.app)},
             self.root / "plugin" / "handoff_state"),
            ({"PLUGIN_DATA": " ", "ANTIGRAVITY_APP_DATA_DIR": " ~/app "},
             self.app / "plugin_data" / "codex-orchestrator" / "handoff_state"),
            ({}, Path("/tmp/codex-orchestrator/handoff-state")),
        ):
            with self.subTest(environ=environ), mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
                    mock.patch.object(telemetry, "ensure_private_dir", side_effect=lambda path: path) as ensure:
                self.assertEqual(telemetry.handoff_state_dir(environ), expected)
                ensure.assert_called_once_with(expected)

    def test_telemetry_preserves_planner_tokens_behind_large_tool_output(self):
        response = planner(320000, model="gemini-4-hydrogen-high")
        tool_output = {"type": "GENERIC", "stepIndex": 2, "output": "x" * (512 * 1024)}
        self.write_transcript(response, tool_output)
        with mock.patch.object(telemetry, "_parse_transcript_row",
                               wraps=telemetry._parse_transcript_row) as decode:
            result = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual(result.active_tokens, 320000)
        self.assertEqual(result.active_model, response["model"])
        self.assertEqual(result.recent_models, (response["model"],))
        self.assertEqual(result.last_planner_step_index, 1)
        self.assertEqual(telemetry.evaluate_context_band(result, self.config).band, "hard")
        self.assertEqual(decode.call_count, 1)

    def test_context_limits_and_threshold_boundaries(self):
        for model, limit, soft, hard, emergency in (
            ("gemini-4-hydrogen-high", 512000, 200000, 300000, 409600),
            (MODEL, 256000, 115200, 166400, 204800),
            ("claude-sonnet-4-6", 160000, 72000, 104000, 128000),
            ("gemini-3-1-pro-high", 128000, 57600, 83200, 102400),
            ("gpt-oss-120b", 80000, 36000, 52000, 64000),
            ("unrecognized-model", 160000, 72000, 104000, 128000),
        ):
            for tokens, band in ((soft - 1, "ok"), (soft, "soft"), (hard - 1, "soft"),
                                 (hard, "hard"), (emergency - 1, "hard"), (emergency, "emergency")):
                with self.subTest(model=model, tokens=tokens):
                    self.write_transcript(planner(tokens, model=model))
                    value = self.assessment()
                    self.assertEqual((value.context_limit, value.soft_threshold, value.hard_threshold,
                                      value.emergency_threshold), (limit, soft, hard, emergency))
                    self.assertEqual(value.band, band)

    def test_dual_model_orchestrator_uses_lower_limit(self):
        first = "gemini-4-hydrogen-high"
        for models in ((first, MODEL), (MODEL, first)):
            with self.subTest(models=models):
                self.write_transcript(planner(120000, model=models[0]), planner(125000, model=models[1], index=2))
                value = self.assessment()
                self.assertEqual(value.recent_models, models)
                self.assertEqual(value.active_model, models[-1])
                self.assertEqual((value.context_limit, value.band), (256000, "soft"))

    def test_checkpoint_resets_active_usage_and_tracks_new_planner(self):
        first = planner(170000, cache=1000, output=800)
        self.write_transcript(first, checkpoint(2))
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual((value.checkpoint_epoch, value.active_tokens, value.output_tokens), (1, 0, 0))
        self.assertEqual(value.recent_models, ())
        self.assertIsNone(value.last_planner_step_index)
        self.assertFalse(self.assessment().checkpoint_fatigue)
        last = planner(23000, index=4, cache=3000, output=410, model="gpt-oss-120b")
        self.write_transcript(first, checkpoint(2), checkpoint(3), last)
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual((value.active_tokens, value.input_tokens, value.cache_read_tokens,
                          value.output_tokens), (26000, 23000, 3000, 410))
        self.assertEqual((value.checkpoint_count, value.checkpoint_epoch, value.last_step_index,
                          value.last_planner_step_index), (2, 2, 4, 4))
        self.assertEqual(value.recent_models, ("gpt-oss-120b",))
        self.assertTrue(self.assessment().checkpoint_fatigue)

    def test_subagent_bootstrap_outside_tail_persists_across_compaction(self):
        work = planner(100, calls=[
            tool_call("run_command", command="ssh host"),
            tool_call("read_file", Path="/workspace/item"),
        ])
        rows = (" ", subagent_bootstrap(), {"type": "USER_INPUT", "content": "x" * 262144}, work)
        self.write_transcript(*rows)
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertIs(value.is_subagent, True)
        self.assertEqual((value.remote_cmd_count, value.remote_turn_streak, value.read_file_count), (1, 1, 1))
        self.write_transcript(*rows, checkpoint(2), checkpoint(3), planner(100, index=4))
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertIs(value.is_subagent, True)
        self.assertEqual(value.checkpoint_epoch, 2)
        self.assertEqual((value.remote_cmd_count, value.remote_turn_streak, value.read_file_count), (0, 0, 0))

    def test_only_first_nonblank_row_can_mark_subagent(self):
        bootstrap = subagent_bootstrap()
        cases = (
            ({"type": "USER_INPUT", "step_index": 0}, dict(bootstrap, step_index=1)),
            (planner(100, index=0), bootstrap),
            ("bad json", bootstrap),
            (dict(bootstrap, content="[Message] timestamp=2026-01-01T00:00:00Z"),),
            (dict(bootstrap, step_index=1),),
            (dict(bootstrap, step_index=False),),
            (dict(bootstrap, source="USER"),),
        )
        for rows in cases:
            with self.subTest(rows=rows):
                self.write_transcript(*rows)
                self.assertIs(telemetry.read_transcript_telemetry(self.transcript).is_subagent, False)

    def test_malformed_truncated_and_missing_transcripts_fail_open(self):
        self.transcript.write_bytes(
            b"bad json\n[]\n" + json.dumps(planner(1234)).encode() + b'\n{"type":"PLANNER'
        )
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertTrue(value.exists)
        self.assertEqual((value.active_tokens, value.malformed_lines), (1234, 3))
        self.transcript.unlink()
        value = telemetry.read_transcript_telemetry(self.transcript)
        self.assertFalse(value.exists)
        self.assertEqual((value.active_tokens, value.malformed_lines, self.assessment().band), (0, 0, "ok"))

    def test_tool_activity_decodes_quoted_args_and_resets_on_checkpoint(self):
        commands = (
            "ssh host", "scp host:file .", "rsync file host:", "sbatch job.sh", "squeue",
            "kubectl exec workload -- date", "docker exec service date",
        )
        remote_turns = []
        for index, command in enumerate(commands):
            call = (tool_call("run_command", CommandLine=json.dumps(command)) if index % 2 == 0
                    else tool_call("run_command", command=command))
            remote_turns.append(planner(100, calls=[call]))
        remote_turns.extend((
            planner(100, calls=[tool_call("manage_task", Action=json.dumps(" STATUS "))]),
            planner(100, calls=[]),
            planner(100, calls=[None, "malformed"]),
            planner(100),
        ))
        self.write_transcript(*remote_turns)
        activity = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual((activity.remote_cmd_count, activity.remote_turn_streak), (8, 8))

        excluded_commands = [
            tool_call("run_command", CommandLine=json.dumps(f'./{script} "ssh host"'))
            for script in ("run_codex.sh", "run_codex_parallel.sh", "handoff.sh")
        ]
        non_remote = planner(100, calls=[
            *excluded_commands,
            tool_call("manage_task", action="list"),
            tool_call("read_file", Path=json.dumps("/workspace/source.txt")),
            tool_call("read_file", path="/workspace/other.txt"),
            tool_call("read_file", AbsolutePath=json.dumps("/workspace/handoff/packet.md")),
            tool_call("read_file", Path=r"C:\workspace\handoff"),
            tool_call("read_file", Path="/tmp/codex-orchestrator/work/log"),
            tool_call("read_file", Path="/private/tmp/codex-orchestrator/work/log"),
            tool_call("invoke_subagent", TypeName=json.dumps(" Remote-Runner ")),
            tool_call("invoke_subagent", type_name="RESEARCH"),
            tool_call("invoke_subagent", typeName="remote-runner"),
        ])
        non_remote["toolCalls"] = non_remote.pop("tool_calls")
        self.write_transcript(*remote_turns, non_remote)
        activity = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual((activity.remote_cmd_count, activity.remote_turn_streak), (8, 0))
        self.assertEqual(activity.read_file_count, 2)
        self.assertEqual(activity.invoked_subagents, ("remote-runner", "research"))

        self.write_transcript(*remote_turns, non_remote, remote_turns[0], checkpoint(2))
        activity = telemetry.read_transcript_telemetry(self.transcript)
        self.assertEqual((activity.remote_cmd_count, activity.remote_turn_streak, activity.read_file_count), (0, 0, 0))
        self.assertEqual(activity.invoked_subagents, ())

    def test_remote_activity_uses_shell_command_position(self):
        for expected, commands in (
            (0, (
                "cat ~/.ssh/config",
                "rg ssh README.md",
                "kubectl get pods -l app=exec",
                "docker ps --filter name=exec",
                'bash /path/run_codex.sh implement --prompt "ssh host && scp file host:"',
                'zsh -x /path/run_codex_parallel.sh "ssh host"',
                'sh /path/handoff.sh create --packet "ssh"',
                'echo "cd /tmp && ssh host"',
                "kubectl --namespace exec get pods",
                "docker --host exec ps",
                "docker -- exec ctr",
                'rg ssh "unterminated',
            )),
            (1, (
                "kubectl -n training exec worker -- date",
                "kubectl --namespace=training --context prod exec -it pod -- bash",
                "docker -H tcp://host:2375 exec -it ctr bash",
                "cd /tmp && /usr/bin/ssh host date",
                "FOO=1 timeout 30 rsync -avz a b",
                "if command true; then time ! nohup ssh host; fi",
                "pwd\nenv -i -u LANG -C /tmp -S ignored FOO=1 sudo -u user BAR=2 scp a host:",
                "timeout --kill-after 1 -s TERM 30 nice -n 5 stdbuf -o L squeue",
                "nice -5 sbatch job.sh",
                "kubectl --request-timeout=10s --as exec exec pod -- date",
                'bash /path/run_codex.sh --prompt "ssh host"; ssh host',
                'cd /tmp && ssh host "unterminated',
            )),
        ):
            for command in commands:
                with self.subTest(command=command):
                    self.write_transcript(planner(100, calls=[tool_call("run_command", CommandLine=command)]))
                    activity = telemetry.read_transcript_telemetry(self.transcript)
                    self.assertEqual((activity.remote_cmd_count, activity.remote_turn_streak),
                                     (expected, expected))

    def test_chain_mode_tags_accept_supported_modes_only(self):
        for mode in ("auto", "propose", "off", "foo"):
            with self.subTest(mode=mode):
                expected = {"chain_id": "chain-1", "hop_index": 2}
                if mode in ("auto", "propose"):
                    expected["chain_handoff_mode"] = mode
                self.assertEqual(telemetry.chain_position_from_tags([
                    "chain:chain-1", "hop:2", f"handoff-mode:{mode}",
                ]), expected)

    def test_noise_profile_validation_and_legacy_defaults(self):
        config_file = self.root / "profiles.json"
        profiles = load_profiles()
        context = profiles["context_handoff"]
        noise = context.pop("noise_nudge")
        self.assertEqual(noise, DEFAULT_CONTEXT_HANDOFF["noise_nudge"])
        self.assertEqual(noise, {
            "enabled": True, "remote_cmd_threshold": 4, "remote_streak_threshold": 3,
            "read_file_threshold": 6, "read_file_rearm_delta": 6,
            "read_file_min_tokens": 45000, "read_file_min_ratio": 0.12, "read_spike_tokens": 25000,
        })
        config_file.write_text(json.dumps(profiles), encoding="utf-8")
        legacy = load_profiles(config_file)
        self.assertEqual(resolve_context_handoff_config(legacy, environ={})["noise_nudge"], noise)
        invalid_values = {
            "enabled": 1, "remote_cmd_threshold": 0, "remote_streak_threshold": True,
            "read_file_threshold": -1, "read_file_min_tokens": "45000",
            "read_file_rearm_delta": 0, "read_file_min_ratio": 1.01, "read_spike_tokens": True,
        }
        invalid_overrides = [None, []]
        invalid_overrides.extend({**noise, key: value} for key, value in invalid_values.items())
        for override in invalid_overrides:
            with self.subTest(override=override):
                context["noise_nudge"] = override
                config_file.write_text(json.dumps(profiles), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "context_handoff.noise_nudge"):
                    load_profiles(config_file)
        context["noise_nudge"] = {"enabled": False}
        config_file.write_text(json.dumps(profiles), encoding="utf-8")
        config = resolve_context_handoff_config(load_profiles(config_file), environ={})
        self.assertEqual(config["noise_nudge"], {**noise, "enabled": False})

    def test_noise_overrides_and_explicit_mode(self):
        for value, enabled in (
            ("on", True), ("off", False), ("1", True), ("0", False), ("true", True),
            ("false", False), (" YeS ", True), (" nO ", False),
        ):
            with self.subTest(value=value):
                config = resolve_context_handoff_config(
                    {"context_handoff": {"noise_nudge": {"enabled": not enabled}}},
                    environ={"CODEX_NOISE_NUDGE": value},
                )
                self.assertIs(config["noise_nudge"]["enabled"], enabled)
        config = resolve_context_handoff_config(environ={"CODEX_NOISE_NUDGE": "invalid"})
        self.assertIs(config["noise_nudge"]["enabled"], True)
        for raw_mode, mode, explicit in (
            ("", "propose", False), ("invalid", "propose", False), (" AuTo ", "auto", True),
            ("propose", "propose", True), ("off", "off", True),
        ):
            with self.subTest(raw_mode=raw_mode):
                config = resolve_context_handoff_config(environ={"CODEX_HANDOFF_MODE": raw_mode})
                self.assertEqual(config["mode"], mode)
                self.assertIs(config["mode_explicit"], explicit)

    def test_environment_overrides(self):
        for mode in (" AuTo ", " PrOpOsE ", " OfF "):
            with self.subTest(mode=mode):
                cfg = resolve_context_handoff_config(load_profiles(), environ={
                    "CODEX_HANDOFF_MODE": mode, "CODEX_HANDOFF_SOFT_TOKENS": "900",
                    "CODEX_HANDOFF_HARD_TOKENS": "1000", "CODEX_HANDOFF_MAX_HOPS": "3",
                    "CODEX_HANDOFF_ARCHIVE_OLD": " YeS ",
                })
                self.assertEqual(cfg["mode"], mode.strip().lower())
                self.assertEqual(cfg["max_hops"], 3)
                self.assertTrue(cfg["archive_old"])
                self.write_transcript(planner(1000))
                value = telemetry.evaluate_context_band(telemetry.read_transcript_telemetry(self.transcript), cfg)
                self.assertEqual((value.soft_threshold, value.hard_threshold, value.band), (900, 1000, "hard"))

    def test_redaction_and_child_environment(self):
        for secret in (f"x-antigravity-csrf-token: {MOCK_CSRF}", f'ANTIGRAVITY_CSRF_TOKEN="{MOCK_CSRF}"'):
            with self.subTest(prefix=secret.split(MOCK_CSRF)[0]):
                self.assertEqual(redact_secrets(f"prefix {secret}\nsuffix"), "prefix [REDACTED]\nsuffix")
        baseline = {"PATH": "/bin", "ANTIGRAVITY_CSRF_TOKEN": MOCK_CSRF, "ANTIGRAVITY_LS_ADDRESS": "example.invalid"}
        for inherit_all in (True, False):
            self.assertEqual(build_child_env(baseline, inherit_all=inherit_all), {"PATH": "/bin"})

    def test_brain_paths_allowed_for_all_hosts(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "ANTIGRAVITY_APP_DATA_DIR": str(self.app)}):
            for directory in (str(self.app), "~/.gemini/antigravity-cli", "~/.gemini/antigravity-ide"):
                with self.subTest(directory=directory):
                    self.assertTrue(is_safe_temp_or_config_path(directory + "/brain/example/handoff/packet.md"))
                    self.assertFalse(is_safe_temp_or_config_path(directory + "/brain-other/packet.md"))


class EngineTests(HandoffFixture):
    def test_runtime_snapshot_bounds_status_diff_worktrees_and_artifacts(self):
        status = "\n".join(f" M {idx:02d}-" + "界" * 40 for idx in range(30))
        diff = "\n".join(f"file-{idx:02d} | 1 +" for idx in range(32))
        worktrees = [{"path": f"/work/{idx:02d}"} for idx in range(12)]
        artifacts = self.root / "artifacts"
        for idx in range(14):
            child = artifacts / f"{idx:02d}"
            child.mkdir(parents=True)
            (child / "summary.json").write_text("{}")
        for idx in range(16):
            (self.transcript.parent / f"{idx:02d}.md").touch()
        with mock.patch.object(handoff, "_git_root", return_value=self.workspace), \
             mock.patch.object(handoff, "_git", return_value=mock.Mock(stdout=b"oid\nsubject")), \
             mock.patch.object(handoff, "_git_text", side_effect=[diff, "main", status]), \
             mock.patch.object(handoff, "_worktrees", return_value=worktrees):
            snapshot = handoff.collect_runtime_snapshot(
                workspaces=[self.workspace], artifact_dirs=[artifacts],
                conversation_id=self.cid, app_data_dir=self.app,
            )
        workspace = snapshot["workspaces"][0]
        status_lines = workspace["status_porcelain"].splitlines()
        self.assertLessEqual(len(workspace["status_porcelain"].encode("utf-8")), 2048)
        self.assertEqual(status_lines[:-1], status.splitlines()[:len(status_lines) - 1])
        self.assertEqual(status_lines[-1],
                         f"... ({31 - len(status_lines)} more lines truncated; run git status --short for full list)")
        diff_lines = workspace["diff_stat"].splitlines()
        self.assertEqual(diff_lines[:-1], diff.splitlines()[:24])
        self.assertEqual(diff_lines[-1], "... (8 more lines truncated; run git diff --stat for full list)")
        self.assertEqual(workspace["worktrees"], worktrees[:10] + [{"truncated_count": 2}])
        self.assertEqual(len(snapshot["artifact_dirs"][0]["artifacts"]), 13)
        self.assertEqual(snapshot["artifact_dirs"][0]["artifacts"][-1], {"truncated_count": 2})
        self.assertEqual(len(snapshot["conversation_artifacts"]), 16)
        self.assertEqual(snapshot["conversation_artifacts"][-1], "... (1 more conversation artifacts truncated)")

    def test_small_snapshot_text_keeps_original_bytes(self):
        for text in (" M tracked.txt", "先\r\nsecond\n"):
            self.assertEqual(handoff._truncate_snapshot_text(
                text, max_lines=25, max_bytes=2048, hint="full list"), text)

    def source(self, *, metadata=None, annotations=None, **legacy_fields):
        return {
            "id": self.cid,
            "metadata": {
                "createTime": "2026-01-01T00:00:00Z", "projectId": "project-nested",
                "environmentId": "environment-nested", "rootConversationId": self.cid,
                "parentConversationId": "", "tags": [f"chain:{self.chain}", "hop:1"],
                **(metadata or {}),
            },
            "annotations": {
                "title": "研究任务", "tags": ["existing"], "archived": False,
                "conversationGroupId": "group-1", **(annotations or {}),
            },
            "state": {"summary": "fixture summary", "runState": "idle"},
            **legacy_fields,
        }

    def service(self, source=None, *, reject_model=None):
        first = self.source() if source is None else source
        conversations = {self.cid: first}
        created = []

        def dispatch(path, payload):
            if path == SERVICE + "GetConversation":
                return 200, conversations[payload["conversationId"]]
            if path == SERVICE + "CreateConversation":
                if payload["model"] == reject_model:
                    return 400, {"message": "unsupported model"}
                cid = "successor-" + str(len(created) + 1)
                created.append(cid)
                conversations[cid] = {
                    "id": cid,
                    "metadata": {key: payload[key] for key in ("projectId", "environmentId") if key in payload},
                    "annotations": {"title": payload["title"], "tags": ["existing"]},
                }
                conversations[cid]["metadata"].update(rootConversationId=cid, tags=payload["tags"])
                return 200, {"conversation": conversations[cid]}
            if path == SERVICE + "UpdateConversation":
                conversations[payload["conversationId"]]["annotations"].update(payload["annotations"])
                return 200, {}
            return 404, {"message": "unexpected RPC"}

        return self.start_service(dispatch)

    def assert_invalid(self, content, message, *, workspaces=()):
        self.packet.write_text(content, encoding="utf-8")
        args = ["--packet", self.packet]
        for workspace in workspaces:
            args.extend(("--workspace", workspace))
        for operation in ("validate", "create"):
            with self.subTest(operation=operation):
                result = self.invoke(operation, *args, exit_code=2, shim=True)
                self.assertFalse(result["valid"])
                self.assertTrue(any(message in error for error in result["errors"]), result)
        self.assertFalse((self.transcript.parent / "handoff").exists())

    def test_validate_and_create_reject_invalid_packets(self):
        valid = self.packet_text()
        self.packet.write_text(valid, encoding="utf-8")
        result = self.invoke("validate", "--packet", self.packet, shim=True)
        self.assertTrue(result["valid"])
        self.assertEqual(result["sections"], list(range(1, 9)))
        maximum = (valid + "x" * (16384 - len(valid.encode("utf-8")))).encode("utf-8")
        self.packet.write_bytes(maximum)
        self.assertTrue(self.invoke("validate", "--packet", self.packet)["valid"])
        for case, content, message in (
            ("missing", self.packet_text(omit=8), "Missing packet section 8"),
            ("short section", self.packet_text(section2="Short"), "section 2 must have at least 10"),
            ("comment", valid + "\n<!-- fill this in -->", "unresolved HTML comments or placeholders"),
            ("placeholder", valid + "\n<TODO>", "unresolved HTML comments or placeholders"),
            ("too small", "x" * 399, "400–16384 bytes"),
            ("too large", valid.ljust(16385, "x"), "400–16384 bytes"),
            ("missing working file", self.packet_text(section6=f"Open `{self.root / 'missing.py'}` now."),
             "Referenced path does not exist"),
            ("missing artifact", self.packet_text(section7=f"See [evidence]({self.root / 'missing.json'})."),
             "Referenced path does not exist"),
            ("API key", valid + "\nAPI fixture sk-0123456789abcdef", "unredacted secret"),
            ("CSRF value", valid + "\nLiteral token " + MOCK_CSRF, "unredacted secret"),
            ("CSRF assignment", valid + "\nANTIGRAVITY_CSRF_TOKEN=another-fixture-token", "unredacted secret"),
        ):
            with self.subTest(case=case):
                self.assert_invalid(content, message)

    def test_validate_ignores_slash_commands_in_path_sections(self):
        real_path = self.root / "sub dir" / "real.py"
        real_path.parent.mkdir()
        real_path.touch()
        self.packet.write_text(self.packet_text(
            section6=f"Use /handoff, `/impl`, or `/handoff --resume-mode auto`. Open `{real_path}:12-34`.",
            section7="See `/handoff --workspace /tmp/nonexistent`, `/review/`, "
                     f"`/handoff///`, and [cmd](/dag). Read [doc](<{real_path}#L10-L20>).",
        ), encoding="utf-8")

        result = self.invoke("validate", "--packet", self.packet, shim=True)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["referenced_paths"], [str(real_path)])

    def test_validate_and_create_reject_missing_path_references(self):
        missing_uri_path = self.root / "missing fixture.py"
        missing_root_path = Path("/") / self.root.name
        missing_path = self.root / "missing-dir" / "file.py"
        self.environ["HOME"] = str(self.root)
        for reference, expected in (
            ("~/missing-fixture.py", self.root / "missing-fixture.py"),
            (missing_uri_path.as_uri(), missing_uri_path),
            (missing_root_path.as_uri(), missing_root_path),
            (str(missing_path), missing_path),
        ):
            with self.subTest(reference=reference):
                self.assert_invalid(self.packet_text(section7=f"Open `{reference}` for evidence."),
                                    f"Referenced path does not exist: {expected}")

    def test_validate_and_create_reject_git_conflict_markers(self):
        file = self.init_git()
        file.write_text("<<<<<<< HEAD\nconflict\n", encoding="utf-8")
        self.assert_invalid(self.packet_text(), "Git conflict markers", workspaces=[self.workspace])

    def test_validate_and_create_reject_unmerged_git_index(self):
        self.init_git()
        oid = self.git("rev-parse", "HEAD:tracked.txt")
        self.git("update-index", "--force-remove", "tracked.txt")
        index = "".join(f"100644 {oid} {stage}\ttracked.txt\n" for stage in (1, 2, 3))
        self.git("update-index", "--index-info", input_data=index)
        self.assert_invalid(self.packet_text(), "Git index has unmerged entries", workspaces=[self.workspace])

    def test_inspect_reports_telemetry_assessment_config_and_state(self):
        self.write_transcript(checkpoint(1), planner(154000, index=2, cache=16000))
        self.save_state(checkpoint_epoch=1, handed_off=False, fired_bands=["soft"], hop_index=2)
        value = self.invoke("inspect", shim=True, extra_env={
            "CODEX_HANDOFF_MODE": " AuTo ", "CODEX_HANDOFF_MAX_HOPS": "7",
        })
        self.assertEqual(value["conversation_id"], self.cid)
        self.assertEqual(value["telemetry"]["transcript_path"], str(self.transcript))
        self.assertEqual(value["telemetry"]["active_tokens"], 170000)
        self.assertEqual(value["telemetry"]["last_planner_step_index"], 2)
        self.assertEqual(value["assessment"]["checkpoint_epoch"], 1)
        self.assertEqual(value["assessment"]["band"], "hard")
        self.assertEqual(value["assessment"]["context_limit"], 256000)
        self.assertEqual(value["config"], {"mode": "auto", "max_hops": 7})
        self.assertEqual(value["state"], {
            "checkpoint_epoch": 1, "handed_off": False, "fired_bands": ["soft"], "hop_index": 2,
        })

    def test_inspect_classifies_subagent_offline_without_writing_state(self):
        self.environ.pop("ANTIGRAVITY_CSRF_TOKEN")
        self.write_transcript(subagent_bootstrap(), planner(170000))
        value = self.invoke("inspect", shim=True)
        self.assertIs(value["telemetry"]["is_subagent"], True)
        self.assertEqual(value["state"], {"is_root": False})
        self.assertFalse((Path(self.environ["PLUGIN_DATA"]) / "handoff_state").exists())

    def test_inspect_reports_tool_activity_and_effective_mode(self):
        self.write_transcript(planner(85000, calls=[
            tool_call("run_command", CommandLine=json.dumps("ssh host")),
            tool_call("manage_task", Action="status"),
            tool_call("read_file", Path="/workspace/input.txt"),
            tool_call("invoke_subagent", TypeName="Research"),
        ]))
        expected_activity = {
            "remote_cmd_count": 2, "remote_turn_streak": 1,
            "read_file_count": 1, "invoked_subagents": ["research"],
        }
        self.assertEqual(self.invoke("inspect")["effective_mode"], "propose")
        self.save_state(chain_handoff_mode="auto", fired_nudges=["remote_noise"])
        for override, effective_mode in (("", "auto"), ("invalid", "auto"), ("propose", "propose"), ("off", "off")):
            with self.subTest(override=override):
                value = self.invoke("inspect", extra_env={"CODEX_HANDOFF_MODE": override})
                self.assertEqual(value["effective_mode"], effective_mode)
                self.assertEqual(value["tool_activity"], expected_activity)
                self.assertEqual(value["state"], {
                    "chain_handoff_mode": "auto", "fired_nudges": ["remote_noise"],
                })

    def test_inspect_resolves_tags_without_writing_missing_state(self):
        state_dir = Path(self.environ["PLUGIN_DATA"]) / "handoff_state"
        expected_state = {
            "is_root": True, "chain_id": "abc", "hop_index": 2, "chain_handoff_mode": "auto",
        }
        calls = self.start_service(lambda _path, _body: (200, {
            "conversation": {"id": self.cid, "metadata": {
                "tags": ["codex-handoff", "chain:abc", "hop:2", "handoff-mode:auto"],
            }},
        }))
        for override, mode in (("", "auto"), ("propose", "propose")):
            with self.subTest(override=override):
                value = self.invoke("inspect", shim=not self.service_uses_socketpair,
                                    extra_env={"CODEX_HANDOFF_MODE": override})
                self.assertEqual(value["effective_mode"], mode)
                self.assertEqual(value["state"], expected_state)
                self.assertFalse(state_dir.exists())
        self.assertEqual([(path, body) for path, _, body in calls],
                         [(SERVICE + "GetConversation", {"conversationId": self.cid})] * 2)
        self.assert_rpc_headers(calls)

        self.save_state(hop_index=3, chain_handoff_mode="propose")
        value = self.invoke("inspect")
        self.assertEqual(value["effective_mode"], "propose")
        self.assertEqual(value["state"], {**expected_state, "hop_index": 3, "chain_handoff_mode": "propose"})
        self.assertEqual(self.state(), {"hop_index": 3, "chain_handoff_mode": "propose"})
        self.assertEqual(len(calls), 3)

        for is_root in (True, False):
            self.save_state(is_root=is_root)
            self.assertEqual(self.invoke("inspect")["state"], {"is_root": is_root})
        for invalid_env in ({"ANTIGRAVITY_LS_ADDRESS": ""}, {"ANTIGRAVITY_CSRF_TOKEN": ""}):
            self.save_state()
            self.assertEqual(self.invoke("inspect", extra_env=invalid_env)["state"], {})
        self.assertEqual(self.invoke("inspect", "--conversation-id", "../invalid")["state"], {})
        self.assertEqual(len(calls), 3)

    def test_hook_and_shell_share_handoff_state_when_plugin_data_is_hook_only(self):
        self.service()
        self.write_transcript(planner(170000))
        plugin_data = self.app / "plugin_data" / "codex-orchestrator"
        state_dir = plugin_data / "handoff_state"
        state_path = state_dir / f"{self.cid}.json"
        self.environ["PLUGIN_DATA"] = str(plugin_data)
        turn = "/handoff 接续实验"
        turn_hash = telemetry.hash_user_input(turn)
        self.assertEqual(self.hook(lastUserInput=turn), {})
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["last_seen_user_hash"], turn_hash)

        del self.environ["PLUGIN_DATA"]
        self.assertEqual(telemetry.handoff_state_dir(self.environ), state_dir)
        # The mock socket-pair transport is available only within this process.
        receipt = self.create(shim=not self.service_uses_socketpair)
        self.assertEqual(receipt["new_conversation_id"], "successor-1")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["handed_off_turn_hash"], turn_hash)
        self.assertTrue(state["handed_off"])
        inspected = self.invoke("inspect", shim=True)["state"]
        self.assertTrue(inspected["handed_off"])
        self.assertEqual(inspected["handed_off_to"], receipt["new_conversation_id"])
        self.assertEqual(inspected["hop_index"], state["hop_index"])

        self.environ["PLUGIN_DATA"] = str(plugin_data)
        self.assertEqual(self.hook(lastUserInput=turn), {"injectSteps": [{"systemMessage": {
            "systemMessage": "[codex-context-handoff:post_handoff] Handoff to successor-1 already succeeded. "
                             "Do not invoke any more mutating tools. Output the final handoff receipt now.",
        }}]})
        self.assertEqual(self.hook(lastUserInput=turn), {})

    def test_create_persists_canonical_artifacts_and_successor_can_handoff_again(self):
        calls = self.service(self.source(
            rootConversationId="wrong-legacy-root", parentConversationId="wrong-legacy-parent",
            projectId="wrong-legacy-project", environmentId="wrong-legacy-environment",
        ))
        file = self.init_git()
        file.write_text("changed\n", encoding="utf-8")
        self.write_transcript(planner(170000))
        self.assertEqual(self.hook(lastUserInput="/handoff 接续实验"), {})
        turn_hash = telemetry.hash_user_input("/handoff 接续实验")
        self.assertEqual(self.state()["last_seen_user_hash"], turn_hash)
        calls.clear()
        result = self.create("--workspace", self.workspace, "--resume-mode", "auto")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["new_conversation_id"], "successor-1")
        self.assertEqual([path for path, _, _ in calls], [SERVICE + name for name in (
            "GetConversation", "CreateConversation", "UpdateConversation", "UpdateConversation",
        )])
        self.assert_rpc_headers(calls)
        self.assertEqual(calls[0][2], {"conversationId": self.cid})
        create = calls[1][2]
        self.assertEqual(create["projectId"], "project-nested")
        self.assertEqual(create["environmentId"], "environment-nested")
        self.assertEqual(create["model"], MODEL)
        self.assertEqual(create["title"], "研究任务 [接力 #2]")
        self.assertEqual(create["tags"][:-1], [
            "codex-handoff", f"chain:{self.chain}", "hop:2", f"from:{self.cid[:8]}", "handoff-mode:auto",
        ])
        self.assertRegex(create["tags"][-1], r"^attempt:[0-9a-f]{12}$")
        self.assertEqual(calls[2][2], {
            "conversationId": "successor-1", "annotations": {"conversationGroupId": "group-1"},
        })
        self.assertEqual(calls[3][2], {
            "conversationId": self.cid, "annotations": {"tags": ["existing", "handoff-completed"]},
        })

        out = self.transcript.parent / "handoff"
        packet = out / "handoff-hop-02.md"
        manifest_path = out / "handoff-manifest.json"
        receipt_path = out / "handoff-receipt.json"
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result["packet_path"], str(packet))
        self.assertEqual(result["manifest_path"], str(manifest_path))
        self.assertEqual(result["receipt_path"], str(receipt_path))
        manifest_text = manifest_path.read_text(encoding="utf-8")
        self.assertIn("研究任务 [接力 #2]", manifest_text)
        manifest = json.loads(manifest_text)
        packet_text = packet.read_text(encoding="utf-8")
        _, snapshot = packet_text.split("## 9. Deterministic Runtime Snapshot\n\n```json\n")
        self.assertEqual(json.loads(snapshot.removesuffix("```\n")), manifest["runtime_snapshot"])
        workspace = manifest["runtime_snapshot"]["workspaces"][0]
        self.assertEqual(workspace["head_oid"], self.git("rev-parse", "HEAD"))
        self.assertEqual(workspace["status_porcelain"], " M tracked.txt")
        self.assertEqual(manifest["assessment"]["band"], "hard")
        self.assertEqual(manifest["assessment"]["active_tokens"], 170000)
        self.assertEqual(manifest["assessment"]["context_limit"], 256000)
        for path in (receipt_path, self.pointer):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)
        self.assertEqual(self.state()["handed_off_turn_hash"], turn_hash)
        self.assertEqual(telemetry.load_conversation_state("successor-1", environ=self.environ), {
            "conversation_id": "successor-1", "is_root": True, "chain_id": self.chain, "hop_index": 2,
            "chain_handoff_mode": "auto",
        })

        prompt = create["prompt"][0]["text"]
        self.assertTrue(prompt.startswith(
            f"[Codex Orchestrator Session Handoff — Chain {self.chain} · Hop 2/5]\n"
            f"上一会话：conversation://{self.cid}\n"
        ))
        read_step, sanity_step, resume_step, footer = prompt.splitlines()[-4:]
        self.assertTrue(read_step.startswith("1. "))
        for fragment in ("本步仅用", "read_file", str(packet), "禁止再读 `handoff-manifest.json`",
                         "禁止预读第 6 节", "Active Working Set", "扫描整个仓库或通读",
                         "StartLine", "EndLine", ">= 3", "必须并行派发", 'TypeName="research"',
                         str(self.transcript), "只在需要时查询"):
            self.assertIn(fragment, read_step)
        self.assertNotIn(str(manifest_path), read_step)
        self.assertEqual(sanity_step,
                         "2. 在 3 秒内运行 `git rev-parse HEAD` 和 `git status --short`，与第 9 节快照核对。")
        self.assertTrue(resume_step.startswith("3. auto"))
        for fragment in ("第 1 节约束", "第 2 节", "Immediate Next Atomic Action", "无需重复确认",
                         "remote-runner", "research", "首轮即并行派发"):
            self.assertIn(fragment, resume_step)
        expected = {
            "prev_conversation_id": self.cid, "prev_transcript_path": str(self.transcript),
            "chain_id": self.chain, "hop_index": 2, "max_hops": 5, "resume_mode": "auto",
        }
        self.assertEqual(footer, json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        reminder = self.hook(lastUserInput="/handoff 接续实验")
        self.assertIn("Handoff to successor-1 already succeeded",
                      reminder["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(self.hook(lastUserInput="/handoff 接续实验"), {})

        calls.clear()
        result = self.create("--conversation-id", "successor-1", "--archive-old", "--resume-mode", "pause")
        self.assertEqual(result["hop_index"], 3)
        self.assertEqual(calls[1][2]["title"], "研究任务 [接力 #3]")
        self.assertNotIn("handoff-mode:auto", calls[1][2]["tags"])
        self.assertNotIn("chain_handoff_mode", telemetry.load_conversation_state(
            "successor-2", environ=self.environ,
        ))
        self.assertEqual(calls[3][2], {
            "conversationId": "successor-1",
            "annotations": {"tags": ["existing", "handoff-completed"], "archived": True},
        })
        pause_step, footer = calls[1][2]["prompt"][0]["text"].splitlines()[-2:]
        self.assertTrue(pause_step.startswith("3. pause"))
        for fragment in ("不超过 5 行中文", "第 8 节待决事项", "等待用户确认"):
            self.assertIn(fragment, pause_step)
        self.assertEqual(json.loads(footer)["resume_mode"], "pause")
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8"))["hop_index"], 3)

    def test_normalizes_all_trailing_handoff_title_forms(self):
        for suffix in ("| [接力 #2]", "· (接力 #2)", "— [Hop 2]", "- (hOP 2)", "[Hop 1] — [接力 #2]"):
            with self.subTest(suffix=suffix):
                self.assertEqual(handoff.normalize_handoff_title("  实验  状态  " + suffix, 3), "实验 状态 [接力 #3]")
        self.assertEqual(handoff.normalize_handoff_title("[接力 #2]", 3), "Codex Orchestrator Session [接力 #3]")

    def test_legacy_metadata_fields_and_optional_ids(self):
        source = self.source(projectId="legacy-project", environmentId="legacy-environment",
                             rootConversationId=self.cid)
        for field in ("projectId", "environmentId", "rootConversationId"):
            del source["metadata"][field]
        calls = self.service(source)
        self.create()
        self.assertEqual(calls[1][2]["projectId"], "legacy-project")
        self.assertEqual(calls[1][2]["environmentId"], "legacy-environment")
        # Explicitly empty metadata takes precedence over populated legacy fields.
        source["metadata"].update(projectId="", environmentId="")
        calls.clear()
        self.create("--force", extra_env={"CODEX_HANDOFF_ARCHIVE_OLD": " yes "})
        self.assertNotIn("projectId", calls[1][2])
        self.assertNotIn("environmentId", calls[1][2])
        self.assertEqual(calls[3][2]["annotations"], {"tags": ["existing", "handoff-completed"], "archived": True})
        self.assertNotIn("archived", calls[3][2])

    def test_model_400_retries_with_tier_and_records_receipt(self):
        exact = "gemini-4-neon-xhigh"
        calls = self.service(reject_model=exact)
        result = self.create("--model", exact)
        creates = [body for path, _, body in calls if path == SERVICE + "CreateConversation"]
        self.assertEqual([request["model"] for request in creates], [exact, "pro"])
        self.assertEqual(creates[1], dict(creates[0], model="pro"))
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["model_requested"], exact)
        self.assertEqual(receipt["model_used"], "pro")
        self.assertIs(receipt["model_fallback_applied"], True)

    def test_child_root_or_parent_guard_and_override(self):
        source = self.source(metadata={"rootConversationId": "another-root"})
        calls = self.service(source)
        for metadata in ({"rootConversationId": "another-root", "parentConversationId": ""},
                         {"rootConversationId": self.cid, "parentConversationId": "another-parent"}):
            with self.subTest(metadata=metadata):
                source["metadata"].update(metadata)
                result = self.create(exit_code=2)
                self.assertIn("--allow-child", result["error"])
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"] * 2)
        self.assertEqual(self.create("--allow-child")["status"], "ok")
        self.assertIs(self.state()["is_root"], False)

    def test_duplicate_same_epoch_requires_force_and_new_epoch_allows_create(self):
        calls = self.service()
        self.write_transcript(planner(170000))
        self.create()
        count = len(calls)
        self.assertIn("already exists", self.create(exit_code=2)["error"])
        self.assertEqual(len(calls), count)
        self.assertEqual(self.create("--force")["status"], "ok")
        self.write_transcript(planner(170000), checkpoint(2))
        self.assertEqual(self.create()["checkpoint_epoch"], 1)

    def test_new_user_message_preserves_same_epoch_duplicate_create_guard(self):
        calls = self.service()
        self.write_transcript(planner(170000))
        self.hook(lastUserInput="/handoff")
        result = self.create()
        self.assertIs(self.state()["post_handoff_turn_active"], True)
        self.hook(lastUserInput="Start the next task")
        state = self.state()
        self.assertIs(state["post_handoff_turn_active"], False)
        self.assertIs(state["handed_off"], True)
        self.assertEqual(state["handed_off_to"], result["new_conversation_id"])
        self.assertEqual(state["handed_off_epoch"], result["checkpoint_epoch"])
        call_count = len(calls)
        self.assertIn("already exists", self.create(exit_code=2)["error"])
        self.assertEqual(len(calls), call_count)

    def test_create_timeout_marks_outcome_unknown_and_reconciles(self):
        self.write_transcript(planner(170000))
        for immediately_visible in (False, True):
            with self.subTest(immediately_visible=immediately_visible):
                self.save_state()
                visible = immediately_visible
                attempts = []

                def dispatch(path, body):
                    if path == SERVICE + "GetConversation":
                        return 200, self.source()
                    if path == SERVICE + "CreateConversation":
                        attempts.append(self.state()["pending_handoff"])
                        return 200, {"id": "existing-successor"}
                    if path == SERVICE + "ListConversations":
                        return 200, {"conversations": [
                            {"id": self.cid},
                            {"id": "different-attempt", "metadata": {"tags": ["attempt:000000000000"]}},
                            {"id": "existing-successor", "metadata": {"tags": body["tags"]}},
                        ] if visible else []}
                    return 200, {}

                calls = self.start_service(dispatch)
                load = json.load

                def read_response(response):
                    if response.geturl().endswith(SERVICE + "CreateConversation"):
                        raise TimeoutError(errno.ETIMEDOUT, "response timed out")
                    return load(response)

                with mock.patch.object(handoff.json, "load", side_effect=read_response):
                    result = self.create("--resume-mode", "auto", exit_code=0 if visible else 7)
                self.assertEqual(attempts[0]["resume_mode"], "auto")
                tag = result.get("attempt_tag") or attempts[0]["attempt_tag"]
                self.assertRegex(tag, r"^attempt:[0-9a-f]{12}$")
                if immediately_visible:
                    self.assertEqual(result["status"], "ok")
                    self.assertTrue(result["reconciled_after_timeout"])
                    self.assertEqual(result["new_conversation_id"], "existing-successor")
                else:
                    self.assertEqual(result["status"], "create_outcome_unknown")
                    self.assertIs(result["reconciliation_required"], True)
                    self.assertNotIn("fallback_prompt_path", result)
                    self.assertFalse(Path(result["receipt_path"]).with_name("handoff-bootstrap.txt").exists())
                    self.assertEqual(json.loads(Path(result["receipt_path"]).read_text()), result)
                    self.assertEqual(self.state()["pending_handoff"], attempts[0])
                    self.assertIn("unknown outcome", self.create(exit_code=2)["error"])
                    visible = True
                    self.assertIn("already created successor existing-successor", self.create(exit_code=2)["error"])
                self.assertNotIn("pending_handoff", self.state())
                self.assertEqual(self.state()["handed_off_to"], "existing-successor")
                self.assertEqual(telemetry.load_conversation_state(
                    "existing-successor", environ=self.environ,
                )["chain_handoff_mode"], "auto")
                creates = [body for path, _, body in calls if path == SERVICE + "CreateConversation"]
                self.assertEqual(len(creates), 1)
                self.assertIn(tag, creates[0]["tags"])
                lookups = [body for path, _, body in calls if path == SERVICE + "ListConversations"]
                query = {"tags": [tag], "pageSize": 5, "includeArchived": True}
                self.assertEqual(lookups, [query] * (1 if immediately_visible else 3))

    def test_max_hops_guard(self):
        calls = self.service(self.source(metadata={"tags": [f"chain:{self.chain}", "hop:5"]}))
        result = self.create(exit_code=2)
        self.assertIn("Next hop 6 exceeds the maximum of 5", result["error"])
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"])

    def test_unavailable_rpc_preserves_fallback_and_redacts_credentials(self):
        cases = ("missing-env", "forbidden-get", "forbidden-create", "refused")
        for case in cases:
            with self.subTest(case=case):
                extra = {"ANTIGRAVITY_LS_ADDRESS": ""}
                transport = mock.patch.object(
                    socket, "create_connection", side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
                )
                if case == "refused":
                    with transport:
                        result = self.create("--title", "恢复 " + MOCK_CSRF, exit_code=7,
                                             extra_env={"ANTIGRAVITY_LS_ADDRESS": "http://127.0.0.1:1"})
                    self.assertIn("could not reach", result["error"])
                else:
                    if case.startswith("forbidden"):
                        def dispatch(path, _body):
                            if case == "forbidden-create" and path == SERVICE + "GetConversation":
                                return 200, self.source(annotations={"title": "恢复 " + MOCK_CSRF})
                            return 403, {"message": MOCK_CSRF}

                        self.start_service(dispatch)
                        extra = {}
                    result = self.create("--title", "恢复 " + MOCK_CSRF, exit_code=7, extra_env=extra)
                    self.assertIn("forbidden" if extra == {} else "missing", result["error"])
                self.assertEqual(result["status"], "ls_api_unreachable")
                out = self.transcript.parent / "handoff"
                for name in ("handoff-hop-02.md", "handoff-manifest.json", "handoff-bootstrap.txt",
                             "handoff-receipt.json"):
                    text = (out / name).read_text(encoding="utf-8")
                    self.assertTrue(text)
                    self.assertNotIn(MOCK_CSRF, text)
                self.assertFalse(self.pointer.exists())
                self.assertFalse(self.state().get("handed_off"))
                self.assertNotIn("pending_handoff", self.state())
                shutil.rmtree(out)

    def test_output_directory_default_explicit_and_manual_fallback(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
                mock.patch.object(handoff, "_HANDOFFS", self.root / "handoffs"):
            default = handoff._output_dir(None, self.chain, 2, cid=self.cid, environ={})
            self.assertEqual(default, self.root / ".gemini" / "antigravity" / "brain" / self.cid / "handoff")
            explicit = self.root / "explicit"
            self.assertEqual(handoff._output_dir(explicit, self.chain, 2, cid=self.cid), explicit)
            manual = handoff._output_dir(None, self.chain, 2)
            self.assertEqual(manual.parent, self.root / "handoffs" / self.chain)
            self.assertTrue(manual.name.startswith("hop-02-"))
            with self.assertRaises(ValueError):
                handoff._output_dir(None, self.chain, 2, cid="../invalid", environ={})


class HookTests(HandoffFixture):
    def noise_plan(self, tokens=80000, *, remote=0, reads=0, subagent=None):
        calls = [tool_call("run_command", CommandLine=json.dumps("ssh host")) for _ in range(remote)]
        calls.extend(tool_call("read_file", Path=f"/workspace/item-{index}") for index in range(reads))
        if subagent:
            calls.append(tool_call("invoke_subagent", TypeName=json.dumps(subagent)))
        return planner(tokens, calls=calls)

    def message(self, result, *, hook_metadata=False):
        self.assertEqual(len(result.get("injectSteps", [])), 1, result)
        message = result["injectSteps"][0]["systemMessage"]
        if not hook_metadata:
            self.assertNotIn("metadata", message)
        return message

    def assert_band(self, result, band):
        message = self.message(result)["systemMessage"]
        self.assertTrue(message.startswith(f"[codex-context-handoff:{band}] 上下文 "), message)
        self.assertIn(" tokens（", message)
        self.assertIn("/handoff", message)
        return message

    def assert_nudge(self, result, nudge, *fragments):
        message = self.message(result)["systemMessage"]
        self.assertTrue(message.startswith(f"[codex-context-handoff:{nudge}] "), message)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_remote_noise_by_count_or_streak_once_per_checkpoint(self):
        remote_turn = self.noise_plan(remote=1)
        self.write_transcript(self.noise_plan(remote=3))
        self.assertEqual(self.hook(), {})
        self.write_transcript(self.noise_plan(remote=4))
        self.assert_nudge(self.hook(), "remote_noise", "4 次", "1 个", "remote-runner", 'Model="flash"')
        self.assertEqual(self.hook(), {})

        self.write_transcript(checkpoint(2), remote_turn, remote_turn, planner(80000), remote_turn)
        self.assert_nudge(self.hook(), "remote_noise", "3 次", "3 个")
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.state()["fired_nudges"], ["remote_noise"])
        self.assertEqual(self.state()["checkpoint_epoch"], 1)

    def test_remote_noise_skips_existing_remote_runner(self):
        self.write_transcript(self.noise_plan(remote=4, subagent="Remote-Runner"))
        self.assertEqual(self.hook(), {})
        self.write_transcript(self.noise_plan(remote=4, subagent="Remote-Runner"),
                              checkpoint(2), self.noise_plan(remote=4))
        self.assert_nudge(self.hook(), "remote_noise", "remote-runner")

    def test_read_noise_requires_both_thresholds_and_skips_existing_research(self):
        for plan in (self.noise_plan(30719, reads=6), self.noise_plan(reads=5),
                     self.noise_plan(reads=6, subagent="Research")):
            self.write_transcript(plan)
            self.assertEqual(self.hook(), {})
        self.write_transcript(self.noise_plan(reads=6, subagent="Research"),
                              checkpoint(2), self.noise_plan(reads=6))
        self.assert_nudge(self.hook(), "read_noise", "6 次", "80,000 tokens", "research",
                          "6 次未限定范围", "请停止继续整文件 read_file", "StartLine/EndLine")
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.state()["fired_nudges"], ["read_noise"])

    def test_read_noise_model_relative_floor(self):
        for epoch, (model, floor) in enumerate(
            (("gpt-oss-120b", 9600), ("gemini-3-1-pro-high", 15360))
        ):
            with self.subTest(model=model):
                prefix = [checkpoint(index) for index in range(epoch)]
                for tokens in (floor - 1, floor):
                    row = self.noise_plan(tokens, reads=6)
                    row["model"] = model
                    self.write_transcript(*prefix, row)
                    result = self.hook(modelName=model)
                    if tokens == floor:
                        self.assert_nudge(result, "read_noise", f"{floor:,} tokens")
                    else:
                        self.assertEqual(result, {})

    def test_read_noise_spike_requires_previous_counted_read(self):
        for previous, tokens in ((planner(1000), 26000), (self.noise_plan(1000, reads=1), 25999)):
            self.write_transcript(previous, planner(tokens))
            self.assertEqual(self.hook(), {})
        self.write_transcript(self.noise_plan(1000, reads=1), planner(26000))
        self.assert_nudge(self.hook(), "read_noise", "单步激增 +25,000 tokens", "1 次")

    def test_read_noise_rearms_six_reads_after_ignored_l1_and_resets(self):
        self.write_transcript(self.noise_plan(40000, reads=8))
        self.assert_nudge(self.hook(), "read_noise", "8 次")
        self.assertEqual(self.state()["read_noise_l1_reads"], 8)
        self.write_transcript(self.noise_plan(40000, reads=13))
        self.assertEqual(self.hook(), {})
        self.write_transcript(self.noise_plan(40000, reads=14))
        self.assert_nudge(self.hook(), "read_noise_l2", "14 次", "40,000 tokens", "200K", "rg/python3")
        self.assertEqual(self.hook(), {})
        self.write_transcript(checkpoint(2), planner(40000))
        self.assertEqual(self.hook(), {})
        self.assertNotIn("read_noise_l1_reads", self.state())
        self.assertEqual(self.state()["fired_nudges"], [])
        self.write_transcript(checkpoint(2), self.noise_plan(40000, reads=6))
        self.assert_nudge(self.hook(), "read_noise", "6 次")

    def test_read_noise_rearms_after_drift_since_most_recent_research(self):
        prefix = [self.noise_plan(40000, reads=20, subagent="Research"),
                  self.noise_plan(40000, reads=11, subagent="RESEARCH")]
        for tokens, reads in ((40000, 11), (30719, 12)):
            self.write_transcript(*prefix, self.noise_plan(tokens, reads=reads))
            self.assertEqual(self.hook(), {})
        self.write_transcript(*prefix, self.noise_plan(40000, reads=12))
        self.assert_nudge(self.hook(), "read_noise_l2", "43 次", "12 次", "40,000 tokens")
        self.assertEqual(self.state()["fired_nudges"], ["read_noise_l2"])
        self.assertEqual(self.hook(), {})

    def test_vertical_then_remote_then_read_each_inject_once(self):
        self.write_transcript(self.noise_plan(120000, remote=4, reads=12))
        self.assert_band(self.hook(), "soft")
        self.assert_nudge(self.hook(), "remote_noise", "remote-runner")
        self.assert_nudge(self.hook(), "read_noise", "research")
        self.assertEqual(self.hook(), {})

    def test_noise_controls_are_independent_and_opt_outs_are_not_recorded(self):
        self.write_transcript(self.noise_plan(170000, remote=4))
        self.environ["CODEX_NOISE_NUDGE"] = "off"
        self.assert_band(self.hook(), "hard")
        self.assertEqual(self.hook(), {})
        self.environ["CODEX_HANDOFF_MODE"] = "off"
        self.save_state()
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.state(), {})

        self.environ["CODEX_NOISE_NUDGE"] = "on"
        for prompt in ("不用子代理", "别用子代理", "不要子代理", "主循环直跑", "关闭降噪提醒",
                       "skip noise nudge", "NO NOISE NUDGE", "disable noise nudge", "no subagent",
                       "  /Compact", "/handoff"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.hook(lastUserInput=prompt), {})
                self.assertNotIn("fired_nudges", self.state())
        self.assert_nudge(self.hook(), "remote_noise", "remote-runner")
        self.assertNotIn("fired_bands", self.state())
        self.assertEqual(self.hook(), {})
        self.write_transcript(checkpoint(2), self.noise_plan(170000, remote=4))
        self.assert_nudge(self.hook(), "remote_noise", "remote-runner")

        self.environ.pop("CODEX_HANDOFF_MODE")
        self.save_state()
        self.assert_band(self.hook(lastUserInput="no subagent"), "hard")
        self.assertEqual(self.hook(lastUserInput="no subagent"), {})
        self.assert_nudge(self.hook(lastUserInput="skip handoff"), "remote_noise", "remote-runner")

    def test_noise_opt_in_metadata_uses_inject_envelope(self):
        self.environ["CODEX_HANDOFF_HOOK_METADATA"] = "true"
        self.write_transcript(self.noise_plan(remote=4))
        message = self.message(self.hook(), hook_metadata=True)
        self.assertEqual(message["metadata"], {
            "source": "codex-context-handoff", "nudge": "remote_noise",
            "user_markdown": message["systemMessage"].split("] ", 1)[1],
        })

    def test_preinvocation_fires_soft_then_hard_once_and_resets_after_checkpoint(self):
        soft = planner(120000)
        hard = planner(170000, index=2)
        self.write_transcript(soft)
        message = self.assert_band(self.hook(), "soft")
        self.assertIn("上下文 120,000 / 256,000 tokens", message)
        self.assertIn("Context pressure is rising", message)
        next_turn = "继续第二步"
        self.assertEqual(self.hook(lastUserInput=next_turn), {})
        self.assertEqual(self.state()["last_seen_user_hash"], telemetry.hash_user_input(next_turn))
        self.write_transcript(soft, hard)
        self.assert_band(self.hook(), "hard")
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.state()["fired_bands"], ["soft", "hard"])
        self.write_transcript(soft, hard, checkpoint(3))
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.state()["checkpoint_epoch"], 1)
        self.assertEqual(self.state()["fired_bands"], [])
        self.write_transcript(soft, hard, checkpoint(3), planner(120000, index=4))
        self.assert_band(self.hook(), "soft")

    def assert_hop_limit_notice(self, band, *fragments):
        message = self.message(self.hook())["systemMessage"]
        self.assertTrue(message.startswith(f"[codex-context-handoff:{band}] 上下文 "), message)
        self.assertNotIn("handoff.sh create", message)
        self.assertNotIn("`/handoff`", message)
        for fragment in ("已达接力跳数上限", "/compact", *fragments):
            self.assertIn(fragment, message)

    def test_preinvocation_at_max_hops_keeps_pressure_urgency(self):
        calls = self.start_service(lambda _path, _body: (200, {
            "conversation": {"id": self.cid, "metadata": {
                "tags": ["codex-handoff", "chain:abc", "hop:5", "handoff-mode:auto"],
            }},
        }))
        for inherited in (False, True):
            self.environ["CODEX_HANDOFF_MODE"] = "" if inherited else "auto"
            for band, tokens, urgency in (
                ("hard", 170000, "Pause at a safe point."),
                ("emergency", 210000, "Avoid starting additional work."),
            ):
                with self.subTest(inherited=inherited, band=band):
                    if inherited:
                        self.save_state()
                    else:
                        self.save_state(is_root=True, hop_index=5)
                    self.write_transcript(planner(tokens))
                    self.assert_hop_limit_notice(band, urgency, "--max-hops")
                    self.assertEqual(self.state()["hop_index"], 5)
                    if inherited:
                        self.assertEqual(self.state()["chain_handoff_mode"], "auto")
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"] * 2)

    def test_preinvocation_at_max_hops_suggests_compaction_for_soft_and_fatigue(self):
        self.environ["CODEX_HANDOFF_MODE"] = "auto"
        self.save_state(hop_index=5)
        self.write_transcript(planner(120000))
        self.assert_hop_limit_notice("soft", "已达接力跳数上限，可用 /compact 压缩")
        self.write_transcript(planner(120000), checkpoint(2), checkpoint(3))
        self.assert_hop_limit_notice("checkpoint_fatigue", "已多次压缩", "Ask the user", "--max-hops")

    def test_preinvocation_below_max_hops_keeps_auto_handoff(self):
        self.environ["CODEX_HANDOFF_MODE"] = "auto"
        self.save_state(hop_index=4)
        self.write_transcript(planner(170000))
        self.assertIn("handoff.sh create --packet <packet> --resume-mode auto",
                      self.assert_band(self.hook(), "hard"))

    def test_checkpoint_fatigue_fires_once(self):
        self.write_transcript(planner(200000), checkpoint(2), checkpoint(3))
        self.assertIn("已多次压缩", self.assert_band(self.hook(), "checkpoint_fatigue"))
        self.assertEqual(self.hook(lastUserInput="继续实验"), {})

    def test_hook_metadata_can_be_enabled_explicitly(self):
        self.write_transcript(planner(120000))
        status = "上下文 120,000 / 256,000 tokens（46.9%），可用 /handoff 接力或 /compact 压缩。"
        for value in ("1", "true", " YeS "):
            with self.subTest(value=value):
                self.environ["CODEX_HANDOFF_HOOK_METADATA"] = value
                self.save_state()
                message = self.message(self.hook(), hook_metadata=True)
                self.assertEqual(message["metadata"], {
                    "source": "codex-context-handoff", "band": "soft", "user_markdown": status,
                })
                self.assertTrue(message["systemMessage"].startswith(f"[codex-context-handoff:soft] {status} "))

        self.save_state(handed_off=True, handed_off_epoch=0, handed_off_turn_input="继续当前任务",
                        handed_off_to="successor-1")
        reminder = self.message(self.hook(), hook_metadata=True)
        self.assertEqual(reminder["metadata"], {"source": "codex-context-handoff"})
        self.assertTrue(reminder["systemMessage"].startswith("[codex-context-handoff:post_handoff] "))

    def test_commands_opt_outs_and_off_suppress_context_notices(self):
        self.write_transcript(planner(170000))
        for prompt in ("  /compact", "/HANDOFF", "本轮不要接力", "please skip handoff", "don't use it, no handoff"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.hook(lastUserInput=prompt), {})
                self.assertEqual(self.state()["last_seen_user_hash"], telemetry.hash_user_input(prompt))
                self.assertNotIn("fired_bands", self.state())
        self.environ["CODEX_HANDOFF_MODE"] = " OfF "
        self.assertEqual(self.hook(), {})
        self.environ["CODEX_HANDOFF_MODE"] = "propose"
        self.assert_band(self.hook(), "hard")

    def test_subagent_transcript_suppresses_notices_offline(self):
        self.environ.pop("ANTIGRAVITY_CSRF_TOKEN")
        self.environ["CODEX_HANDOFF_MODE"] = "auto"
        cases = (
            ("read_noise", "PreInvocation", self.noise_plan(reads=12)),
            ("remote_noise", "PreInvocation", self.noise_plan(remote=4)),
            ("hard", "PreInvocation", planner(170000)),
            ("fatigue", "PreInvocation", planner(100)),
            ("auto_handoff", "Stop", planner(170000)),
        )
        for notice, event, work in cases:
            with self.subTest(notice=notice):
                self.save_state()
                checkpoints = (checkpoint(1), checkpoint(2)) if notice == "fatigue" else ()
                self.write_transcript(subagent_bootstrap(), *checkpoints, work)
                self.assertEqual(self.hook(event), {})
                state = self.state()
                self.assertIs(state["is_root"], False)
                for key in ("fired_nudges", "last_seen_user_hash", "fired_bands", "fatigue_fired", "stop_nudged_epoch"):
                    self.assertNotIn(key, state)

    def test_subagent_transcript_repairs_stale_state_without_rpc(self):
        self.environ.update(ANTIGRAVITY_LS_ADDRESS="http://127.0.0.1:1", CODEX_HANDOFF_MODE="auto")
        self.save_state(hop_index=2, chain_handoff_mode="auto")
        self.write_transcript(subagent_bootstrap(), planner(170000))
        with mock.patch("codex_orch.context_hook._resolve_is_root") as lookup:
            self.assertEqual(self.hook("Stop"), {})
            self.assertEqual(self.hook(), {})
        lookup.assert_not_called()
        state = self.state()
        self.assertIs(state["is_root"], False)
        self.assertEqual((state["hop_index"], state["chain_handoff_mode"]), (2, "auto"))
        for key in ("fired_nudges", "last_seen_user_hash", "stop_nudged_epoch"):
            self.assertNotIn(key, state)

    def test_payload_marks_child_offline_at_top_level_or_in_metadata(self):
        self.environ.pop("ANTIGRAVITY_CSRF_TOKEN")
        self.write_transcript(planner(170000))
        for key, value in (
            ("parentConversationId", " parent "), ("parent_conversation_id", "parent"),
            ("rootConversationId", " root "), ("root_conversation_id", "root"),
            ("isSubagent", True), ("is_subagent", True),
        ):
            for fields in ({key: value}, {"metadata": {key: value}}):
                with self.subTest(fields=fields):
                    self.save_state()
                    self.assertEqual(self.hook(**fields), {})
                    self.assertIs(self.state()["is_root"], False)

    def test_malformed_and_root_payload_fields_preserve_root_notices(self):
        self.environ.pop("ANTIGRAVITY_CSRF_TOKEN")
        self.write_transcript(planner(170000))
        self.assert_band(self.hook(
            parentConversationId=" ", parent_conversation_id=["parent"],
            rootConversationId=f" {self.cid} ", root_conversation_id=42,
            isSubagent="true", is_subagent=1, metadata=[{"isSubagent": True}],
        ), "hard")
        self.assertNotIn("is_root", self.state())

    def test_child_is_skipped_and_cached_using_conversation_service(self):
        calls = self.start_service(lambda _path, _body: (200, {
            "id": self.cid, "metadata": {"rootConversationId": "different-root"},
        }))
        self.write_transcript(self.noise_plan(170000, remote=10, reads=20))
        self.assertEqual(self.hook(), {})
        self.assertNotIn("fired_nudges", self.state())
        self.assertIs(self.state()["is_root"], False)
        self.assertNotIn("last_seen_user_hash", self.state())
        self.assertEqual(self.hook(), {})
        self.assertEqual([(path, body) for path, _, body in calls],
                         [(SERVICE + "GetConversation", {"conversationId": self.cid})])
        self.assert_rpc_headers(calls)

    def test_unreachable_rpc_fails_open_without_caching_root_decision(self):
        self.environ["ANTIGRAVITY_LS_ADDRESS"] = "http://127.0.0.1:1"
        self.write_transcript(planner(120000))
        with mock.patch.object(socket, "create_connection",
                               side_effect=PermissionError(errno.EPERM, "network disabled")):
            self.assert_band(self.hook(), "soft")
            self.assertNotIn("is_root", self.state())
        calls = self.start_service(lambda _path, _body: (200, {
            "id": self.cid, "metadata": {"rootConversationId": "different-root"},
        }))
        self.write_transcript(planner(170000, index=2))
        self.assertEqual(self.hook(), {})
        self.assertIs(self.state()["is_root"], False)
        self.assert_rpc_headers(calls)

    def test_unusable_rpc_json_fails_open(self):
        calls = self.start_service(lambda _path, _body: (200, ["unexpected JSON shape"]))
        self.write_transcript(planner(120000))
        self.assert_band(self.hook(), "soft")
        self.assertNotIn("is_root", self.state())
        self.assert_rpc_headers(calls)

    def test_hook_root_lookup_blocks_redirect_without_forwarding_csrf(self):
        calls = self.start_service(lambda _path, _body: (302, {
            "location": self.environ["ANTIGRAVITY_LS_ADDRESS"] + "/redirected",
        }))
        self.write_transcript(planner(120000))
        self.assert_band(self.hook(), "soft")
        self.assertNotIn("is_root", self.state())
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"])
        self.assert_rpc_headers(calls)

    def test_hook_state_update_preserves_concurrent_handoff_commit(self):
        self.environ["CODEX_HANDOFF_MODE"] = "auto"
        self.write_transcript(planner(170000))

        def dispatch(_path, _body):
            telemetry.update_conversation_state(
                self.cid, lambda state: state.update(
                    handed_off=True, handed_off_epoch=0, handed_off_to="concurrent-successor",
                ), environ=self.environ,
            )
            return 200, {"id": self.cid, "metadata": {"rootConversationId": self.cid}}

        self.start_service(dispatch)
        self.assertEqual(self.hook("Stop"), {})
        state = self.state()
        self.assertIs(state["is_root"], True)
        self.assertIs(state["handed_off"], True)
        self.assertEqual(state["handed_off_epoch"], 0)
        self.assertEqual(state["handed_off_to"], "concurrent-successor")
        self.assertNotIn("stop_nudged_epoch", state)

    def test_hook_never_persists_raw_user_input_plaintext(self):
        self.write_transcript(planner(120000))
        prompt = "use key sk-secretpromptvalue123456"
        self.save_state(last_seen_user_input="legacy-user-secret", handed_off_turn_input="legacy-turn-secret")
        self.assert_band(self.hook(lastUserInput=prompt), "soft")
        path = telemetry.state_file_path(self.cid, environ=self.environ)
        persisted = path.read_text(encoding="utf-8")
        for raw in ("sk-secretpromptvalue123456", "last_seen_user_input", "handed_off_turn_input",
                    "legacy-user-secret", "legacy-turn-secret"):
            self.assertNotIn(raw, persisted)
        self.assertEqual(json.loads(persisted)["last_seen_user_hash"],
                         hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24])

    def test_same_turn_hand_off_reminder_fires_once_then_new_prompt_clears_reminder(self):
        turn = "执行这一轮"
        self.write_transcript(planner(170000))
        self.assert_band(self.hook(lastUserInput=turn), "hard")
        self.save_state(**dict(self.state(), handed_off=True, handed_off_epoch=0,
                               handed_off_turn_input=turn, handed_off_to="successor-1"))
        reminder = self.message(self.hook(lastUserInput=turn))
        self.assertIn("Handoff to successor-1 already succeeded", reminder["systemMessage"])
        self.assertIn("Do not invoke any more mutating tools", reminder["systemMessage"])
        self.assertEqual(self.hook(lastUserInput=turn), {})
        self.assertIs(self.state()["handed_off"], True)
        next_turn = "现在启动下一项任务"
        self.assertEqual(self.hook(lastUserInput=next_turn), {})
        self.assertIs(self.state()["handed_off"], True)
        self.assertIs(self.state()["post_handoff_turn_active"], False)
        self.assertIs(self.state()["post_handoff_reminded"], False)
        self.assertEqual(self.state()["last_seen_user_hash"], telemetry.hash_user_input(next_turn))

    def shim(self, stdin):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "hook_context_guard.py")], input=stdin,
            capture_output=True, text=True, encoding="utf-8", env=self.environ, cwd=self.root, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_native_hook_cli_fails_open_and_emits_readable_chinese(self):
        self.assertEqual(json.loads(self.shim('{"hookEventName":')), {})
        self.write_transcript(planner(120000))
        stdout = self.shim(json.dumps({
            "hookEventName": "PreInvocation", "conversationId": self.cid,
            "transcriptPath": str(self.transcript), "lastUserInput": "继续当前任务",
        }))
        self.assertIn("上下文", stdout)
        self.assert_band(json.loads(stdout), "soft")

    def test_stop_nudges_once_per_epoch_with_no_last_user_input(self):
        self.write_transcript(planner(170000))
        self.assertEqual(self.hook("Stop"), {})
        self.assertNotIn("stop_nudged_epoch", self.state())
        self.environ["CODEX_HANDOFF_MODE"] = " AuTo "
        result = self.hook("Stop")
        self.assertEqual(result["decision"], "continue")
        self.assertIn("handoff.sh create --packet", result["reason"])
        self.assertEqual(self.state()["stop_nudged_epoch"], 0)
        self.assertEqual(self.hook("Stop"), {})
        self.write_transcript(planner(170000), checkpoint(2), planner(170000, index=3))
        self.assertEqual(self.hook("Stop", lastUserInput=None)["decision"], "continue")
        self.assertEqual(self.state()["stop_nudged_epoch"], 1)

    def test_stop_inherits_auto_from_rpc_tags_and_caches_mode(self):
        self.write_transcript(planner(170000))
        calls = self.start_service(lambda _path, _body: (200, {
            "conversation": {"id": self.cid, "metadata": {
                "tags": ["codex-handoff", "chain:example", "hop:2", "handoff-mode:auto"],
            }},
        }))
        result = self.hook("Stop")
        self.assertEqual(result["decision"], "continue")
        self.assertIn("--resume-mode auto", result["reason"])
        self.assertEqual(self.state()["chain_handoff_mode"], "auto")
        self.assertEqual(self.hook("Stop"), {})
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"])

    def test_stop_inherits_auto_from_state_and_honors_explicit_overrides(self):
        self.write_transcript(planner(170000))
        for override, decision in (("", "continue"), ("invalid", "continue"), ("propose", None), ("off", None)):
            with self.subTest(override=override):
                self.save_state(chain_handoff_mode="auto", is_root=True)
                self.environ["CODEX_HANDOFF_MODE"] = override
                self.assertEqual(self.hook("Stop").get("decision"), decision)

    def test_successor_at_max_hops_syncs_hop_from_tags_and_skips_stop_continue(self):
        self.environ.update(CODEX_HANDOFF_MODE="auto", CODEX_HANDOFF_MAX_HOPS="5")
        self.write_transcript(planner(170000))
        calls = self.start_service(lambda _path, _body: (200, {
            "conversation": {"id": self.cid, "metadata": {
                "tags": ["codex-handoff", "chain:abc", "hop:5", "hop:0", "hop:invalid"],
            }},
        }))
        self.assertEqual(self.hook("Stop"), {})
        state = self.state()
        self.assertIs(state["is_root"], True)
        self.assertEqual(state["hop_index"], 5)
        self.assertEqual(state["chain_id"], "abc")
        self.assertNotIn("stop_nudged_epoch", state)
        self.assertEqual(self.hook("Stop"), {})
        self.assertEqual([path for path, _, _ in calls], [SERVICE + "GetConversation"])

    def test_auto_stop_requires_idle_no_tool_call_pressure_and_available_hop(self):
        self.environ["CODEX_HANDOFF_MODE"] = "auto"
        self.write_transcript(planner(120000))
        self.assertEqual(self.hook("Stop"), {})
        self.write_transcript(planner(170000))
        for fields in ({"continuationActive": True}, {"fullyIdle": False}, {"terminationReason": "TOOL_CALL"},
                       {"lastUserInput": "本轮不用接力"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.hook("Stop", **fields), {})
                self.assertNotIn("stop_nudged_epoch", self.state())
        for state in ({"handed_off": True}, {"hop_index": 5}, {"hop_index": 6}):
            with self.subTest(state=state):
                self.save_state(**state)
                self.assertEqual(self.hook("Stop"), {})
                self.assertNotIn("stop_nudged_epoch", self.state())
        self.save_state(hop_index=4, handed_off=False)
        self.assertEqual(self.hook("Stop")["decision"], "continue")
