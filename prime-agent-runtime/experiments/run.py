"""One isolated benchmark run. Native model transport, environment, and agent are separate."""

from __future__ import annotations

import argparse
from dataclasses import asdict, fields
import importlib.metadata
import json
import multiprocessing
import re
import subprocess
import tempfile
import time
from pathlib import Path

from contracts import Environment, RunLimits, capture_states, snapshot_errors
from backends.prime_agent import PrimeAgentBackend
from envs.arc import ArcEnvironment, PROLONG_REVISION
from model_gateway import MODEL, ModelGateway
from sandbox import command

DEFAULT_LIMITS = RunLimits(
    seconds=600,
    max_actions=1000,
    max_model_requests=256,
    max_output_tokens_per_request=8192,
    max_model_output_tokens=131072,
    model_concurrency=4,
    action_cap=20,
)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def model_usage(log: Path):
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    completed = [row for row in rows if isinstance(row.get("usage"), dict)]
    return {
        "responses_with_usage": len(completed),
        "input_tokens": sum(row["usage"].get("input_tokens", 0) for row in completed),
        "cached_input_tokens": sum(row["usage"].get("input_tokens_details", {}).get("cached_tokens", 0) for row in completed),
        "output_tokens": sum(row["usage"].get("output_tokens", 0) for row in completed),
        "note": "Completed responses only; an interrupted request can have unreported usage. Billing is not inferred.",
    }


def configure_agent(directory: Path, limits: RunLimits):
    write_json(directory / "models.json", {"providers": {"github-copilot": {
        "api": "openai-responses", "baseUrl": "http://127.0.0.1:9151",
        "apiKey": "local-transport-no-upstream-credential",
        "models": [{
            "id": MODEL, "name": "GPT-5.6 Sol", "reasoning": True, "input": ["text", "image"],
            "contextWindow": 1050000, "maxTokens": limits.max_output_tokens_per_request,
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


def run_game(args, limits, root, io, deadline):
    repo = Path(__file__).resolve().parents[2]
    backend = PrimeAgentBackend(args.binary.resolve(), repo, args.kernel.resolve(),
                                args.python_home.resolve(), root, io, limits, deadline)
    env: Environment = ArcEnvironment(args.task, args.data.resolve(), root / "environment", limits)
    try:
        env.prepare()
        env.run(backend.analyze)
    finally:
        backend.close()
        env.close()


def environment_result(root):
    path = root / "environment/metrics.json"
    if path.exists():
        return json.loads(path.read_text())
    log = root / "environment/logs.txt"
    actions = []
    if log.exists():
        actions = re.findall(r"^Action (\d+) \|.*?Score: (\d+)", log.read_text(), re.MULTILINE)
    return {
        "status": "INTERRUPTED", "partial_metrics": True,
        "run_total_actions": max((int(action) for action, _ in actions), default=0),
        "final_score": max((int(score) for _, score in actions), default=0),
        "note": "Progress parsed from the original PRO-LONG log because the worker was stopped.",
    }


def execute(args, limits: RunLimits):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "agent").mkdir()
    (root / "workspace").mkdir()
    configure_agent(root / "agent", limits)
    write_json(root / "initial-harness-states.json", capture_states(root / "agent"))
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
        "network": "private namespace, fixed model Unix socket only; environment via files",
        "snapshots": True, "forced_refinement": False,
        "git_revision": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
    }
    write_json(root / "manifest.json", manifest)
    with tempfile.TemporaryDirectory(prefix="prime-eval-io-") as temporary:
        io = Path(temporary)
        gateway = ModelGateway(io / "model.sock", args.credential_helper.resolve(), args.auth_dir.resolve(),
                               root / "model-requests.jsonl", limits)
        process = None
        try:
            # Resolve credentials before the timed run, never forwarding them into bwrap.
            gateway.credentials()
            probe = command(
                args.binary.resolve(), repo, args.kernel.resolve(), args.python_home.resolve(),
                root, io, [str(args.kernel.resolve() / "bin/python"), "-c", """
import os, socket
import rlm, rlm.mcp, agent_message, attach_image, compact, edit, goal, refine
assert not os.path.exists('/home/juheon/.ssh')
assert not os.path.exists('/data/juheon/primeagent/copilot-native')
assert not os.path.exists('/data/juheon/primeagent/benchmarks')
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
            gateway.start(deadline)
            reason = "environment_exit"
            process = multiprocessing.get_context("spawn").Process(
                target=run_game, args=(args, limits, root, io, deadline),
            )
            process.start()
            while process.is_alive():
                if gateway.exhausted:
                    reason = gateway.exhausted
                    break
                if time.monotonic() >= deadline:
                    reason = "time_limit"
                    break
                process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
            final = environment_result(root)
            if reason == "environment_exit":
                if final["status"] == "COMPLETED_RUN":
                    reason = "game_completed"
                elif final["run_total_actions"] >= limits.max_actions:
                    reason = "action_limit"
            errors = snapshot_errors(root)
            model_errors = []
            for path in (root / "agent").rglob("*.jsonl"):
                for line in path.read_text().splitlines():
                    message = json.loads(line).get("message", {})
                    if message.get("role") == "assistant" and message.get("stopReason") == "error":
                        model_errors.append(message.get("errorMessage", "model request failed"))
            if reason == "environment_exit" and gateway.exhausted:
                reason = gateway.exhausted
            write_json(root / "final-harness-states.json", capture_states(root / "agent"))
            report = {
                "status": "completed" if final["status"] == "COMPLETED_RUN" else (
                    "failed" if reason == "environment_exit" and (process.exitcode != 0 or model_errors or final["status"] == "ERROR") else "stopped"
                ),
                "stop_reason": reason, "elapsed_seconds": time.monotonic() - started,
                "limits": asdict(limits),
                "worker_exit_code": process.exitcode, "model_requests": gateway.count,
                "model_errors": model_errors,
                "usage": model_usage(root / "model-requests.jsonl"),
                "environment": final, "logging_complete": not errors, "snapshot_errors": errors,
                "harness_stores": len(capture_states(root / "agent")),
                "snapshots": len(list((root / "agent").rglob("snapshots/*.json"))),
            }
            if errors:
                report["status"] = "incomplete"
            write_json(root / "result.json", report)
            print(json.dumps(report, indent=2))
        finally:
            if process is not None and process.is_alive():
                process.kill()
                process.join(timeout=5)
            if gateway.thread.is_alive():
                gateway.close()
            else:
                gateway.server.server_close()
                gateway.socket.unlink(missing_ok=True)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="ls20")
    for field in fields(RunLimits):
        parser.add_argument("--" + field.name.replace("_", "-"), type=int,
                            default=getattr(DEFAULT_LIMITS, field.name))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--credential-helper", type=Path, required=True)
    parser.add_argument("--auth-dir", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--python-home", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        limits = RunLimits(**{field.name: getattr(arguments, field.name) for field in fields(RunLimits)})
    except ValueError as error:
        parser.error(str(error))
    return arguments, limits


if __name__ == "__main__":
    execute(*parse_arguments())
