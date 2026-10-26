# Opt-in harness snapshots

For installation, native login, and experiment commands, start with the
[experiment README](README.md). This document describes the runtime behavior,
not a separate installation procedure.

The original harness save completes first. When
`PRIME_AGENT_HARNESS_SNAPSHOTS=1`, an additional complete copy is written.
Unset the variable (the default) or set it to `0` to leave recording disabled.
The flag does not change refinement scheduling, prompts, reasoning, or factory
settings. Children inherit the flag through the existing launch environment.

## Files

Each local/global store keeps its original `harness_state.json`. Opted-in saves
add files under its adjacent `snapshots/` directory:

```text
harness/
  harness_state.json
  snapshots/
    <epoch-nanoseconds>-<uuid>.json
    <epoch-nanoseconds>-<uuid>.json
```

Filenames are unique, with capture time for normal chronological sorting; they
are not a global sequence or an entry version. Clock adjustments can affect
timestamp ordering. Each snapshot contains the entire state:

```json
{
  "schema": 2,
  "timestamp": "2026-10-24T00:00:00.000Z",
  "source": "agent_refine",
  "session_id": "session-id",
  "scope": "local",
  "refinement_id": "refine-id",
  "state": {
    "schema": 1,
    "entries": {
      "prompt": {},
      "memory": {},
      "skill": {},
      "subagent": {},
      "factory": {}
    },
    "refinements": []
  }
}
```

Sources distinguish `agent_crud`, `agent_record_refinement`, `agent_refine`,
`auto_refine`, `user_refine`, and `rollback`. The standalone Rust save API
records `host`, with null scope/session/refinement metadata where unknown.
Reads and rejected writes do not create snapshots. A refinement that persists
history but applies no entry edits still produces a snapshot.

There are no automatic initial snapshots, pending transactions, recovery
replays, external-change detection, compression, or pruning. Old schema-1
snapshots are left untouched; new writes do not read or depend on old history.
The experiment wrapper separately captures initial/final state files, including
an empty mapping when no store has been created.

## Preserving agent behavior

The original live-state serialization, atomic replacement, fsync, and lock
retry limit are retained. Snapshot code never writes back to the live state.
Snapshots add synchronous serialization and filesystem I/O but no additional
fsync or history scan. Timing overhead is not zero, especially with large
states or concurrent writers; equal model behavior cannot be guaranteed merely
because a deterministic replay passes.

Snapshot write errors are reported with `harness snapshot error:` in host
diagnostics. Python uses the REPL's existing original-stderr channel, not the
cell's captured stdout/stderr. Rust reports on host stderr. The original state
save still succeeds; actual live-state write failures still raise as before.
The wrappers read root stderr, kernel stderr, and daemon worker stderr logs,
including nested child workers. Snapshot errors set `status` to `incomplete`
rather than presenting incomplete logs as a successful experiment. The live
runner also sets `logging_complete` to false and retains the original
`stop_reason` and environment outcome.

This deliberately prioritizes agent behavior over crash-proof log delivery.
A process or machine failure after the live save may lose its final snapshot.
Diagnostics also depend on a functioning host output channel. Snapshots contain
task data, have owner-only file permissions, and should not be committed or
uploaded indiscriminately.

## Separate functional fixes

These changes are not part of logging equivalence:

- Session refinement now uses the same local artifact store as kernel writes
  and the harness digest. Old ambiguous shared `sessions/harness` files are
  not automatically migrated.
- Copilot provider/model headers reach both refinement and automatic review.
- The TypeScript turn-interval review path is restored in Rust. This scheduling
  correction is independent of snapshot recording.

The refiner and automatic review retain the original **minimum-supported
reasoning** policy. The main agent's effort setting does not override it.
For an A/B logging comparison, keep the functional fixes identical on both
sides and change only the snapshot flag.

## Automatic refinement triggers

