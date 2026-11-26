"""Offline integration coverage for the remote scheduler and controller."""

from __future__ import annotations

import fcntl
import io
import json
import os
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-orchestrator"
SCRIPTS = SKILL / "scripts"
SHIM = SCRIPTS / "remote_dispatch.sh"
sys.path.insert(0, str(SCRIPTS))

from codex_orch.remote.controller import RemoteController, main, resolve_cluster_path
from codex_orch.remote.gpu_probe import REMOTE_PROBE_SCRIPT, build_probe_request, parse_probe_response
from codex_orch.remote.models import (
    AttemptProbeStatus, GpuSlotSnapshot, HostConfig, HostProbeSnapshot, TaskSpec,
    ValidationError, load_cluster_config,
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


if __name__ == "__main__":
    unittest.main()
