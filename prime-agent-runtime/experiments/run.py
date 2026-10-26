"""One isolated benchmark run. Native model transport, environment, and agent are separate."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from contracts import Environment, RunLimits, SHUTDOWN_GRACE_SECONDS, capture_states, process_start, write_json
from backends.prime_agent import PrimeAgentBackend
from envs.arc import ArcEnvironment, PROLONG_REVISION
from finalization import finalize, model_usage, read_json, timestamp
from model_gateway import MODEL, ModelGateway
from sandbox import command

DEFAULT_LIMITS = RunLimits(seconds=600)


def configure_agent(directory: Path):
    write_json(directory / "models.json", {"providers": {"github-copilot": {
        "api": "openai-responses", "baseUrl": "http://127.0.0.1:9151",
        "apiKey": "local-transport-no-upstream-credential",
        "models": [{
            "id": MODEL, "name": "GPT-5.6 Sol", "reasoning": True, "input": ["text", "image"],
            "contextWindow": 1050000,
            "thinkingLevelMap": {"off": "none", "low": "low", "medium": "medium",
                                 "high": "high", "xhigh": "xhigh", "max": "max"},
        }],
    }}})
    write_json(directory / "settings.json", {
        "defaultProvider": "github-copilot", "defaultModel": MODEL,
        "subagentDefaultModel": f"github-copilot/{MODEL}",
        "allowedModels": [f"github-copilot/{MODEL}"],
        "telemetry": {"enabled": False},
        "mcpServers": {},
    })


def run_game(args, limits, root, io, deadline, stop_requested, backend_identity):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    repo = Path(__file__).resolve().parents[2]
    backend = PrimeAgentBackend(args.binary.resolve(), repo, args.kernel.resolve(),
                                args.python_home.resolve(), root, io, deadline,
                                stop_requested=stop_requested, process_identity=backend_identity)
    env: Environment = ArcEnvironment(args.task, args.data.resolve(), root / "environment",
                                      max_actions=limits.max_actions)
    try:
        env.prepare()
        env.run(backend.analyze, check_stop=backend.check_stop)
    finally:
        stop_requested.set()
        try:
            backend.close()
        finally:
            env.close()


def stop_worker(process, stop_requested, backend_identity) -> dict:
    stop_requested.set()
    shutdown = {"forced": False, "writers_stopped": True, "errors": []}
    if process is not None and process.pid is not None:
        process.join(timeout=SHUTDOWN_GRACE_SECONDS)
        if process.is_alive():
            shutdown["forced"] = True
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        shutdown["writers_stopped"] = not process.is_alive()
    pid, started = backend_identity[:]
    if pid and process_start(pid) == started:
        shutdown["forced"] = True
        for termination in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, termination)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + 1
            while process_start(pid) == started and time.monotonic() < deadline:
                time.sleep(0.02)
            if process_start(pid) != started:
                break
        if process_start(pid) == started:
            shutdown["writers_stopped"] = False
            shutdown["errors"].append("sandbox process did not terminate")
    return shutdown


def execute(args, limits: RunLimits):
    if args.data_root is not None:
        from reproduce import MARKER, check, check_ready, environment

        if (args.data_root / MARKER).is_file():
            check_ready(args.data_root)
            spec = json.loads((Path(__file__).parent / "repro/environment.lock.json").read_text())
            check(args.data_root, spec, environment(args.data_root))
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "agent").mkdir()
    (root / "workspace").mkdir()
    configure_agent(root / "agent")
    initial = capture_states(root / "agent")
    write_json(root / "initial-harness-states.json", initial)
    repo = Path(__file__).resolve().parents[2]
    distribution = importlib.metadata.distribution("prolong-agent")
    provenance = json.loads(distribution.read_text("direct_url.json"))
    if provenance.get("vcs_info", {}).get("commit_id") != PROLONG_REVISION:
        raise RuntimeError("installed PRO-LONG revision does not match the pinned research dependency")
    manifest = {
        "benchmark": "arc", "task": args.task, "model": MODEL, "main_reasoning": "medium",
        "environment_runner": "PRO-LONG GameRunner", "prolong_revision": PROLONG_REVISION,
        "observation_format": "PRO-LONG full hex-grid log", "log_window": None,
        "prompt_source": "PRO-LONG BaseAgent._build_system_prompt/_build_prompt (unmodified)",
        "backend": "Prime Agent persistent daemon session; task prompt supplements immutable base prompt",
        "native_autonomous_driver": False,
        "refiner_reasoning": "original minimum-supported default",
        "limits": asdict(limits),
        "request_output_policy": "Prime Agent native default; no experiment override",
        "network": "private namespace, fixed model Unix socket only; environment via files",
        "snapshots": True, "forced_refinement": False,
        "git_revision": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
    }
    write_json(root / "manifest.json", manifest)
    with tempfile.TemporaryDirectory(prefix="prime-eval-io-") as temporary:
        io = Path(temporary)
        gateway = ModelGateway(io / "model.sock", args.credential_helper.resolve(), args.auth_dir.resolve(),
                               root / "model-requests.jsonl")
        process = None
        context = multiprocessing.get_context("spawn")
        stop_requested = context.Event()
        backend_identity = context.Array("q", [0, 0])
        gateway.stop_requested = stop_requested
        started = None
        deadline_at = None
        reason = "runner_error"
        failure = None
        shutdown = {"forced": False, "writers_stopped": False, "errors": []}
        try:
            # Resolve credentials before the timed run, never forwarding them into bwrap.
            gateway.credentials()
            probe = command(
                args.binary.resolve(), repo, args.kernel.resolve(), args.python_home.resolve(),
                root, io, [str(args.kernel.resolve() / "bin/python"), "-c", f"""
