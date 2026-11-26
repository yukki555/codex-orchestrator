"""Offline integration coverage for the remote scheduler and controller."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-orchestrator"
SCRIPTS = SKILL / "scripts"
SHIM = SCRIPTS / "remote_dispatch.sh"
sys.path.insert(0, str(SCRIPTS))

from codex_orch.remote.controller import RemoteController, main, resolve_cluster_path
from codex_orch.remote.ssh_transport import RelayError, compute_file_sha256
from codex_orch.remote.gpu_probe import REMOTE_PROBE_SCRIPT, build_probe_request, parse_probe_response, probe_host
from codex_orch.remote.models import (
    AttemptProbeStatus, ClusterConfig, ConfigError, DispatchManifest, GpuSlotSnapshot, HostConfig,
    HostProbeSnapshot, HostRuntime, RelayInput, TaskAttempt, TaskSpec, ValidationError,
    load_and_merge_additional_tasks, load_cluster_config, load_dispatch_manifest, render_task_command,
)
from codex_orch.remote.scheduler import (
    advance_dag_states, compute_free_gpus, has_active_tasks, init_run_state, is_terminal,
    mark_dispatch_started, mark_relay_failed, mark_task_relaying, needs_cross_host_relay,
    plan_dispatches, reconcile_host_probe, task_counts,
)
from codex_orch.remote.ssh_transport import ControlSocketManager, SshResult, SshTransport, safe_extract_tar_bytes
from codex_orch.schema_check import matches_schema, preflight_schema


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += seconds


class FakeClusterTransport:
    """Simulate independent GPU leases and attempt observations at logical times."""

    def __init__(self, clock):
        self.clock = clock
        self.attempts = {}
        self.unreachable = set()
        self.external = {}
        self.disk = {}
        self.failures = {}
        self.preflight_failures = set()
        self.sync_failures = set()
        self.existing = set()
        self.calls = []
        self.next_pgid = 2000

    def probe(self, host, attempts):
        self.calls.append(("probe", host.id, attempts))
        if host.id in self.unreachable:
            return HostProbeSnapshot(host.id, False)
        gpus = [
            GpuSlotSnapshot(index, 24000, used, external_pids=pids)
            for index in host.gpu_ids or ()
            for used, pids in [self.external.get((host.id, index), (0, []))]
        ]
        statuses = {}
        for attempt in attempts:
            key = (attempt["task_id"], attempt["attempt"])
            running = self.attempts[key]
            done = self.clock() >= running["ends"]
            failure = self.failures.get(key)
            payload, log = None, ""
            if done:
                if failure == "ecc":
                    payload, log = {"exit_code": 1}, "CUDA uncorrectable ECC"
                elif failure == "nonfinite":
                    payload, log = {"exit_code": 1}, "non-finite"
                elif failure != "vanished":
                    payload = {"exit_code": 0, "skipped_existing": key[0] in self.existing}
            statuses[key[0]] = AttemptProbeStatus(
                not done and not running["killed"], payload,
                payload is not None and not payload.get("skipped_existing", False), log, key[1],
            )
        return HostProbeSnapshot(host.id, True, self.disk.get(host.id, 100), gpus, statuses)

    def launch_detached(self, host, *, task, run_id, out_dir, attempt_dir, gpu_ids, rendered_cmd):
        key = (task.id, int(attempt_dir.rsplit("-", 1)[1]))
        if key in self.attempts:
            raise AssertionError(f"Duplicate launch: {key}")
        for prior in self.attempts.values():
            if (prior["host"] == host.id and not prior["killed"] and prior["ends"] > self.clock()
                    and set(gpu_ids) & set(prior["gpus"])):
                raise AssertionError(f"Duplicate lease on {host.id}: {gpu_ids}")
        self.next_pgid += 1
        duration = 0 if task.id in self.existing else task.est_duration_s * (1 if host.id == "a" else 2)
        self.attempts[key] = dict(host=host.id, gpus=list(gpu_ids), start=self.clock(),
                                  ends=self.clock() + duration, pgid=self.next_pgid, killed=False)
        self.calls.append(("launch", task.id, host.id, list(gpu_ids), rendered_cmd))
        return True, self.next_pgid, None

    def kill_pgid(self, host, pgid):
        self.calls.append(("kill", host.id, pgid))
        running = next(row for row in self.attempts.values() if row["pgid"] == pgid)
        running["killed"] = True
        return self.result()

    @staticmethod
    def result(ok=True):
        return SshResult(ok, 0 if ok else 1, "", "", 0, error=None if ok else "Bearer abcdef123456")

    def run_preflight(self, host, rendered_cmd, *, timeout_s):
        self.calls.append(("preflight", host.id, rendered_cmd, timeout_s))
        return self.result(host.id not in self.preflight_failures)

    def sync_directory_tar(self, host, **kwargs):
        self.calls.append(("sync", host.id, kwargs))
        return self.result(host.id not in self.sync_failures)

    def mirror_remote_outputs(self, host, **kwargs):
        self.calls.append(("mirror", host.id, kwargs))
        return self.result()

    def pull_outputs_tar(self, host, **kwargs):
        self.calls.append(("pull", host.id, kwargs))
        return self.result()

    def close_all(self, hosts):
        self.calls.append(("close", sorted(host.id for host in hosts)))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.run_dir = self.root / "run"
        self.cluster = self.root / "cluster.json"
        self.manifest = self.root / "manifest.json"
        self.clock = Clock()
        self.transport = FakeClusterTransport(self.clock)
        env = patch.dict(os.environ, {"PLUGIN_DATA": str(self.root / "plugin"),
                                     "CODEX_ORCH_DISABLE_DISPATCH_REGISTRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.controller = RemoteController(
            transport=self.transport, clock=self.clock, sleeper=self.clock.advance,
            probe_fn=lambda transport, host, active: transport.probe(host, active),
        )

    @staticmethod
    def host(host_id="a", count=8, **options):
        return {"ssh_target": f"worker@{host_id}.example.invalid", "work_root": "/remote/work",
                "speed_weight": 2.0 if host_id == "a" else 1.0,
                "gpu_ids": list(range(count)), **options}

    @staticmethod
    def task(task_id, duration=1, **options):
        return {"id": task_id, "cmd": "true", "est_duration_s": duration, **options}

    def config(self, tasks, hosts=None, **manifest):
        self.cluster.write_text(json.dumps({"version": "1", "hosts": hosts if hosts is not None
                                            else {"a": self.host(), "b": self.host("b")}}))
        self.manifest.write_text(json.dumps({"version": "1", "run_id": "offline", "tasks": tasks, **manifest}))

    def initialize(self):
        return self.controller.initialize_run(self.run_dir, self.cluster, self.manifest)

    def tick(self, advance=0, **kwargs):
        self.clock.advance(advance)
        return self.controller.run_tick(self.run_dir, **kwargs)

    def status(self):
        return self.controller.status(self.run_dir)

    def shim(self, *args, expected=0):
        result = subprocess.run([str(SHIM), *map(str, args)], capture_output=True, text=True,
                                check=False, timeout=4)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def test_stop_cancels_only_confirmed_kills_and_blocks_dependents(self):
        self.config([self.task("job", 100), self.task("child", depends_on=["job"])])
        self.initialize()
        self.tick()
        with patch.object(self.transport, "kill_pgid", return_value=self.transport.result(False)):
            failed = self.controller.stop(self.run_dir, kill_remote=True)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["failed_kills"], ["job"])
        self.assertEqual(failed["tasks"]["job"]["state"], "running")
        stopped = self.controller.stop(self.run_dir, kill_remote=True)
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["terminal"])
        self.assertEqual(stopped["cancelled_tasks"], ["job"])
        self.assertEqual(stopped["tasks"]["child"]["state"], "blocked_dep")
        attempt = stopped["tasks"]["job"]["attempts"][0]
        self.assertEqual((attempt["state"], attempt["cancel_reason"], attempt["finished_at"]),
                         ("cancelled", "stop_kill_remote", self.clock()))
        event = next(row for row in map(json.loads, (self.run_dir / "events.jsonl").read_text().splitlines())
                     if row["kind"] == "task_cancelled")
        self.assertEqual(event["details"], {"attempt": 1, "reason": "stop_kill_remote"})

    def test_table_sections_and_mutation_receipts(self):
        self.config([self.task("job", 100)])
        self.initialize()
        self.tick()
        for action in (("status",), ("cordon", "--host", "b")):
            stdout = self.shim(*action, "--run-dir", self.run_dir, "--format", "table")
            for text in ("task_counts", "\nHOSTS\n", "\nTASKS\n", "DRAINING_FOR", "ATTEMPTS"):
                self.assertIn(text, stdout)
            self.assertNotIn('"attempt_dir"', stdout)
        receipt = self.controller.uncordon(self.run_dir, host_id="b")
        self.assertEqual((receipt["ok"], receipt["action"], receipt["host_id"]), (True, "uncordon", "b"))
        self.assertIn("hosts", receipt)
        cancelled = self.controller.cancel(self.run_dir, task_id="job")
        self.assertEqual((cancelled["action"], cancelled["cancelled_tasks"]), ("cancel", ["job"]))
        stdout = self.shim("requeue", "--run-dir", self.run_dir, "--task", "job", "--format", "table")
        self.assertIn("requeued_tasks", stdout)
        self.assertIn("\nTASKS\n", stdout)
        with patch("codex_orch.remote.controller.RemoteController", return_value=self.controller), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["plan", "--cluster", str(self.cluster), "--manifest", str(self.manifest),
                                   "--live-probe", "--format", "table"]), 0)
        for text in ("\nHOSTS / GPUS\n", "\nEXECUTION_ORDER\n", "MEM_MB", "EXT_BUSY"):
            self.assertIn(text, stdout.getvalue())

    def test_start_and_add_tasks_table_receipts(self):
        self.config([self.task("job", 100, allowed_hosts=["a"])])
        with patch("codex_orch.remote.controller.RemoteController", return_value=self.controller), \
                patch.object(self.controller, "_spawn", return_value=4321), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["start", "--cluster", str(self.cluster), "--manifest", str(self.manifest),
                                   "--run-dir", str(self.run_dir), "--format", "table"]), 0)
        fields, tables = stdout.getvalue().split("\nHOSTS\n", 1)
        self.assertIn("controller_pid", fields)
        self.assertNotIn("host_gpu_summary", fields)
        self.assertNotIn("dispatched", fields)
        hosts, dispatched = tables.split("\nDISPATCHED\n", 1)
        self.assertEqual(hosts.splitlines()[0].split(), ["HOST", "STATE", "FREE", "LEASED", "DISK_GB", "DRAINING_FOR"])
        self.assertEqual([row.split() for row in dispatched.splitlines()],
                         [["TASK_ID", "HOST", "GPUS", "ATTEMPT", "PGID"], ["job", "a", "0", "1", "2001"]])

        payload = self.root / "additional.json"
        payload.write_text(json.dumps([self.task("added", depends_on=["job"])]))
        stdout = self.shim("add-tasks", "--run-dir", self.run_dir, "--tasks", payload, "--format", "table")
        fields, tables = stdout.split("\nHOSTS\n", 1)
        self.assertIn('["added"]', fields)
        self.assertNotIn("host_gpu_summary", fields)
        self.assertNotIn('"attempt_dir"', stdout)
        _, tasks = tables.split("\nTASKS\n", 1)
        self.assertEqual(tasks.splitlines()[1].split()[:2], ["added", "pending"])

    def test_add_tasks_live_and_invalid_payloads_leave_run_files_unchanged(self):
        self.config([self.task("completed"), self.task("cancelled", 100), self.task("live", 100)])
        self.initialize()
        self.tick()
        self.tick(2)
        self.controller.cancel(self.run_dir, task_id="cancelled")
        payload = self.root / "additional.json"
        payload.write_text(json.dumps([self.task("new-ready", depends_on=["completed"])]))
        receipt = self.controller.add_tasks(self.run_dir, payload)
        self.assertEqual((receipt["action"], receipt["added_tasks"]), ("add_tasks", ["new-ready"]))
        self.assertEqual(receipt["tasks"]["new-ready"]["state"], "ready")
        self.assertEqual(receipt["tasks"]["live"]["state"], "running")
        payload.write_text(json.dumps({"tasks": [self.task("new-blocked", depends_on=["cancelled"]),
                                                  self.task("new-pending", depends_on=["live"])]}))
        receipt = json.loads(self.shim("add-tasks", "--run-dir", self.run_dir, "--tasks", payload))
        self.assertEqual(receipt["added_tasks"], ["new-blocked", "new-pending"])
        self.assertEqual(receipt["tasks"]["new-blocked"]["state"], "blocked_dep")
        self.assertEqual(receipt["tasks"]["new-pending"]["state"], "pending")
        self.assertEqual(stat.S_IMODE((self.run_dir / "manifest.snapshot.json").stat().st_mode), 0o600)
        paths = [self.run_dir / name for name in ("manifest.snapshot.json", "state.json",
                                                "summary.json", "events.jsonl")]
        before = [path.read_bytes() for path in paths]
        for invalid in ([self.task("live")], [self.task("x", depends_on=["y"]), self.task("y", depends_on=["x"])]):
            payload.write_text(json.dumps(invalid))
            self.assertFalse(json.loads(self.shim("add-tasks", "--run-dir", self.run_dir,
                                                  "--tasks", payload, expected=2))["ok"])
            self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertIn("new-ready", [row["task_id"] for row in self.tick()["dispatched"]])

    def test_stall_watchdog_kills_then_settles_failure(self):
        self.config([self.task("stall", 100, stall_timeout_s=5)])
        self.initialize()
        started = self.tick()["dispatched"][0]
        killed = self.tick(6)["preempted"]
        self.assertEqual([(row["reason"], row["pgid"]) for row in killed], [("stall_timeout", started["pgid"])])
        task = self.status()["tasks"]["stall"]
        self.assertEqual((task["state"], task["attempts"][0]["state"]), ("running", "cancelling"))
        observed = [row for call in self.transport.calls if call[0] == "probe" for row in call[2]]
        self.assertEqual(observed[-1]["gpu_ids"], started["gpu_ids"])
        self.assertTrue(self.tick(1)["terminal"])
        self.assertEqual(self.status()["tasks"]["stall"]["state"], "failed")
        events = [json.loads(row) for row in (self.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["details"]["reason"], "stall_timeout")

    def test_cross_host_marker_precheck_unlocks_dependent_without_launch(self):
        self.config([self.task("existing", done_markers=["done"]),
                     self.task("ineligible", done_markers=["done"], allowed_hosts=["a"]),
                     self.task("child", depends_on=["existing"])])
        self.initialize()
        requests = {}

        def probe(transport, host, active, *, marker_checks):
            requests[host.id] = marker_checks
            result = transport.probe(host, active)
            if host.id == "b":
                result.existing_done_tasks = [check["task_id"] for check in marker_checks]
            return result

        self.controller.probe_fn = probe
        dispatched = {row["task_id"] for row in self.tick()["dispatched"]}
        self.assertEqual(requests["b"], [{"task_id": "existing",
                                          "out_dir": "/remote/work/offline/outputs/existing",
                                          "done_markers": ["done"]}])
        self.assertEqual(dispatched, {"ineligible", "child"})
        existing = self.status()["tasks"]["existing"]
        self.assertEqual((existing["state"], existing["completed_host"], existing["attempts"]),
                         ("completed", "b", []))

    def test_cross_host_marker_precheck_cascades_before_relay_or_launch(self):
        self.config([
            self.task("upstream", allowed_hosts=["b"], done_markers=["done"]),
            self.task("downstream", allowed_hosts=["a"], depends_on=["upstream"], done_markers=["done"],
                      relay_inputs=[{"from_task": "upstream", "rel_path": "artifact", "target_rel_path": "artifact"}]),
            self.task("final", allowed_hosts=["b"], depends_on=["downstream"], done_markers=["done"]),
        ])
        for upstream_exists in (True, False):
            with self.subTest(upstream_exists=upstream_exists):
                self.run_dir = self.root / str(upstream_exists)
                self.initialize()
                markers = {"downstream", "final"}
                if upstream_exists:
                    markers.add("upstream")
                requests = {}

                def probe(transport, host, active, *, marker_checks):
                    requests[host.id] = [check["task_id"] for check in marker_checks]
                    snapshot = transport.probe(host, active)
                    snapshot.existing_done_tasks = [task_id for task_id in requests[host.id] if task_id in markers]
                    return snapshot

                self.controller.probe_fn = probe
                with patch.object(self.transport, "relay_file", create=True) as relay, \
                        patch.object(self.transport, "launch_detached", wraps=self.transport.launch_detached) as launch:
                    receipt = self.tick()
                relay.assert_not_called()
                self.assertEqual(requests, {"a": ["downstream"], "b": ["upstream", "final"]})
                tasks = self.status()["tasks"]
                events = [json.loads(line) for line in (self.run_dir / "events.jsonl").read_text().splitlines()]
                completions = [event for event in events if event["kind"] == "task_completed"]
                if upstream_exists:
                    launch.assert_not_called()
                    self.assertTrue(receipt["terminal"])
                    self.assertEqual(receipt["dispatched"], [])
                    expected = [("upstream", "b"), ("downstream", "a"), ("final", "b")]
                    self.assertEqual([(event["task_id"], event["host_id"]) for event in completions], expected)
                    for event in completions:
                        self.assertEqual(event["details"], {"attempt": 0, "skipped_existing": True, "prechecked": True})
                    for task_id, host_id in expected:
                        task = tasks[task_id]
                        self.assertEqual((task["state"], task["completed_host"], task["attempts"]),
                                         ("completed", host_id, []))
                else:
                    launch.assert_called_once()
                    self.assertFalse(receipt["terminal"])
                    self.assertEqual([row["task_id"] for row in receipt["dispatched"]], ["upstream"])
                    self.assertEqual(completions, [])
                    for task_id in ("downstream", "final"):
                        self.assertEqual((tasks[task_id]["state"], tasks[task_id]["attempts"]), ("pending", []))

    def _configure_relay(self, target_host):
        self.config([
            self.task("parent", allowed_hosts=["a"]),
            self.task("child", allowed_hosts=[target_host], depends_on=["parent"],
                      cmd="cat {dep.parent.out_dir}/model/data {inputs_dir}/model",
                      retry_policy={"backoff_s": 1}, relay_inputs=[
                          {"from_task": "parent", "rel_path": "model/data",
                           "target_rel_path": "model", "max_bytes": 123},
                      ]),
            self.task("grandchild", depends_on=["child"]),
        ], hosts={"a": self.host(work_root="/source"), "b": self.host("b", work_root="/target")})
        self.initialize()
        self.tick()

    def test_relay_stages_target_paths_and_releases_state_lock(self):
        for target in ("a", "b"):
            with self.subTest(target=target):
                self.run_dir = self.root / target
                self.transport.attempts.clear()
                self._configure_relay(target)
                ready_states = []

                def relay(src, src_path, dst, dst_path, *, local_cache_path, max_bytes):
                    with (self.run_dir / "state.lock").open("rb") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    ready_states.append(self.status()["tasks"]["child"]["state"])
                    self.assertEqual((src.id, dst.id, max_bytes), ("a", "b", 123))
                    self.assertEqual(src_path, "/source/offline/outputs/parent/model/data")
                    self.assertEqual(dst_path, "/target/offline/relay_cache/parent/model/data")
                    self.assertEqual(local_cache_path, self.run_dir / "relay_cache/parent/model/data")
                    return {"ok": True}

                prior = len(self.transport.calls)
                with patch.object(self.transport, "relay_file", create=True, side_effect=relay) as transfer, \
                        patch.object(self.transport, "stage_local_input", create=True, return_value=True) as stage:
                    child = self.tick(2)["dispatched"][0]
                self.assertEqual(transfer.call_count, int(target == "b"))
                self.assertEqual(ready_states, ["relaying"] if target == "b" else [])
                self.assertEqual(child["task_id"], "child")
                parent = "/target/offline/relay_cache/parent" if target == "b" else "/source/offline/outputs/parent"
                inputs = f"{'/target' if target == 'b' else '/source'}/offline/inputs/child/model"
                self.assertEqual(stage.call_args.args[1:], (f"{parent}/model/data", inputs))
                calls = self.transport.calls[prior:]
                self.assertIn(f"{parent}/model/data {inputs}", next(row[-1] for row in calls if row[0] == "launch"))
                self.assertEqual(self.status()["tasks"]["child"]["state"], "running")
                events = [json.loads(row) for row in (self.run_dir / "events.jsonl").read_text().splitlines()]
                self.assertEqual([row["kind"] for row in events
                                  if row["task_id"] == "child" and row["kind"] != "task_ready"],
                                 ["relay_started", "relay_completed", "task_started"] if target == "b"
                                 else ["task_started"])

    def test_relay_failures_preserve_retry_policy_and_block_descendants(self):
        for retryable in (False, True):
            with self.subTest(retryable=retryable):
                self.run_dir = self.root / str(retryable)
                self.transport.attempts.clear()
                self._configure_relay("b")
                reason = "relay_source_unreachable" if retryable else "relay_exceeds_max_bytes"
                error = RelayError("Relay failed.", reason=reason, retryable=retryable)
                with patch.object(self.transport, "relay_file", create=True, side_effect=error):
                    self.assertEqual(self.tick(2)["dispatched"], [])
                status = self.status()
                child = status["tasks"]["child"]
                self.assertEqual(child["attempts"][0]["failure_kind"], "infra_error")
                self.assertEqual(child["state"], "ready" if retryable else "failed")
                self.assertEqual(status["tasks"]["grandchild"]["state"], "pending" if retryable else "blocked_dep")
                if retryable:
                    with patch.object(self.transport, "relay_file", create=True, return_value={"ok": True}), \
                            patch.object(self.transport, "stage_local_input", create=True, return_value=True):
                        self.assertEqual(self.tick(121)["dispatched"][0]["attempt"], 2)

    def test_dispatch_registry_prunes_and_isolates_plugin_data(self):
        self.config([self.task("job")])
        plugin = Path(os.environ["PLUGIN_DATA"])
        plugin.mkdir()
        registry = plugin / "dispatch_runs.json"
        old = self.root / "old"
        old.mkdir()
        registry.write_text(json.dumps({"runs": {
            "old": {"run_dir": str(old), "terminal": True, "updated_at": 1},
            "missing": {"run_dir": str(self.root / "missing"), "updated_at": 1},
        }}))
        self.initialize()
        entries = json.loads(registry.read_text())["runs"]
        self.assertEqual(list(entries), [str(self.run_dir.resolve())])
        self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)
        self.assertEqual(entries[str(self.run_dir.resolve())]["task_counts"], {"ready": 1})
        isolated = self.root / "isolated"
        with patch.dict(os.environ, {"PLUGIN_DATA": str(isolated), "CODEX_ORCH_DISABLE_DISPATCH_REGISTRY": "1"}):
            self.tick()
            self.assertFalse(isolated.exists())
        self.assertEqual(json.loads(registry.read_text())["runs"], entries)

    def test_schema_and_inventory(self):
        reference = SKILL / "references"
        for name in ("cluster-inventory-schema.json", "dispatch-manifest-schema.json"):
            with self.subTest(name=name):
                schema, error = preflight_schema(reference / name)
                self.assertIsNone(error)
                self.assertIsInstance(schema, dict)
        example = reference / "cluster-inventory-example.json"
        schema, _ = preflight_schema(reference / "cluster-inventory-schema.json")
        self.assertTrue(matches_schema(json.loads(example.read_text()), schema))
        self.assertTrue(load_cluster_config(example).hosts)

    def test_validation_rejects_bad_inputs_before_creating_run(self):
        cases = {
            "cycle": ([self.task("x", depends_on=["y"]), self.task("y", depends_on=["x"])], {}),
            "unknown dependency": ([self.task("x", depends_on=["y"])], {}),
            "duplicate": ([self.task("x"), self.task("x")], {}),
            "placeholder": ([self.task("x", cmd="echo {absent}")], {}),
            "marker traversal": ([self.task("x", done_markers=["../marker"])], {}),
            "proxy command": ([self.task("x")], {"ssh_options": ["ProxyCommand=false"]}),
            "local command": ([self.task("x")], {"ssh_options": ["LocalCommand=false"]}),
            "relative root": ([self.task("x")], {"work_root": "relative"}),
        }
        for case, (tasks, options) in cases.items():
            with self.subTest(case=case):
                self.config(tasks, {"a": self.host(**options)})
                with self.assertRaises(ValidationError):
                    self.initialize()
                self.assertFalse(self.run_dir.exists())
        args = ["--cluster", self.cluster, "--manifest", self.manifest]
        self.assertEqual(json.loads(self.shim("validate", *args, expected=2))["exit_code"], 2)
        self.assertEqual(json.loads(self.shim("start", *args, "--run-dir", self.run_dir, expected=2))["exit_code"], 2)
        self.assertFalse(self.run_dir.exists())
        self.shim("start", "--cluster", self.root / "missing", "--manifest", self.manifest,
                  "--run-dir", self.run_dir, expected=2)
        self.assertFalse(self.run_dir.exists())

    def test_cli_local_plan_validate_status_and_path_precedence(self):
        self.config([self.task("fast"), self.task("long", 9),
                     self.task("root"), self.task("child", depends_on=["root"])])
        args = ["--cluster", self.cluster, "--manifest", self.manifest]
        with patch.dict(os.environ, {"CODEX_ORCH_CLUSTER_CONFIG": str(self.cluster)}):
            self.assertEqual(resolve_cluster_path(None), self.cluster)
            self.assertEqual(resolve_cluster_path(str(self.manifest)), self.manifest)
            with self.assertRaises(ValidationError):
                resolve_cluster_path(str(self.root))
            self.assertTrue(json.loads(self.shim("validate", "--manifest", self.manifest))["ok"])
        with patch.dict(os.environ, {"CODEX_ORCH_CLUSTER_CONFIG": ""}), patch.object(Path, "home", return_value=self.root):
            with self.assertRaises(ValidationError):
                resolve_cluster_path(None)
            default = self.root / ".config" / "codex-orchestrator" / "clusters.json"
            default.parent.mkdir(parents=True)
            default.write_text(self.cluster.read_text())
            del os.environ["CODEX_ORCH_CLUSTER_CONFIG"]
            self.assertEqual(resolve_cluster_path(None), default)
        plan = json.loads(self.shim("plan", *args))
        self.assertEqual(plan["execution_order"][:2], ["root", "long"])
        self.assertEqual(plan["eligible_hosts"]["child"], ["a", "b"])
        self.assertEqual(self.transport.calls, [])
        live = self.controller.plan(self.cluster, self.manifest, live_probe=True)
        self.assertEqual(len(live["host_gpu_summary"]["a"]["gpus"]), 8)
        self.assertEqual(live["host_gpu_summary"]["b"]["disk_free_gb"], 100)
        self.initialize()
        self.cluster.unlink()
        self.manifest.unlink()
        self.assertEqual(json.loads(self.shim("status", "--run-dir", self.run_dir))["run_id"], "offline")
        self.assertIn("task_counts", self.shim("--format", "table", "status", "--run-dir", self.run_dir))
        self.assertEqual(stat.S_IMODE(SHIM.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.run_dir.stat().st_mode), 0o700)
        for name in ("state.lock", "state.json", "summary.json", "events.jsonl",
                     "cluster.snapshot.json", "manifest.snapshot.json"):
            self.assertEqual(stat.S_IMODE((self.run_dir / name).stat().st_mode), 0o600)

    def test_heterogeneous_work_stealing_beats_static_split(self):
        durations = [5] * 8 + [3] * 8 + [2] * 4 + [1] * 4
        tasks = [self.task(f"task-{i:02}", duration) for i, duration in enumerate(durations)]
        self.config(tasks)
        self.initialize()
        start = self.clock()
        first = self.tick()["dispatched"]
        self.assertEqual([entry["task_id"] for entry in first[:8]], [t["id"] for t in tasks[:8]])
        self.assertEqual([entry["host_id"] for entry in first], ["a"] * 8 + ["b"] * 8)
        for index in range(1, 25):
            tick = self.tick(1)
            if index == 5:
                self.assertEqual(len(tick["dispatched"]), 8)
                self.assertTrue(all(row["host_id"] == "a" for row in tick["dispatched"]))
                self.assertTrue(all(row["ends"] > self.clock() for row in
                                    list(self.transport.attempts.values())[8:16]))
            if tick["terminal"]:
                break
        self.assertTrue(tick["terminal"])

        # Each host gets twelve static jobs in manifest order and fills its eight slots greedily.
        def static_end(group, ticks_per_unit):
            slots = [0] * 8
            for duration in group:
                first_free = slots.index(min(slots))
                slots[first_free] += ticks_per_unit * duration
            return max(slots)

        by_host = {host: sum(row["host"] == host for row in self.transport.attempts.values())
                   for host in ("a", "b")}
        self.assertEqual(by_host, {"a": 16, "b": 8})
        self.assertEqual(self.clock() - start, 7)
        static_makespan = max(static_end(durations[:12], 1), static_end(durations[12:], 2))
        self.assertEqual(static_makespan, 8)

    def test_dag_failure_isolation_and_dependency_affinity(self):
        self.config([
            self.task("fail", retry_policy={"max_retries": 2, "non_retryable_log_patterns": ["non-finite"]}),
            self.task("blocked", depends_on=["fail"]),
            self.task("independent", allowed_hosts=["b"]),
            self.task("child", depends_on=["independent"], host_affinity="prefer_dep_host",
                      cmd="echo {dep.independent.out_dir}"),
            self.task("aggregate", depends_on=["blocked", "child"], allow_partial_deps=True),
        ])
        self.transport.failures["fail", 1] = "nonfinite"
        self.initialize()
        self.tick()
        for _ in range(8):
            self.tick(1)
        summary = self.status()["tasks"]
        self.assertEqual(summary["fail"]["state"], "failed")
        self.assertEqual(len(summary["fail"]["attempts"]), 1)
        self.assertEqual(summary["blocked"]["state"], "blocked_dep")
        self.assertEqual(summary["child"]["completed_host"], "b")
        self.assertEqual(summary["independent"]["state"], "completed")
        self.assertEqual(summary["aggregate"]["state"], "completed")
        self.assertIn("/outputs/independent", next(c[-1] for c in self.transport.calls if c[:2] == ("launch", "child")))

    def test_infrastructure_failure_retries_on_other_host(self):
        for failure in ("ecc", "vanished"):
            with self.subTest(failure=failure):
                self.run_dir = self.root / failure
                self.transport.attempts.clear()
                self.config([self.task("task", retry_policy={"backoff_s": 0})])
                self.transport.failures["task", 1] = failure
                self.initialize()
                self.assertEqual(self.tick()["dispatched"][0]["host_id"], "a")
                self.assertEqual(self.tick(1)["dispatched"][0]["host_id"], "b")
                runtime = self.status()["tasks"]["task"]
                self.assertEqual(runtime["attempts"][0]["failure_kind"], "infra_error")
                self.assertGreater(runtime["failed_infra_hosts"]["a"], self.clock())
                self.assertTrue(self.tick(2)["terminal"])

    def test_skip_gpu_and_cross_container_vram(self):
        self.config([self.task(f"task-{i}", 100) for i in range(8)], {"a": self.host()})
        self.transport.external = {("a", 3): (8700, [88]), ("a", 5): (8700, [99])}
        self.initialize()
        first = self.tick()
        self.assertEqual([a["gpu_ids"][0] for a in first["dispatched"]], [0, 1, 2, 4, 6, 7])
        self.transport.external = {("a", 3): (8700, []), ("a", 0): (18000, [])}
        second = self.tick(1)
        self.assertEqual([a["gpu_ids"] for a in second["dispatched"]], [[5]])
        self.assertFalse(second["preempted"])
        self.assertEqual(second["task_counts"], {"ready": 1, "running": 7})

    def test_external_preempt_gpu_and_yield_host(self):
        for policy in ("preempt_gpu", "yield_host"):
            with self.subTest(policy=policy):
                self.run_dir = self.root / policy
                self.transport.attempts.clear()
                self.transport.external.clear()
                self.config([self.task("one", 100), self.task("two", 100)], {
                    "a": self.host(count=2, external_gpu_policy=policy, external_grace_ticks=2,
                                   yield_preempt_running=True),
                    "b": self.host("b", count=2),
                })
                self.initialize()
                self.tick()
                self.transport.external["a", 0] = 8700, [999]
                self.assertEqual(self.tick(1)["preempted"], [])
                receipt = self.tick(1)
                expected = {"one"} if policy == "preempt_gpu" else {"one", "two"}
                self.assertEqual({a["task_id"] for a in receipt["preempted"]}, expected)
                self.assertEqual({a["task_id"] for a in receipt["dispatched"]}, expected)
                self.assertTrue(all(a["host_id"] == "b" for a in receipt["dispatched"]))
                self.assertEqual(self.status()["hosts"]["a"]["cordoned"], policy == "yield_host")
                self.transport.external.clear()
                self.tick(1)
                self.assertFalse(self.status()["hosts"]["a"]["cordoned"])

    def test_unreachable_backoff_reconnect_and_existing_marker(self):
        self.config([self.task("existing", done_markers=["result"]), self.task("running", 200),
                     self.task("finishes", 40)], {"a": self.host(count=3)})
        self.transport.existing.add("existing")
        self.initialize()
        self.tick()
        self.tick(1)
        self.assertEqual(self.status()["tasks"]["existing"]["state"], "completed")
        self.transport.unreachable.add("a")
        self.tick(1)
        self.tick(1)
        self.tick(1)
        host = self.status()["hosts"]["a"]
        self.assertEqual(host["state"], "unreachable")
        self.assertEqual(host["next_probe_at"], self.clock() + 30)
        calls = len(self.transport.calls)
        self.tick(29)
        self.assertEqual(len(self.transport.calls), calls)
        self.tick(1)
        self.assertEqual(self.status()["hosts"]["a"]["next_probe_at"], self.clock() + 60)
        self.transport.unreachable.clear()
        self.tick(60)
        self.assertEqual(self.status()["tasks"]["running"]["state"], "running")
        self.assertEqual(self.status()["tasks"]["finishes"]["state"], "completed")
        self.assertEqual(len(self.transport.attempts), 3)
        self.assertTrue(self.tick(200)["terminal"])

    def test_disk_full_guard(self):
        self.config([self.task("task")], {"a": self.host(min_disk_free_gb=10)})
        self.transport.disk["a"] = 9
        self.initialize()
        self.assertEqual(self.tick()["dispatched"], [])
        self.assertEqual(self.status()["hosts"]["a"]["state"], "disk_full")
        self.transport.disk["a"] = 10
        self.assertEqual(len(self.tick(1)["dispatched"]), 1)

    def test_failed_yield_kill_keeps_lease_and_retries(self):
        self.config([self.task("parent", 30, retry_policy={"max_preempt_retries": 0}),
                     self.task("child", depends_on=["parent"])],
                    {"a": self.host(count=1, external_gpu_policy="yield_host",
                                    external_grace_ticks=2, yield_preempt_running=True),
                     "b": self.host("b", count=1)})
        self.initialize()
        self.tick()
        self.transport.external["a", 0] = 8700, [50]
        self.tick(1)
        with patch.object(self.transport, "kill_pgid", return_value=self.transport.result(False)):
            result = self.tick(1)
        self.assertEqual(result["task_counts"], {"pending": 1, "running": 1})
        self.assertEqual(result["dispatched"], [])
        self.assertTrue(self.status()["hosts"]["a"]["cordoned"])
        self.assertTrue(self.tick(1)["terminal"])
        self.assertEqual(self.status()["task_counts"], {"blocked_dep": 1, "failed": 1})

    def test_concurrent_ticks_probe_in_parallel_and_launch_once(self):
        self.config([self.task("task", 10)])
        self.initialize()
        original = self.transport.probe
        parallel = threading.Barrier(2)

        def probe(host, attempts):
            with (self.run_dir / "state.lock").open("r") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                parallel.wait(timeout=3)
            return original(host, attempts)

        with patch.object(self.transport, "probe", side_effect=probe):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ticks = [pool.submit(self.controller.run_tick, self.run_dir) for _ in range(2)]
                dispatches = [future.result(timeout=8)["dispatched"] for future in ticks]
        self.assertEqual(sorted(map(len, dispatches)), [0, 1])
        self.assertEqual(self.status()["task_counts"], {"running": 1})

    def test_dispatch_before_mirror_and_pull_with_exclusions(self):
        self.config([self.task("first", pull_to_local=True), self.task("second", depends_on=["first"])],
                    {"a": self.host(count=1, mirror_root="/remote/mirror")})
        self.initialize()
        self.tick()
        self.transport.calls.clear()
        receipt = self.tick(1)
        self.assertEqual([call[0] for call in self.transport.calls], ["probe", "launch", "mirror", "pull"])
        mirror, pull = self.transport.calls[-2:]
        self.assertEqual(mirror[2]["mirror_dir"], "/remote/mirror/offline/first")
        self.assertIn("*.pt", mirror[2]["excludes"])
        self.assertIn("*.pt", pull[2]["excludes"])
        self.assertEqual(pull[2]["local_dest_dir"], self.run_dir / "results" / "first")
        self.assertTrue(receipt["pulled"][0]["ok"])
        self.assertTrue(self.status()["pulled_tasks"]["first"]["ok"])
        destination = self.root / "explicit-output"
        self.assertTrue(self.controller.pull(self.run_dir, task_id="first", dest=destination)["ok"])
        self.assertEqual(self.transport.calls[-1][2]["local_dest_dir"], destination)

    def test_async_pull_does_not_block_next_tick_or_hold_state_lock(self):
        self.config([self.task("first", pull_to_local=True), self.task("next", 5, depends_on=["first"])],
                    {"a": self.host(count=1)})
        self.initialize()
        self.tick()
        original = self.transport.pull_outputs_tar
        entered, release = threading.Event(), threading.Event()

        def delayed_pull(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release the simulated transfer.")
            return original(*args, **kwargs)

        with patch.object(self.transport, "pull_outputs_tar", side_effect=delayed_pull):
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    first = self.tick(1, sync_pulls=False, pull_executor=pool)
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(first["dispatched"][0]["task_id"], "next")
                    self.assertTrue(self.tick(5, sync_pulls=False, pull_executor=pool)["terminal"])
                    self.assertEqual(self.status()["pulled_tasks"], {})
                finally:
                    release.set()
        self.assertTrue(self.status()["pulled_tasks"]["first"]["ok"])

    def test_operator_cordon_cancel_requeue_and_stop(self):
        self.config([self.task("job", 100), self.task("child", depends_on=["job"]),
                     self.task("bad", retry_policy={"max_retries": 2, "non_retryable_log_patterns": ["non-finite"]}),
                     self.task("blocked", depends_on=["bad"]), self.task("stop-job", 100)])
        self.transport.failures["bad", 1] = "nonfinite"
        self.initialize()
        assignments = {a["task_id"]: a for a in self.tick()["dispatched"]}
        self.tick(1)
        job = assignments["job"]
        summary = self.controller.cordon(self.run_dir, host_id=job["host_id"], gpu=job["gpu_ids"][0],
                                         preempt=True, reason="Bearer abcdef123456")
        self.assertEqual(summary["tasks"]["job"]["state"], "ready")
        self.assertEqual(summary["tasks"]["stop-job"]["state"], "running")
        self.assertEqual(summary["tasks"]["job"]["attempts"][0]["failure_kind"], "preempted_external")
        self.assertEqual(summary["hosts"]["a"]["cordoned_gpus"], job["gpu_ids"])
        self.assertEqual(self.controller.uncordon(self.run_dir, host_id="a", gpu=job["gpu_ids"][0])
                         ["hosts"]["a"]["cordoned_gpus"], [])
        self.tick()
        self.assertEqual(self.controller.cancel(self.run_dir, task_id="job")["tasks"]["child"]["state"],
                         "blocked_dep")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["requeue", "--run-dir", str(self.run_dir), "--failed"]), 0)
        self.assertEqual(self.status()["tasks"]["bad"]["state"], "ready")
        self.assertEqual(self.status()["tasks"]["blocked"]["state"], "pending")
        receipt = self.controller.stop(self.run_dir, kill_remote=True)
        self.assertTrue(receipt["ok"])
        self.assertTrue((self.run_dir / "stop.flag").is_file())
        self.assertEqual(len([call for call in self.transport.calls if call[0] == "kill"]), 3)
        events = (self.run_dir / "events.jsonl").read_text()
        self.assertNotIn("abcdef123456", events)
        self.assertIn("[REDACTED]", events)

    def test_start_exit_six_for_preflight_and_probe_failures(self):
        for mode in ("preflight", "unreachable"):
            with self.subTest(mode=mode):
                self.run_dir = self.root / mode
                self.config([self.task("job")], preflight={"cmd": "echo {work_root}", "timeout_s": 12})
                failed = self.transport.preflight_failures if mode == "preflight" else self.transport.unreachable
                failed.update(("a", "b"))
                with patch("codex_orch.remote.controller.RemoteController", return_value=self.controller):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = main(["start", "--cluster", str(self.cluster), "--manifest",
                                          str(self.manifest), "--run-dir", str(self.run_dir)])
                self.assertEqual(exit_code, 6)
                self.assertFalse(json.loads(stdout.getvalue())["ok"])
                self.assertFalse((self.run_dir / "controller.pid").exists())
                self.assertEqual(self.status()["task_counts"], {"ready": 1})
                failed.clear()

    def test_sync_failure_and_skipping_preflight(self):
        self.config([self.task("job")], {"a": self.host(vars={"src": "/remote/source"}),
                                         "b": self.host("b", vars={"src": "/remote/source"})},
                    sync={"local_dir": ".", "remote_var": "src", "excludes": ["*.pt"]},
                    preflight={"cmd": "false"})
        self.transport.sync_failures.add("b")
        summary = self.controller.initialize_run(self.run_dir, self.cluster, self.manifest,
                                                 sync=True, skip_preflight=True)
        self.assertTrue(summary["hosts"]["a"]["preflight_ok"])
        self.assertEqual(summary["hosts"]["b"]["state"], "preflight_failed")
        self.assertEqual([call[0] for call in self.transport.calls], ["sync", "sync"])
        self.assertEqual(self.transport.calls[0][2]["local_dir"], self.root)
        self.assertEqual(self.transport.calls[0][2]["excludes"], ("*.pt",))

    def test_detached_start_first_tick_and_resume(self):
        self.config([self.task("job", 5)])
        with patch("codex_orch.remote.controller.subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()
            receipt = self.controller.start_detached(self.run_dir, self.cluster, self.manifest)
            self.assertEqual(receipt["dispatched"][0]["task_id"], "job")
            invocation = popen.call_args
            self.assertIn("-I", invocation.args[0])
            self.assertIn("-B", invocation.args[0])
            self.assertTrue(invocation.kwargs["start_new_session"])
            self.assertEqual(invocation.kwargs["stdin"], subprocess.DEVNULL)
            with self.assertRaises(ValidationError):
                self.controller.start_detached(self.run_dir, None, None, resume=True)
        (self.run_dir / "controller.pid").unlink()
        self.controller.stop(self.run_dir)
        self.clock.advance(5)
        receipt = self.controller.start_detached(self.run_dir, None, None, resume=True)
        self.assertIsNone(receipt["controller_pid"])
        self.assertEqual(receipt["task_counts"], {"completed": 1})
        self.assertFalse((self.run_dir / "stop.flag").exists())

    def test_foreground_signal_and_cleanup(self):
        self.config([self.task("job", 20)])
        self.initialize()
        before = signal.getsignal(signal.SIGTERM)
        durations = []

        def interrupt(seconds):
            durations.append(seconds)
            os.kill(os.getpid(), signal.SIGTERM)

        self.controller.sleeper = interrupt
        self.assertEqual(self.controller.run_loop(self.run_dir), 128 + signal.SIGTERM)
        self.assertEqual(durations, [0.5])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse((self.run_dir / "controller.pid").exists())
        self.assertFalse(self.status()["controller_alive"])
        self.assertIn(("close", ["a", "b"]), self.transport.calls)
        self.clock.advance(20)
        self.assertEqual(self.controller.run_loop(self.run_dir, max_ticks=1), 0)
        self.assertTrue(self.status()["terminal"])


class LocalTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="rd-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.host = HostConfig("a", "worker@a.example.invalid", str(self.root))

    def test_control_socket_length_and_mode(self):
        manager = ControlSocketManager(self.root / "sockets")
        self.assertLess(len(os.fsencode(manager.control_path().replace("%C", "0" * 40))), 104)
        self.assertEqual(stat.S_IMODE(manager.sock_dir.stat().st_mode), 0o700)

    def test_relay_hash_cache_resume_restart_and_limit(self):
        def loopback(argv, **kwargs):
            self.assertFalse(kwargs["shell"])
            return subprocess.run(shlex.split(argv[-1]), **kwargs)

        transport = SshTransport(socket_dir=self.root / "sockets", runner=loopback)
        destination_host = HostConfig("b", "worker@b.example.invalid", str(self.root))
        content = bytes(range(256)) * 400
        digest = hashlib.sha256(content).hexdigest()
        source = self.root / "source data"
        source.write_bytes(content)
        cache = self.root / "cache/local"
        cache.parent.mkdir()
        target = self.root / "remote/destination"
        remote_part = target.with_suffix(".part")
        local_part = cache.with_suffix(".part")

        def transfer(**kwargs):
            return transport.relay_file(self.host, str(source), destination_host, str(target),
                                        local_cache_path=cache, **kwargs)

        def assert_transfer(downloaded, uploaded, *, hit=False):
            self.assertEqual(transfer(), {"ok": True, "cache_hit": hit, "size": len(content), "sha256": digest,
                                          "downloaded_bytes": downloaded, "uploaded_bytes": uploaded})
            self.assertEqual(compute_file_sha256(target), digest)
            self.assertFalse(remote_part.exists())

        assert_transfer(len(content), len(content))
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cache.parent.stat().st_mode), 0o700)
        assert_transfer(0, 0, hit=True)
        staged = self.root / "inputs/staged"
        self.assertTrue(transport.stage_local_input(destination_host, str(target), str(staged)))
        self.assertEqual(staged.read_bytes(), content)
        for valid in (True, False):
            with self.subTest(valid=valid):
                target.unlink()
                cache.unlink()
                local_part.write_bytes(content[:701] if valid else b"x" * 701)
                remote_part.write_bytes(content[:999] if valid else b"x" * 999)
                assert_transfer(len(content) - (701 if valid else 0), len(content) - (999 if valid else 0))
                self.assertFalse(local_part.exists())
        target.write_bytes(b"x" * len(content))
        assert_transfer(0, len(content))
        with self.assertRaises(RelayError) as exceeded:
            transfer(max_bytes=len(content) - 1)
        self.assertEqual((exceeded.exception.reason, exceeded.exception.retryable),
                         ("relay_exceeds_max_bytes", False))
        source.unlink()
        source.mkdir()
        with self.assertRaises(RelayError) as missing:
            transfer()
        self.assertEqual((missing.exception.reason, missing.exception.retryable), ("relay_missing_source", False))

    @staticmethod
    def archive(entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    member.linkname = content
                    archive.addfile(member)
        return output.getvalue()

    def test_tar_safety_and_valid_outputs(self):
        for name, kind, content in [
            ("../escape", tarfile.REGTYPE, b"bad"),
            ("/etc/passwd", tarfile.REGTYPE, b"bad"),
            ("symlink", tarfile.SYMTYPE, "../escape"),
            ("hardlink", tarfile.LNKTYPE, "target"),
            ("big", tarfile.REGTYPE, b"x" * (1024 * 1024 + 1)),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    safe_extract_tar_bytes(self.archive([(name, kind, content)]), self.root / "bad", max_mb=1)
        self.assertFalse((self.root / "escape").exists())
        outputs = {"diag_results.json": b"{}", "per_window_metrics.npz": b"test data"}
        safe_extract_tar_bytes(self.archive([(name, tarfile.REGTYPE, content)
                                             for name, content in outputs.items()]), self.root / "safe")
        for name, content in outputs.items():
            self.assertEqual((self.root / "safe" / name).read_bytes(), content)

    def test_local_wrapper_cuda_and_done_marker_skip(self):
        transport = SshTransport(socket_dir=self.root / "sockets")
        task = TaskSpec("job", "unused", done_markers=("diag_results.json",))
        out_dir = self.root / "out"
        out_dir.mkdir()
        for n in (1, 2):
            attempt_dir = self.root / f"attempt-{n}"
            attempt_dir.mkdir()
            cmd = ('printf "%s" "$CUDA_VISIBLE_DEVICES" > "$OUT_DIR/diag_results.json"'
                   if n == 1 else "exit 99")
            env, wrapper = transport.build_remote_wrapper(
                host=self.host, task=task, run_id="local", out_dir=str(out_dir),
                attempt_dir=str(attempt_dir), gpu_ids=[3, 6], rendered_cmd=cmd,
            )
            (attempt_dir / "env.sh").write_text(env)
            subprocess.run(["sh", "-s"], input=wrapper, text=True, check=True, timeout=3)
            done = json.loads((attempt_dir / "done.json").read_text())
            self.assertEqual(done["exit_code"], 0)
            self.assertEqual(done["skipped_existing"], n == 2)
            self.assertEqual(list(attempt_dir.glob("*done*")), [attempt_dir / "done.json"])
            self.assertEqual((out_dir / "diag_results.json").read_text(), "3,6")

    def test_local_probe_cpu_and_nvidia_unsupported_fields(self):
        def execute(host, env=None):
            request = build_probe_request(host, [])
            result = subprocess.run([sys.executable, "-I", "-B", "-c", REMOTE_PROBE_SCRIPT],
                                    input=json.dumps(request), text=True, capture_output=True,
                                    env=env, timeout=4, check=True)
            return parse_probe_response(host, result.stdout)

        cpu = execute(HostConfig("cpu", self.host.ssh_target, str(self.root), accelerator="cpu"))
        self.assertTrue(cpu.ok)
        self.assertEqual(cpu.gpus, [])
        self.assertGreaterEqual(cpu.disk_free_gb, 0)
        smi = self.root / "nvidia-smi"
        smi.write_text("""#!/bin/sh
