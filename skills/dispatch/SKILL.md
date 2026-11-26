---
name: dispatch
description: >-
  Use remote_dispatch.sh for centralized dynamic multi-server GPU scheduling with
  work-stealing across heterogeneous clusters, DAG failure isolation, co-tenancy
  safety, Launch-Verify-Return, and lightweight result pull-back.
---

# /dispatch

Use `scripts/remote_dispatch.sh` from the installed `codex-orchestrator` skill directory.

1. **Prepare inventory and manifest.** Keep `clusters.json` outside the plugin repo.
   Resolve it with `--cluster`, then `$CODEX_ORCH_CLUSTER_CONFIG`, then `~/.config/codex-orchestrator/clusters.json`.
   Use [the inventory example](../codex-orchestrator/references/cluster-inventory-example.json).
   Write host-agnostic `jobs.json` with `{python}`, `{repo_dir}`, `{cache_dir}`, `{out_dir}`, and optional `{inputs_dir}`.
   Set `requires_tags`, `depends_on`, `done_markers`, `stall_timeout_s`, and `relay_inputs` as needed. Use `pull_to_local` for automatic retrieval and `pull_excludes` to omit bulky files.
2. **Validate and dry-run.** Run these commands to check the schema, DAG, host variables, GPU availability, and disk headroom.
   ```sh
   scripts/remote_dispatch.sh validate --cluster "$HOME/.config/codex-orchestrator/clusters.json" --manifest ./jobs.json
   scripts/remote_dispatch.sh plan --cluster "$HOME/.config/codex-orchestrator/clusters.json" --manifest ./jobs.json --live-probe --format table
   ```
3. **Launch-Verify-Return.** Run `scripts/remote_dispatch.sh start --cluster <clusters.json> --manifest <jobs.json> --run-dir <dir> [--sync] [--caffeinate]`.
   Confirm Wave 1 placements from the compact JSON receipt. Return a concise Chinese brief with `run_dir`, the active host-to-GPU map, and the status command. Report blocked or empty placements as shown in the receipt.
4. **Monitor, inject, and control.** Use `scripts/remote_dispatch.sh status --run-dir <dir> --format table` to read local JSON without SSH calls. Use `tick --run-dir <dir>` for one scheduling cycle from `/schedule`; prefer `status` when the detached controller already owns the loop.
   Use `add-tasks --run-dir <dir> --tasks <file.json>` to inject new DAG tasks into an active run. Multi-GPU (`gpus > 1`) tasks automatically reserve and drain the fastest eligible host (`draining_for`) without blocking 1-GPU sweeps on other hosts.
   Use `cordon [--preempt]`, `uncordon`, `requeue`, `cancel`, `pull`, and `stop [--kill-remote]` as needed. See the [CLI reference](../codex-orchestrator/references/cli-reference.md#remote-multi-server-gpu-master-scheduler-remote_dispatchsh) for selectors.
   Remote jobs survive local sleep or disconnect. If the controller has exited, reconnect with `scripts/remote_dispatch.sh start --resume --run-dir <dir>`.
5. **Analyze and hand off.** Lightweight results pulled to `<run_dir>/results/<task_id>/` feed directly into `/analyze`. Detached controllers never block the `/handoff` Quiescence Gate and are auto-recorded in `/handoff` Section 9 (`dispatch_runs`, or pass `--dispatch-run-dir <dir>`).