Eligible root sessions review after 25 completed assistant turns by default,
including tool-call responses, or after successful compaction. Error and aborted
responses do not count. `autoRefine.turnInterval` changes the turn threshold;
`autoRefine.cooldownMs` retains its 20-minute default. A review can decline:
neither the turn threshold nor a compaction guarantees a harness edit.

CLI/JSON and daemon sessions share the review state and cooldown. Long tool
loops yield at an eligible interval boundary and resume the same prompt after
review. The review and planner receive the actual `turn_interval` or `compact`
reason. Explicit refinement resets the interval and cooldown to avoid an
immediate duplicate automatic pass.

`autoRefine.compact=false` disables only the compaction trigger.
`autoRefine.enabled=false` disables both automatic triggers;
`harnessRefinements=false` disables the entire refinement surface.

## Faux-only experiment

The runner uses real CLI/kernel/session persistence with scripted model
responses, a child, refinement, and a resumed root session. Its ARC `ls20`
adapter sends two fixed actions. It does not measure game-solving ability or
learned behavior and is not a sandbox for live agents.

This legacy fixture asks Prime Agent to bootstrap its kernel. Give it a
separate disposable kernel directory, **not** the frozen `envs/kernel`:
bootstrap may resolve different dependencies or install additional skills.
The reproducible installation's `setup.sh ... check` uses the frozen kernel
directly and does not bootstrap it.

```bash
# Run setup.sh first, as described in README.md.
export DATA_ROOT="$HOME/.local/share/primeagent-experiments"
export UV_CACHE_DIR="$DATA_ROOT/cache/uv"
export UV_PYTHON_INSTALL_DIR="$DATA_ROOT/envs/python"
cd prime-agent-runtime/experiments
"$DATA_ROOT/envs/arc/bin/python" faux_harness_run.py \
  --binary "$DATA_ROOT/build/target/debug/prime-agent" \
  --output "$DATA_ROOT/runs/faux-minimal-new" \
  --kernel-venv "$DATA_ROOT/cache/faux-kernel" \
  --environments "$DATA_ROOT/benchmarks/arc-agi-3" \
  --snapshots on
```

Use `--snapshots off` for the comparison run. Output directories must be new.
Use `--download-game` only to provision a missing public game; play is offline.
The runtime kernel uses Python 3.11, and ARC dependencies use Python 3.12.

Outputs include the manifest, console streams, original session logs,
`initial-harness-states.json`, `final-harness-states.json`, and `result.json`.
With recording enabled, this fixture expects ten root snapshots and a separate
child snapshot. Error diagnostics or absent expected snapshots invalidate the
logging check. Each run retains its own state and never reuses a user's global
lessons.

## Focused checks

```bash
cd prime-agent-runtime
PYTHONPATH=src "$DATA_ROOT/envs/kernel/bin/python" -m unittest discover -s test -p 'test_harness*.py'
cd experiments
"$DATA_ROOT/envs/arc/bin/python" -m unittest test_faux_harness_run
cd ../..
cargo test -p pa-core --lib refine
cargo test -p pa-core --test harness_snapshots --test compaction_transport
```

The comparisons exercise logging off, on, and failed writes. They compare
entry returns, state, rendered overview, actual refiner inputs, edit results,
and model-facing notices, excluding generated IDs/timestamps. A real REPL check
ensures snapshot errors do not enter a tool's stdout/stderr. The isolated Rust
fixture is marked ignored only so its parent test can launch it in three
separate processes with independent environment settings.

## Native Copilot helpers

`cargo run -p pa-core --example copilot_catalog -- <directory>` reads that
directory's existing `agent/auth.json` and queries GitHub identity and the
Copilot GPT catalog without inference calls or coproxy.

`cargo run -p pa-core --example copilot_smoke -- <agent-dir> <new-output-dir>`
uses real inference quota. It checks a fixed arithmetic tool roundtrip and
session refinement with three logical requests, each capped at 4,096 output
tokens and 180 seconds. Main requests use medium reasoning; refinement uses the
original minimum-supported level. It executes no model-generated code, shell,
subagent, or benchmark task. Snapshots are optional through the same flag.