case "$1" in
    --query-gpu=*) printf '0, GPU-1, 24000, 8700, [Not Supported]\n' ;;
    --query-compute-apps=*) printf 'GPU-1, [Not Supported], 8700\n' ;;
esac
""")
        smi.chmod(0o755)
        gpu = execute(self.host, {**os.environ, "PATH": f"{self.root}{os.pathsep}{os.defpath}"})
        self.assertTrue(gpu.ok)
        self.assertEqual(gpu.gpus[0].memory_used_mb, 8700)
        self.assertEqual(gpu.gpus[0].utilization_pct, 0)
        self.assertEqual(gpu.gpus[0].external_pids, [])
        self.assertTrue(gpu.gpus[0].external_busy)


class SchedulerCoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def host(self, host_id="a", **options):
        return HostConfig(host_id, f"worker@{host_id}.example.invalid", str(self.root / host_id), **options)

    def runtime(self, hosts, tasks):
        cluster = ClusterConfig({host.id: host for host in hosts})
        manifest = DispatchManifest(tuple(tasks), run_id="offline")
        specs = {task.id: task for task in manifest.tasks}
        state = init_run_state(cluster, manifest, cluster_path="cluster.json", manifest_path="tasks.json", now=0)
        advance_dag_states(state, specs, 0)
        return cluster, manifest, specs, state

    def test_probe_request_and_remote_command_only_forward_allowlisted_host_env(self):
        runtime_env = {
            "PATH": "/usr/local/PPU_SDK/CUDA_SDK/bin:/usr/bin",
            "LD_LIBRARY_PATH": "/usr/local/PPU_SDK/CUDA_SDK/lib64:/usr/local/PPU_SDK/lib",
            "PPU_SDK": "/usr/local/PPU_SDK",
        }
        host = self.host(env={**runtime_env, "OPENAI_API_KEY": "secret-key", "HF_TOKEN": "secret-token"})
        request = build_probe_request(host, [])
        self.assertEqual(request["env"], runtime_env)
        transport = Mock(spec=SshTransport)
        transport.run_remote.return_value = SshResult(True, 0, "{}", "", 0.0)
        self.assertTrue(probe_host(transport, host, []).ok)
        args, kwargs = transport.run_remote.call_args
        self.assertEqual(args[0], host)
        self.assertEqual(kwargs, {"timeout_s": 25.0})
        sent_request = json.loads(base64.b64decode(shlex.split(args[1])[-1]))
        self.assertEqual(sent_request, request)
        for secret in ("OPENAI_API_KEY", "HF_TOKEN", "secret-key", "secret-token"):
            self.assertNotIn(secret, json.dumps(sent_request))

    def test_ppu_probe_resolves_nvidia_smi_from_host_env_and_accounts_for_managed_holders(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        smi = bin_dir / "nvidia-smi"
        smi.write_text(r"""#!/bin/sh
