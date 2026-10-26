# PRO-LONG ARC environment with Prime Agent

## Reproduce this installation

Clone the fork at the commit/tag containing these experiment changes, then run
from the repository root:

```bash
export DATA_ROOT="$HOME/.local/share/primeagent-experiments"
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT"
```

The supported snapshot is **Ubuntu 24.04, Linux x86_64, glibc 2.39**. Setup uses
Ubuntu's system Python, not an active conda/virtualenv. Exact Ubuntu package
revisions are listed in [environment.lock.json](repro/environment.lock.json).
Missing system packages are installed with `apt-get` (using `sudo` when
necessary); installed version mismatches fail rather than upgrading/downgrading
the host. The pinned revisions must still be available in the configured Ubuntu
repositories. Other OS/architectures are not silently mapped to Docker or a
weaker sandbox. Unprivileged bubblewrap/user namespaces must work on the host.

Setup installs:

- `uv` 0.9.18 and Rust 1.99.1, using checksum-verified installer downloads.
- Python 3.11.14 for the kernel (including its captured executable hash) and
  system Python 3.12.3 for ARC. No Python launchers are installed into your home.
- The exact observed kernel packages from
  [kernel.requirements.lock](repro/kernel.requirements.lock), followed by the
  local runtime and the same six editable skills without dependency resolution.
- ARC/PRO-LONG from the existing [uv.lock](uv.lock), including the pinned
  PRO-LONG Git commit.
- The current checkout's **debug** binary and native credential helper, built
  with `Cargo.lock`. It does not download an upstream Prime Agent binary.
- Official `ls20-9607627b` and `ft09-0d8bbf25` game data, verified against
  captured source hashes and scoring/display metadata. No newer game is substituted.

The kernel snapshot deliberately differs from the upstream runtime lock:
for example, it contains `mcp==2.3.0` rather than `2.0.0`, and additional
scientific/image packages. Do **not** run an upstream runtime `uv sync` over
this kernel. Build-only constraints stay outside the kernel. The currently
unavailable optional `agent_observe` and `rlm_heartbeat` skills stay unavailable;
setup does not change the agent's tool inventory to eliminate those warnings.

Use a **new, dedicated data directory outside the checkout**. Setup refuses to
adopt a non-empty unmanaged directory, including the original experiment data
root. A managed interrupted installation can be retried. Re-running completed
setup verifies it without reinstalling packages or replacing games, runs, or
credentials. If the checkout moves or its contents change, create a fresh data
root for that revision. The installation marker records the source content
fingerprint, commit/dirty state, binary hashes, and checked package inventories.

```text
DATA_ROOT/
  toolchain/             pinned uv and Rust
  build/target/debug/    locally built binary and credential helper
  envs/arc/              shared ARC/PRO-LONG environment
  envs/kernel/           shared Prime kernel environment
  envs/python/           managed Python 3.11
  benchmarks/arc-agi-3/   game/version directories
  copilot-native/agent/  NEW machine-local authentication
  runs/                  NEW experiment outputs
  cache/                 disposable downloads and package cache
```

### Authenticate on the new machine

```bash
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT" login
```

This opens the locally built Prime Agent using the new data root's auth
directory. Use `/login`, select **GitHub Copilot**, authorize in the browser,
then exit without submitting a task. Setup/check never call a live model.
Do not copy or commit another machine's tokens. The existing gateway requires
the enterprise Copilot endpoint and access to `gpt-5.6-sol`; another endpoint
or model is not substituted if your account lacks access.

### Run an experiment

```bash
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT" run \
  --task ft09-0d8bbf25 --seconds 60 --max-actions 1000 \
  --max-model-requests 32 --max-model-output-tokens 32768 \
  --output "$DATA_ROOT/runs/ft09-smoke-001"
```

Use `--task ls20-9607627b` for LS20. `run` supplies installation paths to
the existing [run.py](run.py) and forwards the other options unchanged.
Output directories must be new. This command uses real model quota;
installation does not launch it automatically. Defaults, prompts, main/refiner
reasoning, 25-turn review, cooldown, action cap, snapshots, and isolation
remain those of the existing runner/runtime.

### Verify without inference

```bash
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT" check
```

Setup already performs this check: exact Python/package versions, local runtime
source equality, relocated editable imports, PRO-LONG revision, game hashes,
offline game action/observation roundtrips, and the existing sandbox's kernel
import/network/filesystem boundary. No score or harness-learning claim follows
from these checks. Even identical software cannot guarantee identical outputs
from an externally hosted model or identical wall-clock timing on another CPU.

### Share the fork, not the data root

Commit the current source changes **including untracked experiment files**,
these manifests, and the setup scripts; use a fixed commit/tag on the other
machine. Do not commit virtualenvs, compiled targets, caches, game downloads,
`runs`, or `copilot-native`. No existing runs or credentials are migrated.
Downloaded game metadata may contain a different installation path/time;
the pinned game identity, source hash, and scoring/display metadata, not those
host fields, are verified.

Runtime/snapshot details live in [harness-snapshots.md](harness-snapshots.md).
There is no separate copy under the repository's `docs/` directory.

## Environment and backend