import os, socket
import rlm, rlm.mcp, agent_message, attach_image, compact, edit, goal, refine
assert not os.path.exists({str(Path.home() / '.ssh')!r})
assert not os.path.exists({str(args.auth_dir.resolve())!r})
assert not os.path.exists({str(args.data.resolve())!r})
try:
    socket.create_connection(('1.1.1.1',443),timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('external network unexpectedly reachable')
print('SANDBOX_PREFLIGHT_PASSED')
"""],
            )
            checked = subprocess.run(probe, capture_output=True, text=True, timeout=15)
            write_json(root / "preflight.json", {
                "exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr,
            })
            if checked.returncode:
                raise RuntimeError("sandbox/skill preflight failed; no inference started")
            started = time.monotonic()
            deadline = started + limits.seconds
            deadline_at = time.time() + limits.seconds
            gateway.start(deadline)
            reason = "environment_exit"
            process = context.Process(
                target=run_game, args=(args, limits, root, io, deadline, stop_requested, backend_identity),
            )
            process.start()
            while process.is_alive():
                if time.monotonic() >= deadline:
                    reason = "time_limit"
                    break
                process.join(timeout=0.1)
            if reason == "environment_exit" and time.monotonic() >= deadline:
                reason = "time_limit"
        except KeyboardInterrupt:
            reason = "user_interrupt"
        except Exception as error:
            # Reporting must survive a runner failure, but never turn it into success.
            failure = f"{type(error).__name__}: {error}"
            print(f"Experiment failed: {failure}", file=sys.stderr)
        finally:
            stop_requested.set()
            gateway.stop_admission()
            stopping_at = timestamp()
            try:
                shutdown = stop_worker(process, stop_requested, backend_identity)
                saved_shutdown = root / "shutdown.json"
                if saved_shutdown.exists():
                    errors = []
                    saved = read_json(saved_shutdown, root, errors)
                    shutdown["errors"].extend(errors)
                    if saved is not None:
                        if (type(saved.get("forced")) is not bool or type(saved.get("writers_stopped")) is not bool
                                or not isinstance(saved.get("errors"), list)):
                            shutdown["errors"].append("invalid daemon shutdown report")
                        else:
                            shutdown["forced"] |= saved["forced"]
                            shutdown["writers_stopped"] &= saved["writers_stopped"]
                            shutdown["errors"].extend(saved["errors"])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                shutdown["errors"].append(f"worker shutdown: {type(error).__name__}: {error}")
            try:
                shutdown["errors"].extend(gateway.close())
            except (OSError, RuntimeError) as error:
                shutdown["errors"].append(f"gateway shutdown: {type(error).__name__}: {error}")
            report = finalize(root, initial, {
                "stop_reason": reason,
                "elapsed_seconds": time.monotonic() - started if started is not None else 0,
                "deadline_epoch_seconds": deadline_at, "stop_requested_at": stopping_at,
                "limits": asdict(limits), "shutdown": shutdown, "runner_error": failure,
                "worker_exit_code": process.exitcode if process is not None else None,
                "model_requests": gateway.count,
            })
    print(json.dumps(report, indent=2))
    return report


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="ls20")
    parser.add_argument("--seconds", type=int, default=DEFAULT_LIMITS.seconds)
    parser.add_argument("--max-actions", type=int, help="Optional total game actions; unlimited when omitted")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=os.environ.get("DATA_ROOT"),
                        help="Installation root (defaults to DATA_ROOT); explicit paths override it")
    paths = {
        "binary": "build/target/debug/prime-agent",
        "credential_helper": "build/target/debug/examples/copilot_credentials",
        "auth_dir": "copilot-native/agent",
        "kernel": "envs/kernel",
        "python_home": "envs/python",
        "data": "benchmarks/arc-agi-3",
    }
    for name in paths:
        parser.add_argument("--" + name.replace("_", "-"), type=Path)
    arguments = parser.parse_args(argv)
    if arguments.data_root is not None:
        arguments.data_root = arguments.data_root.expanduser().resolve()
        for name, relative in paths.items():
            if getattr(arguments, name) is None:
                setattr(arguments, name, arguments.data_root / relative)
    missing = ["--" + name.replace("_", "-") for name in paths if getattr(arguments, name) is None]
    if missing:
        parser.error("Set DATA_ROOT or supply these paths: " + ", ".join(missing))
    try:
        limits = RunLimits(seconds=arguments.seconds, max_actions=arguments.max_actions)
    except ValueError as error:
        parser.error(str(error))
    return arguments, limits


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    result = execute(*parse_arguments())
    raise SystemExit(1 if result["status"] in ("failed", "incomplete") else 0)