[ "$2" = "--format=csv,noheader,nounits" ] || exit 2
case "$1" in
    --query-gpu=index,uuid,memory.total,memory.used,utilization.gpu)
        printf '0, PPU-1, 32768, 4600, 37\n' ;;
    --query-compute-apps=gpu_uuid,pid,used_gpu_memory)
        printf 'PPU-1, 901, 3000\nPPU-1, 902, 1600\n' ;;
    *) exit 2 ;;
esac
""")
        smi.chmod(0o755)
        proc = self.root / "proc"
        descriptors = proc / "21" / "fd"
        descriptors.mkdir(parents=True)
        (descriptors / "3").symlink_to("/dev/alixpu_ppu0")

        host = self.host(accelerator="ppu", env={"PATH": str(bin_dir), "PPU_SDK": str(self.root)})
        attempt = {"task_id": "job", "attempt": 1, "pgid": 20, "attempt_dir": str(self.root),
                   "out_dir": str(self.root), "done_markers": [], "gpu_ids": [0]}
        request = build_probe_request(host, [attempt])
        namespace = {}
        exec(REMOTE_PROBE_SCRIPT, namespace)
        holders = namespace["count_managed_gpu_holders"]
        namespace["count_managed_gpu_holders"] = lambda processes, pgids, index: holders(
            processes, pgids, index, proc,
        )
        namespace["parse_processes"] = lambda: {21: 20}
        output = io.StringIO()
        namespace["print"] = output.write
        with patch.dict(os.environ, {"PATH": str(self.root)}, clear=True):
            namespace["main"](request)
            self.assertEqual(os.environ["PATH"], str(self.root))
            self.assertNotIn("PPU_SDK", os.environ)
            with patch.dict(namespace, {"parse_processes": Mock(side_effect=RuntimeError)}):
                with self.assertRaises(RuntimeError):
                    namespace["main"](request)
            self.assertEqual(os.environ["PATH"], str(self.root))
            self.assertNotIn("PPU_SDK", os.environ)

        raw = output.getvalue()
        self.assertNotIn("probe_warning", json.loads(raw))
        snapshot = parse_probe_response(host, raw)
        self.assertTrue(snapshot.ok)
        self.assertEqual(snapshot.gpus, [
            GpuSlotSnapshot(0, memory_total_mb=32768, memory_used_mb=4600, utilization_pct=37,
                            external_pids=[902], external_busy=True),
        ])

    def test_ppu_probe_falls_back_and_honors_custom_command(self):
        namespace = {}
        exec(REMOTE_PROBE_SCRIPT, namespace)
        gpu_query = namespace["GPU_QUERY"]
        custom_output = '[{"index": 2, "memory_used_mb": 34}]'
        custom = f"printf '%s' {shlex.quote(custom_output)}"
        for command, responses, expected_calls, expected_slot, warning in (
            (None, [None, "1, PPU-2, 32768, 1200, 14"],
             [["nvidia-smi", *gpu_query], ["ppu-smi", *gpu_query]],
             GpuSlotSnapshot(1, memory_total_mb=32768, memory_used_mb=1200, utilization_pct=14), None),
            (custom, [custom_output],
             [["/bin/sh", "-c", custom]], GpuSlotSnapshot(2, memory_used_mb=34), None),
            (custom, [None], [["/bin/sh", "-c", custom]], GpuSlotSnapshot(2),
             "PPU probe returned no GPU inventory. Configured GPU indices were used."),
        ):
            with self.subTest(command=command, responses=responses):
                host = self.host(accelerator="ppu", ppu_probe_cmd=command, gpu_ids=(expected_slot.index,))
                namespace["parse_processes"] = lambda: {}
                namespace["capture"] = Mock(side_effect=responses)
                output = io.StringIO()
                namespace["print"] = output.write
                namespace["main"](build_probe_request(host, []))
                self.assertEqual([call.args[0] for call in namespace["capture"].call_args_list], expected_calls)
                raw = output.getvalue()
                self.assertEqual(json.loads(raw).get("probe_warning"), warning)
                self.assertEqual(parse_probe_response(host, raw).gpus, [expected_slot])

    def test_pid_namespace_probe_accounts_for_managed_device_holders(self):
        host = self.host()
        namespace = {}
        exec(REMOTE_PROBE_SCRIPT, namespace)
        proc = self.root / "proc"
        for pid in (21, 33):
            directory = proc / str(pid) / "fd"
            directory.mkdir(parents=True)
            for fd in ("3", "4"):
                (directory / fd).symlink_to("/dev/nvidia0")
        (proc / "21" / "fd" / "5").symlink_to("/dev/nvidiactl")
        holders = namespace["count_managed_gpu_holders"]
        self.assertEqual(holders({21: 20, 33: 30, 34: 20}, {20}, 0, proc), 1)
        self.assertEqual(holders({21: 20}, {20}, 1, proc), 0)
        self.assertEqual(holders({21: 20}, {20}, 0, self.root / "missing"), 0)
        namespace["count_managed_gpu_holders"] = lambda processes, pgids, index: holders(
            processes, pgids, index, proc,
        )
        namespace["parse_processes"] = lambda: {21: 20, 33: 30}
        namespace["kill0_pgid"] = lambda pgid: False
        attempt = {"task_id": "job", "attempt": 1, "pgid": 20, "attempt_dir": str(self.root),
                   "out_dir": str(self.root), "done_markers": []}
        for lease, pgid, rows, pids, busy in (
            ([0], 20, "GPU-1, 901, 2000", [], False),
            ([0], 20, "GPU-1, 901, 2000\nGPU-1, 902, 2000", [902], True),
            ([0], 20, "GPU-1, 901, 2000\nGPU-1, [Not Supported], 2000", [], True),
            ([0], 20, "GPU-1, 21, 2000\nGPU-1, 33, 2000", [33], False),
            ([0], 20, "GPU-1, [Not Supported], 0", [], False),
            (None, 20, "GPU-1, 901, 2000", [901], True),
            ([0], 40, "GPU-1, 901, 2000", [901], True),
        ):
            with self.subTest(lease=lease, pgid=pgid, rows=rows):
                namespace["capture"] = lambda args: (
                    "0, GPU-1, 24000, 4000, 0" if args[1].startswith("--query-gpu") else rows
                )
                entry = {**attempt, "pgid": pgid}
                if lease is not None:
                    entry["gpu_ids"] = lease
                request = build_probe_request(host, [entry])
                self.assertEqual(request["attempts"][0]["gpu_ids"], lease or [])
                output = io.StringIO()
                namespace["print"] = lambda value: output.write(value)
                namespace["main"](request)
                snapshot = parse_probe_response(host, output.getvalue())
                self.assertTrue(snapshot.ok)
                self.assertEqual(snapshot.gpus[0].external_pids, pids)
                self.assertIs(snapshot.gpus[0].external_busy, busy)

    def test_busy_without_pid_triggers_external_policies_on_leased_gpu(self):
        for policy in ("skip_gpu", "yield_host", "preempt_gpu"):
            with self.subTest(policy=policy):
                host = self.host(external_gpu_policy=policy, external_grace_ticks=1, yield_preempt_running=True)
                cluster, _, specs, state = self.runtime([host], [TaskSpec("job", "true")])
                state.hosts["a"].gpus = [GpuSlotSnapshot(0)]
                assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
                mark_dispatch_started(state, assignments[0], 0, pgid=20)
                observation = HostProbeSnapshot("a", True, gpus=[GpuSlotSnapshot(0, external_busy=True)])
                preempts, _, events = reconcile_host_probe(state, cluster, specs, observation, 1)
                self.assertEqual(state.hosts["a"].external_gpu_ticks, {"0": 1})
                if policy == "skip_gpu":
                    self.assertEqual([event.kind for event in events], ["external_collision"])
                    self.assertEqual(preempts, [])
                else:
                    self.assertEqual([action.reason for action in preempts], ["preempted_external"])
                if policy == "yield_host":
                    self.assertEqual(state.hosts["a"].state, "cordoned")
                    self.assertEqual(state.hosts["a"].cordon_reason, "external_yield")
                    reconcile_host_probe(state, cluster, specs, HostProbeSnapshot("a", True), 2)
                    self.assertEqual(state.hosts["a"].state, "ready")

    def test_ignore_gpu_policy_preserves_known_memory_headroom(self):
        host = self.host(external_gpu_policy="ignore", min_free_mem_mb=2048)
        runtime = HostRuntime(gpus=[
            GpuSlotSnapshot(0, 4096, 3000),
            GpuSlotSnapshot(1, 4096, 2048, 100, [999], True),
            GpuSlotSnapshot(2),
        ])
        self.assertEqual(compute_free_gpus(host, runtime, set(), 0), [1, 2])

    def test_marker_precheck_completes_on_observed_host_and_unlocks_dependencies(self):
        hosts = [self.host(), self.host("b", mirror_root=str(self.root / "mirror"))]
        cluster, _, specs, state = self.runtime(hosts, [
            TaskSpec("upstream", "true", done_markers=("nested/result", "summary"), pull_max_mb=32),
            TaskSpec("downstream", "true", depends_on=("upstream",), done_markers=("done",)),
            TaskSpec("no_markers", "true"),
        ])

        class ProbeTransport:
            def run_remote(self, host, command, *, timeout_s):
                result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout_s)
                return SshResult(result.returncode == 0, result.returncode, result.stdout, result.stderr, 0.0)

        _, _, events = reconcile_host_probe(state, cluster, specs, HostProbeSnapshot("a", True), 1)
        self.assertEqual(events, [])
        out_dir = Path(hosts[1].work_root) / "offline" / "outputs" / "upstream"
        (out_dir / "nested").mkdir(parents=True)
        (out_dir / "nested" / "result").touch()
        marker_checks = [
            {"task_id": "upstream", "out_dir": str(out_dir), "done_markers": ["nested/result", "summary"]},
            {"task_id": "no_markers", "out_dir": str(out_dir), "done_markers": []},
        ]
        cpu_host = self.host("b", accelerator="cpu", mirror_root=hosts[1].mirror_root)
        snapshot = probe_host(ProbeTransport(), cpu_host, [], marker_checks=marker_checks)
        self.assertTrue(snapshot.ok)
        self.assertEqual(snapshot.existing_done_tasks, [])
        (out_dir / "summary").touch()
        snapshot = probe_host(ProbeTransport(), cpu_host, [], marker_checks=marker_checks)
        self.assertEqual(snapshot.existing_done_tasks, ["upstream"])
        snapshot.existing_done_tasks[:0] = ["downstream", "unknown", "no_markers", "upstream"]
        _, pulls, events = reconcile_host_probe(state, cluster, specs, snapshot, 2)
        self.assertEqual([(event.kind, event.task_id) for event in events],
                         [("task_completed", "upstream"), ("task_ready", "downstream"),
                          ("task_completed", "downstream")])
        self.assertEqual(events[0].details, {"attempt": 0, "skipped_existing": True, "prechecked": True})
        self.assertEqual(state.tasks["upstream"].attempts, [])
        self.assertEqual(state.tasks["upstream"].completed_host, "b")
        self.assertEqual(state.tasks["upstream"].completed_out_dir, str(out_dir))
        self.assertEqual(state.tasks["downstream"].state, "completed")
        self.assertEqual(state.tasks["no_markers"].state, "ready")
        self.assertEqual([pull.task_id for pull in pulls], ["upstream", "downstream"])
        self.assertEqual((pulls[0].attempt, pulls[0].host_id, pulls[0].mirror_root, pulls[0].pull_max_mb),
                         (0, "b", hosts[1].mirror_root, 32))
        self.assertEqual(pulls[0].pull_excludes, list(specs["upstream"].pull_excludes))
        self.assertEqual(reconcile_host_probe(state, cluster, specs, snapshot, 3), ([], [], []))

    def test_non_retryable_failure_reasons_are_precise_with_zero_retry_budget(self):
        for exit_code, log, reason in (
            (42, "configuration denied", "non_retryable_exit_code"),
            (1, "configuration denied", "non_retryable_log_pattern"),
            (1, "ordinary error", "nonzero_or_missing_exit_code"),
        ):
            with self.subTest(exit_code=exit_code, log=log):
                task = TaskSpec("job", "true", gpus=0, retry_policy={
                    "max_retries": 0, "non_retryable_exit_codes": [42],
                    "non_retryable_log_patterns": ["configuration .*"],
                })
                cluster, _, specs, state = self.runtime([self.host(accelerator="cpu")], [task])
                assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
                mark_dispatch_started(state, assignments[0], 0)
                snapshot = HostProbeSnapshot("a", True, attempts={
                    "job": AttemptProbeStatus(done_payload={"exit_code": exit_code}, log_tail=log),
                })
                _, _, events = reconcile_host_probe(state, cluster, specs, snapshot, 1)
                self.assertEqual(events[0].kind, "task_failed")
                self.assertEqual(events[0].details["reason"], reason)
                self.assertEqual(events[0].details.get("non_retryable", False), reason.startswith("non_retryable"))

    def gang_runtime(self, size, *, gang_hosts=None):
        cluster, _, specs, state = self.runtime([self.host(), self.host("b", speed_weight=3)], [
            TaskSpec("short", "true", est_duration_s=10),
            TaskSpec("long", "true", est_duration_s=40),
            TaskSpec("gang", "true", gpus=size, allowed_hosts=gang_hosts),
            TaskSpec("single", "true"),
        ])
        for host_rt in state.hosts.values():
            host_rt.gpus = [GpuSlotSnapshot(index) for index in range(size)]
        for task_id, host_id in (("short", "a"), ("long", "b")):
            state.tasks[task_id].state = "running"
            state.tasks[task_id].attempts.append(TaskAttempt(1, host_id, [0], "/out", "/attempt", 0))
        return cluster, specs, state

    def test_gang_reserves_fastest_drain_and_dispatches_when_ready(self):
        for size in (4, 8):
            with self.subTest(size=size):
                cluster, specs, state = self.gang_runtime(size)
                assignments, events = plan_dispatches(state, cluster, specs, state.run_id, 2)
                self.assertEqual([(a.task_id, a.host_id) for a in assignments], [("single", "b")])
                self.assertEqual(state.hosts["a"].draining_for, "gang")
                self.assertEqual(state.hosts["a"].draining_since, 2)
                self.assertIsNone(state.hosts["b"].draining_for)
                self.assertEqual(events[0].kind, "host_draining")
                self.assertEqual(events[0].details, {"task_id": "gang", "gpus_needed": size, "est_drain_s": 8})
                mark_dispatch_started(state, assignments[0], 2)
                assignments, events = plan_dispatches(state, cluster, specs, state.run_id, 3)
                self.assertEqual((assignments, events), ([], []))
                state.tasks["short"].state = "completed"
                assignments, events = plan_dispatches(state, cluster, specs, state.run_id, 4)
                self.assertEqual([(a.task_id, a.host_id, a.gpu_ids) for a in assignments],
                                 [("gang", "a", list(range(size)))])
                self.assertEqual([(event.kind, event.host_id) for event in events], [("host_drain_released", "a")])
                self.assertIsNone(state.hosts["a"].draining_for)
                self.assertIsNone(state.hosts["a"].draining_since)
                state.hosts["b"].draining_for = "gang"
                state.hosts["b"].draining_since = 3
                mark_dispatch_started(state, assignments[0], 4)
                self.assertIsNone(state.hosts["b"].draining_for)
                self.assertIsNone(state.hosts["b"].draining_since)

    def test_gang_releases_invalid_reservations(self):
        for cause in ("external", "cancelled", "backoff", "cordoned", "disk_full", "preflight", "cooldown"):
            with self.subTest(cause=cause):
                cluster, specs, state = self.gang_runtime(4, gang_hosts=("a",))
                plan_dispatches(state, cluster, specs, state.run_id, 2)
                host_rt = state.hosts["a"]
                self.assertEqual(host_rt.draining_for, "gang")
                if cause == "external":
                    host_rt.gpus[0].external_busy = True
                elif cause == "cancelled":
                    state.tasks["gang"].state = "cancelled"
                elif cause == "backoff":
                    state.tasks["gang"].next_eligible_at = 100
                elif cause == "cordoned":
                    host_rt.cordoned = True
                elif cause == "disk_full":
                    host_rt.disk_full = True
                elif cause == "preflight":
                    host_rt.preflight_ok = False
                else:
                    host_rt.gpu_cooldown_until["0"] = 100
                _, events = plan_dispatches(state, cluster, specs, state.run_id, 3)
                self.assertEqual([(event.kind, event.host_id) for event in events], [("host_drain_released", "a")])
                self.assertIsNone(host_rt.draining_for)
                self.assertIsNone(host_rt.draining_since)

    def test_gang_can_dispatch_elsewhere_and_release_original_reservation(self):
        cluster, specs, state = self.gang_runtime(4)
        plan_dispatches(state, cluster, specs, state.run_id, 2)
        state.tasks["long"].state = "completed"
        assignments, events = plan_dispatches(state, cluster, specs, state.run_id, 3)
        self.assertEqual([(a.task_id, a.host_id) for a in assignments], [("gang", "b"), ("single", "a")])
        self.assertEqual([(event.kind, event.host_id) for event in events], [("host_drain_released", "a")])

    def test_gang_drain_ties_use_affinity_speed_then_host_id(self):
        for recent_key, speed, expected in (("group", 3, "a"), (None, 3, "b"), (None, 1, "a")):
            with self.subTest(recent_key=recent_key, speed=speed):
                hosts = [self.host(), self.host("b", speed_weight=speed)]
                cluster, manifest, _, state = self.runtime(hosts, [
                    TaskSpec("first", "true", est_duration_s=10),
                    TaskSpec("second", "true", est_duration_s=10),
                    TaskSpec("gang", "true", gpus=4, host_affinity="prefer_key:group"),
                ])
                for task_id, host_id in (("first", "a"), ("second", "b")):
                    state.hosts[host_id].gpus = [GpuSlotSnapshot(index) for index in range(4)]
                    state.tasks[task_id].state = "running"
                    state.tasks[task_id].attempts.append(TaskAttempt(1, host_id, [0], "/out", "/attempt", 0))
                if recent_key is not None:
                    state.hosts["a"].recent_affinity_keys.append(recent_key)
                assignments, events = plan_dispatches(state, cluster, manifest, 2)
                self.assertEqual(assignments, [])
                self.assertEqual([(event.kind, event.host_id) for event in events], [("host_draining", expected)])

    def test_additional_tasks_load_and_layer_manifest_defaults(self):
        cluster = ClusterConfig({"a": self.host(vars={"workspace": "/work"})})
        source = self.root / "manifest.json"
        source.write_text(json.dumps({
            "version": "1", "run_id": "offline", "tasks": [{"id": "base", "cmd": "true"}],
            "defaults": {"priority": 3, "env": {"BASE": "yes", "LAYER": "base"}},
            "sync": {"local_dir": "/source", "remote_var": "workspace"},
            "preflight": {"cmd": "true"},
        }))
        existing = load_dispatch_manifest(source)
        self.assertEqual(DispatchManifest.from_dict(existing.to_dict()), existing)
        extra = self.root / "additional.json"
        raw_task = {"id": "extra", "cmd": "{workspace} {dep.base.out_dir}", "depends_on": ["base"]}
        for payload, priority, env in (
            ([raw_task], 3, {"BASE": "yes", "LAYER": "base"}),
            ({"version": "1", "defaults": {"priority": 9, "env": {"LAYER": "batch", "BATCH": "yes"}},
              "tasks": [{**raw_task, "env": {"LAYER": "task"}}]}, 9,
             {"BASE": "yes", "LAYER": "task", "BATCH": "yes"}),
        ):
            with self.subTest(payload=payload):
                extra.write_text(json.dumps(payload))
                combined, added = load_and_merge_additional_tasks(existing, extra, cluster)
                self.assertEqual([task.id for task in combined.tasks], ["base", "extra"])
                self.assertIs(combined.tasks[0], existing.tasks[0])
                self.assertEqual((combined.run_id, combined.defaults, combined.sync, combined.preflight),
                                 (existing.run_id, existing.defaults, existing.sync, existing.preflight))
                self.assertEqual(added, [combined.tasks[1]])
                self.assertEqual((added[0].manifest_index, added[0].priority, added[0].env), (1, priority, env))

    def test_additional_tasks_reject_invalid_schema_ids_cycles_and_host_vars(self):
        cluster = ClusterConfig({"a": self.host()})
        existing = DispatchManifest((TaskSpec("base", "true"),))
        extra = self.root / "additional.json"
        new_task = {"id": "extra", "cmd": "true"}
        for payload, error in (
            ([], "JSON Schema"), (False, "manifest fragment"), ({}, "JSON Schema"),
            ({"tasks": [new_task], "defaults": {"unknown": 1}}, "JSON Schema"),
            ({"tasks": [new_task], "version": "2"}, "JSON Schema"),
            ([{**new_task, "extra_field": True}], "JSON Schema"),
            ([{**new_task, "id": "base"}], "Duplicate"),
            ([new_task, new_task], "Duplicate"),
            ([{**new_task, "depends_on": ["second"]},
              {**new_task, "id": "second", "depends_on": ["extra"]}], "cycle"),
            ([{**new_task, "cmd": "{missing}"}], "missing on hosts"),
        ):
            with self.subTest(payload=payload):
                extra.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ConfigError, error):
                    load_and_merge_additional_tasks(existing, extra, cluster)
                self.assertEqual([task.id for task in existing.tasks], ["base"])

    def test_probe_log_metadata_validation_and_legacy_defaults(self):
        host = self.host()
        namespace = {}
        exec(REMOTE_PROBE_SCRIPT, namespace)
        attempt = {"pgid": None, "attempt": 1, "attempt_dir": str(self.root), "out_dir": str(self.root),
                   "done_markers": []}
        status = namespace["probe_attempt"](attempt, set())
        self.assertIsNone(status["log_size"])
        self.assertIsNone(status["log_mtime"])
        log = self.root / "task.log"
        log.write_bytes(b"progress")
        os.utime(log, (10.1234, 10.1234))
        status = namespace["probe_attempt"](attempt, set())
        snapshot = parse_probe_response(host, json.dumps({"attempts": {"job": status}}))
        self.assertTrue(snapshot.ok)
        self.assertEqual((snapshot.attempts["job"].log_size, snapshot.attempts["job"].log_mtime), (8, 10.123))
        self.assertEqual(snapshot.attempts["job"].log_tail, "progress")
        for name, value in (("log_size", True), ("log_size", -1), ("log_size", 1.0),
                            ("log_mtime", False), ("log_mtime", -1), ("log_mtime", float("inf"))):
            with self.subTest(name=name, value=value):
                self.assertFalse(parse_probe_response(host, json.dumps({"attempts": {"job": {name: value}}})).ok)
        for value in (None, "job", [1]):
            self.assertFalse(parse_probe_response(host, json.dumps({"existing_done_tasks": value})).ok)
        legacy = parse_probe_response(host, '{"attempts":{"job":{}}}')
        self.assertEqual(legacy.to_dict()["existing_done_tasks"], [])
        self.assertIsNone(legacy.attempts["job"].to_dict()["log_size"])
        self.assertIsNone(legacy.attempts["job"].to_dict()["log_mtime"])
        self.assertEqual(HostProbeSnapshot.from_dict({"host_id": "a", "ok": True}).existing_done_tasks, [])
        self.assertIsNone(HostRuntime.from_dict({}).to_dict()["draining_since"])
        old_attempt = TaskAttempt.from_dict({
            "attempt": 1, "host_id": "a", "gpu_ids": [], "out_dir": "/out",
            "attempt_dir": "/attempt", "started_at": 0,
        })
        for name in ("last_activity_at", "last_log_size", "last_log_mtime", "cancel_reason"):
            self.assertIsNone(old_attempt.to_dict()[name])

    def test_watchdog_tracks_progress_and_classifies_stall_after_done_payload(self):
        task = TaskSpec("job", "true", stall_timeout_s=10, retry_policy={
            "max_retries": 0, "non_retryable_log_patterns": ["configuration denied"],
        })
        cluster, _, specs, state = self.runtime([self.host()], [task])
        state.hosts["a"].gpus = [GpuSlotSnapshot(0)]
        assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
        mark_dispatch_started(state, assignments[0], 0, pgid=20)
        attempt = state.tasks["job"].active_attempt

        def observe(now, size, mtime, util=0, payload=None):
            return reconcile_host_probe(state, cluster, specs, HostProbeSnapshot(
                "a", True, gpus=[GpuSlotSnapshot(0, utilization_pct=util)],
                attempts={"job": AttemptProbeStatus(True, payload, log_tail="configuration denied",
                                                    log_size=size, log_mtime=mtime)},
            ), now)

        for now, size, mtime, util, activity in (
            (5, 0, 0, 0, 0), (11, 5, 1, 0, 11), (22, 5, 1, 1, 22),
            (33, 5, 2, 0, 33), (43, 5, 2, 0, 33),
        ):
            with self.subTest(now=now):
                self.assertEqual(observe(now, size, mtime, util), ([], [], []))
                self.assertEqual(attempt.last_activity_at, activity)
        preempts, _, events = observe(44, 5, 2)
        self.assertEqual([(action.reason, action.pgid) for action in preempts], [("stall_timeout", 20)])
        self.assertEqual(attempt.state, "cancelling")
        self.assertEqual(attempt.cancel_reason, "stall_timeout")
        self.assertEqual(events[0].kind, "task_stall_timeout")
        self.assertEqual(events[0].details, {"attempt": 1, "stall_timeout_s": 10, "idle_s": 11})
        self.assertEqual(observe(45, None, None), ([], [], []))
        _, _, events = observe(46, 5, 2, payload={"exit_code": 0})
        self.assertEqual(events[0].kind, "task_failed")
        self.assertEqual(events[0].details["reason"], "stall_timeout")
        self.assertEqual(attempt.failure_kind, "task_error")

    def test_wall_timeout_takes_precedence_and_validates_both_timeout_fields(self):
        for name in ("timeout_s", "stall_timeout_s"):
            for invalid in (True, 0, -1, 1.0, "1"):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ConfigError):
                    TaskSpec("job", "true", **{name: invalid})
        task = TaskSpec("job", "true", gpus=0, timeout_s=5, stall_timeout_s=4)
        cluster, _, specs, state = self.runtime([self.host(accelerator="cpu")], [task])
        assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
        mark_dispatch_started(state, assignments[0], 0, pgid=20)
        snapshot = HostProbeSnapshot("a", True, attempts={"job": AttemptProbeStatus(True)})
        preempts, _, events = reconcile_host_probe(state, cluster, specs, snapshot, 6)
        self.assertEqual([action.reason for action in preempts], ["timeout"])
        self.assertEqual(events[0].kind, "task_timeout")
        self.assertEqual(state.tasks["job"].active_attempt.cancel_reason, "timeout")
        snapshot.attempts["job"].pgid_alive = False
        _, _, events = reconcile_host_probe(state, cluster, specs, snapshot, 7)
        self.assertEqual(events[0].details["reason"], "timeout")

    def test_relay_validation_placeholders_and_lifecycle(self):
        for invalid in (True, 0, -1, 1.0, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                RelayInput("source", "artifact", max_bytes=invalid)
        relay = RelayInput("source", "artifact", max_bytes=1)
        task = TaskSpec("job", "{inputs_dir}/artifact {dep.source.out_dir}", gpus=0,
                        depends_on=("source",), relay_inputs=(relay,),
                        retry_policy={"max_infra_retries": 1, "backoff_s": 5})
        hosts = [self.host(accelerator="cpu"), self.host("b", accelerator="cpu")]
        self.assertEqual(render_task_command(
            task, hosts[0], run_id="offline", out_dir="/out", attempt_dir="/attempt",
            gpu_ids=[], dep_out_dirs={"source": "/source"}, inputs_dir="/inputs",
        ), "/inputs/artifact /source")
        cluster, manifest, specs, state = self.runtime(hosts, [
            TaskSpec("source", "true", gpus=0), task,
            TaskSpec("child", "true", gpus=0, depends_on=("job",)),
        ])
        self.assertEqual(needs_cross_host_relay(state, task, "b"), [])
        state.tasks["source"].state = "completed"
        state.tasks["source"].completed_host = "a"
        self.assertEqual(needs_cross_host_relay(state, task, "a"), [])
        self.assertEqual(needs_cross_host_relay(state, task, "b"), [relay])
        assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
        self.assertEqual(mark_task_relaying(state, assignments[0], 0)[0].kind, "relay_started")
        self.assertTrue(has_active_tasks(state))
        self.assertFalse(is_terminal(state))
        self.assertEqual(task_counts(state), {"completed": 1, "pending": 1, "relaying": 1})
        self.assertEqual(plan_dispatches(state, cluster, specs, state.run_id, 1)[0], [])
        events = mark_relay_failed(state, manifest, assignments[0], "relay_unreachable", 1, retryable=True)
        self.assertEqual(events[0].kind, "task_requeued")
        self.assertEqual(events[0].details["failure_kind"], "infra_error")
        self.assertEqual(state.tasks["job"].infra_retries_used, 1)
        self.assertEqual(state.tasks["job"].next_eligible_at, 6)
        self.assertEqual(plan_dispatches(state, cluster, specs, state.run_id, 5)[0], [])
        assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 6)
        self.assertEqual(assignments[0].attempt, 2)
        mark_task_relaying(state, assignments[0], 6)
        events = mark_relay_failed(state, manifest, assignments[0], "relay_exceeds_max_bytes", 7, retryable=False)
        self.assertEqual([event.kind for event in events], ["task_failed", "task_blocked_dep"])
        self.assertEqual(events[0].details["reason"], "relay_exceeds_max_bytes")
        self.assertTrue(is_terminal(state))
        self.assertFalse(has_active_tasks(state))

    def test_dispatch_start_accepts_relaying_task(self):
        cluster, _, specs, state = self.runtime([self.host(accelerator="cpu")], [TaskSpec("job", "true", gpus=0)])
        assignments, _ = plan_dispatches(state, cluster, specs, state.run_id, 0)
        mark_task_relaying(state, assignments[0], 0)
        mark_dispatch_started(state, assignments[0], 1, pgid=20)
        self.assertEqual(state.tasks["job"].state, "running")
        self.assertEqual(state.tasks["job"].active_attempt.pgid, 20)


if __name__ == "__main__":
    unittest.main()