The live runner imports the public PRO-LONG ARC research implementation at
commit `9d2f2d46fea8759ed494ce5b0166c7004a2e97c4`. The Git dependency and
subdirectory are pinned in `pyproject.toml` and `uv.lock`; startup verifies the
installed package's commit. This is **Prime Agent on the PRO-LONG ARC evaluation
environment**, not an exact reproduction of the unpublished Prime Agent-specific
adaptations described in its paper.

## Reused upstream code

- `prolong_agent.environment.arcagi3.ArcAgi3Env`: ARC reset/action/observation API.
- `prolong_agent.environment.runner.GameRunner`: game loop, retry/reset policy,
  per-action logs, level accounting, and evaluation.
- `prolong_agent.agent.base.BaseAgent`: original system/task prompt generation,
  history synchronization, action JSON parsing, and response-plan parsing.
- Original `GameState`, hex-grid formatter, and `ActionQueue`, including queue
  flushing at score changes and the default 20-action plan cap.

The upstream files are imported, not copied or rewritten. The full-log/hex
condition is used. The runner writes the 64x64 boards to `environment/logs.txt`;
the backend synchronizes this into `/workspace/logs.txt`. Prime Agent writes
`/workspace/actions.json`, and the upstream loop executes that plan.
There is no game MCP server or image-attachment instruction in the live path.
The old `arc_bridge.py` is used only by the separate faux logging fixture.

## Local components

- `run.py`: common limits, setup, bounded worker lifecycle, and final reporting.
- `envs/arc.py`: small lifecycle wrapper around the upstream environment/runner.
- `backends/prime_agent.py`: implements `BaseAgent.analyze` through the existing
  Prime Agent daemon API. One root session and Python kernel persist across
  plans; the daemon's original subagent host remains available.
- `sandbox.py`: bubblewrap namespaces and filesystem mounts.
- `local_relay.py` / `model_gateway.py`: fixed Copilot-only inference channel.

The PRO-LONG system template is supplied as a task supplement through
`appendSystemPrompt`, never as a replacement for Prime Agent's base prompt.
The generated upstream text is saved verbatim as `task-prompt.txt`, and per-plan
user prompts are saved in `prompts.jsonl`.

PRO-LONG controls when to ask for another plan, so the CLI's optional autonomous
continuation driver is not enabled. Prime Agent's native tool loop, subagents,
compaction, and minimum-supported refinement reasoning are retained. The Rust
runtime includes the restored 25-assistant-turn automatic review path, sharing
the existing review cooldown with compaction. The runner does not force
refinement; a review may decline. A zero-entry harness is reported as such.

## Isolation and limits

The sandbox has a private network namespace and exposes only:

```text
Prime Agent -> localhost:9151 -> model.sock -> fixed Copilot /responses
```

The host-side PRO-LONG runner owns the game. It shares only a task workspace
copy with the agent, not the game implementation or hidden evaluator. Existing
system/runtime/skill files are read-only; writable paths are the task workspace
and session state. Native credentials stay outside the sandbox and are resolved
through the repository's `copilot_credentials` helper over a private pipe.
The gateway rejects other models/routes, redirects, remote image/file URLs,
and server-side tools such as web search. No coproxy or Copilot SDK is used.

All defaults live in `run.py`'s `DEFAULT_LIMITS`: time, environment actions, model
requests, output per request, aggregate output, concurrency, and action-plan
length. The adapters receive a `RunLimits` object rather than private defaults.
The manifest records effective limits. PRO-LONG's own retry/reset/queue policy
is preserved; a parent process enforces wall time even during upstream retries.

This is not a separate VM or protection against kernel vulnerabilities. Hard
cgroup CPU/memory/disk quotas are not provided. Timing/transport/model choices
are this experiment's settings, not claimed to match the paper exactly.

## Direct runner interface

The setup command above is the supported installation path. The underlying
runner remains directly callable with explicit paths:

```bash
"$DATA_ROOT/envs/arc/bin/python" prime-agent-runtime/experiments/run.py \
  --task ls20-9607627b --seconds 1800 --max-actions 10000 \
  --output "$DATA_ROOT/runs/ls20-30min-001" \
  --binary "$DATA_ROOT/build/target/debug/prime-agent" \
  --credential-helper "$DATA_ROOT/build/target/debug/examples/copilot_credentials" \
  --auth-dir "$DATA_ROOT/copilot-native/agent" \
  --kernel "$DATA_ROOT/envs/kernel" \
  --python-home "$DATA_ROOT/envs/python" \
  --data "$DATA_ROOT/benchmarks/arc-agi-3"
```

Tests use faux or mocked responses and include a real sandbox, persistent
parent/kernel, real child execution, original prompt equality, original action
parser/queue behavior, grid logging, and snapshots. The final command uses real
inference quota. Output directories must be new.

`result.json` reports the stop reason, model usage, upstream metrics, and harness
state/snapshot counts. Snapshot errors from root stderr, kernel stderr, or daemon
worker stderr (including child workers) set `logging_complete` to false and
`status` to `incomplete`; the game outcome and stop reason remain unchanged.
If the worker is interrupted at its deadline, the report
explicitly labels progress parsed from the upstream log as partial metrics.
Original agent sessions, prompt records, and PRO-LONG logs are preserved.
Copilot billing is not inferred from model cost placeholders.
