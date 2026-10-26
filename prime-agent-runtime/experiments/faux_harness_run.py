"""Run one resumed Prime Agent task with scripted LLM responses and real ARC I/O.

No live-model mode is provided. Game actions are fixed wiring probes, not a
solver or benchmark-performance evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from arc_bridge import ArcBridge
from contracts import capture_states, snapshot_errors


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def tool(code: str) -> dict[str, Any]:
    return {"content": [{"type": "toolCall", "name": "python_repl", "arguments": {"code": code}}]}


def check_tool_errors(agent_dir: Path, output: Path) -> None:
    errors = []
    for path in agent_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            message = row.get("message", {})
            details = message.get("details") or {}
            if message.get("isError") or details.get("status") == "error":
                errors.append({"file": str(path), "entry": row})
    if errors:
        write_json(output / "tool-errors.json", errors)
        raise AssertionError("faux task reported tool errors; see tool-errors.json")


def run(args: argparse.Namespace) -> None:
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    agent_dir = root / "agent"
    agent_dir.mkdir()
    home = root / "home"
    home.mkdir()
    repo = Path(__file__).resolve().parents[2]
    env = {
        key: value for key, value in os.environ.items()
        if not key.endswith("_API_KEY") and key not in (
            "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN",
            "PRIME_AGENT_KERNEL_PYTHON", "RLM_HARNESS_STATE_DIR",
            "RLM_GLOBAL_HARNESS_STATE_DIR", "PRIME_AGENT_SESSION_DIR",
            "PRIME_AGENT_CODING_AGENT_SESSION_DIR", "PRIME_AGENT_DAEMON_SOCKET",
        )
    }
    env.update({
        "HOME": str(home),
        "DO_NOT_TRACK": "1",
        "PRIME_AGENT_CODING_AGENT_DIR": str(agent_dir),
        "PRIME_AGENT_KERNEL_VENV": str(args.kernel_venv.resolve()),
        "PRIME_AGENT_HARNESS_SNAPSHOTS": "1" if args.snapshots == "on" else "0",
        "PI_PACKAGE_DIR": str(repo),
    })
    with args.binary.open("rb") as binary:
        binary_hash = hashlib.file_digest(binary, "sha256").hexdigest()
    manifest = {
        "mode": "faux-only", "task": "arc-ls20-harness-wiring", "real_llm": False,
        "snapshots": args.snapshots,
        "git_revision": subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip(),
        "binary": str(args.binary.resolve()), "binary_sha256": binary_hash, "game_seed": 0,
        "environment_dir": str(args.environments.resolve()),
    }
    write_json(root / "manifest.json", manifest)
    write_json(root / "initial-harness-states.json", capture_states(agent_dir))
    bridge = ArcBridge(args.environments.resolve(), root / "game-recordings", download=args.download_game)
    manifest["game_version"] = bridge.environment.environment_info.game_id
    write_json(root / "manifest.json", manifest)
    bridge.thread.start()
    started = time.perf_counter()
    try:
        first_cell = f"""
import requests
from rlm import host_request
response = requests.post({bridge.url + "/step"!r}, timeout=10)
response.raise_for_status()
assert response.json()["actions"] == 1
rlm.harness.create_prompt_note("Verify", "Validate observations before reporting.", id="policy")
rlm.harness.create_memory("Progress", "First observation.", id="progress")
rlm.harness.create_skill("Inspect children", "Recover current child handles.", id="inspect",
    reference={{"type": "python", "import": "rlm", "callable": "list_subagents",
               "call_pattern": "await rlm.list_subagents()"}}, arguments={{}})
rlm.harness.create_subagent("Reviewer", "Check the observations.", id="reviewer")
rlm.harness.create_memory("Temporary", "Remove after checking.", id="temporary")
rlm.harness.update_memory("progress", "Progress", "Checked first observation.")
rlm.harness.delete_memory("temporary")
child = await rlm.spawn("Record a local review lesson.", name="reviewer")
await rlm.collect(child, timeout_ms=120000)
reply = await host_request("refine.run", {{"instructions": "Refine the progress lesson."}})
print(reply)
"""
        proposal = {
            "summary": "Keep verified observations", "rationale": "Faux test fixture",
            "expectedOutcome": "The next turn sees the refined lesson.",
            "edits": [{
                "action": "update", "kind": "memory", "id": "progress",
                "title": "Progress", "content": "Use validated frames.",
            }],
        }
        scripts = [
            {
                "responses": [tool(first_cell), "Checkpoint reached.", json.dumps(proposal)],
                "children": {"reviewer": {"responses": [
                    tool('rlm.harness.create_memory("Review", "Child-local evidence.", id="child-lesson")'),
                    "Review complete.",
                ]}},
            },
            {"responses": [
                tool(f"""
import requests
assert rlm.harness.get("memory", "progress").content == "Use validated frames."
response = requests.post({bridge.url + "/step"!r}, timeout=10)
response.raise_for_status()
assert response.json()["actions"] == 2
rlm.harness.update_memory("progress", "Progress", "Second observation verified.")
rlm.harness.delete_subagent("reviewer")
print("FAUX_HARNESS_VERIFIED")
"""),
                "Single-task wiring verified.",
            ]},
        ]
        session_files: list[Path] = []
        for index, script in enumerate(scripts):
            write_json(root / f"faux-{index}.json", script)
            env["PRIME_AGENT_FAUX_SCRIPT"] = json.dumps(script)
            command = [str(args.binary.resolve()), "--mode", "json", "--session-dir", str(agent_dir / "sessions")]
            if session_files:
                command += ["--resume", str(session_files[0])]
            command += ["-p", "Continue the same ARC task and verify the harness." if index else "Probe ARC and record lessons."]
            with (root / f"console-{index}.jsonl").open("w") as stdout, (root / f"stderr-{index}.log").open("w") as stderr:
                subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr, check=True, timeout=600)
            session_files = sorted((agent_dir / "sessions").glob("*.jsonl"))
            if len(session_files) != 1:
                raise AssertionError(f"expected one root session, found {session_files}")
            check_tool_errors(agent_dir, root)
        write_json(root / "final-harness-states.json", capture_states(agent_dir))
        root_id = session_files[0].stem
        harness = agent_dir / "session-artifacts" / root_id / "harness"
        paths = sorted((harness / "snapshots").glob("[0-9]*.json"))
        history = [json.loads(path.read_text()) for path in paths]
        state = json.loads((harness / "harness_state.json").read_text())
        errors = snapshot_errors(root)
        expected = 10 if args.snapshots == "on" else 0
        if len(history) != expected:
            errors.append(f"expected {expected} snapshots, found {len(history)}")
        if history and not errors:
            assert all(row["schema"] == 2 for row in history)
            assert [row["source"] for row in history].count("agent_refine") == 1
            assert history[-1]["state"] == state
        assert state["entries"]["memory"]["progress"]["content"] == "Second observation verified."
        assert "temporary" not in state["entries"]["memory"]
        assert not state["entries"]["subagent"]
        assert not (agent_dir / "sessions/harness").exists()
        assert not list(agent_dir.rglob("pending.json"))
        child_states = [
            path for path in agent_dir.rglob("harness_state.json")
            if "child-lesson" in json.loads(path.read_text())["entries"]["memory"]
        ]
        assert len(child_states) == 1, "child must retain its own separate local state"
        write_json(root / "result.json", {
            "status": "incomplete" if errors else "passed", "mode": "faux-only", "real_llm": False,
            "snapshot_errors": errors,
            "snapshots": args.snapshots,
            "elapsed_seconds": time.perf_counter() - started,
            "session_id": root_id, "root_snapshots": len(history),
            "child_state": str(child_states[0].relative_to(root)),
            "game_actions": bridge.actions,
            "final_harness": str((harness / "harness_state.json").relative_to(root)),
            "scope": "Persistence and ARC wiring only; no learned behavior or benchmark score claim.",
        })
        print(json.dumps({"status": "incomplete" if errors else "passed", "run": str(root), "snapshots": len(history)}))
    finally:
        bridge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kernel-venv", type=Path, required=True)
    parser.add_argument("--environments", type=Path, required=True)
    parser.add_argument("--download-game", action="store_true", help="Allow initial public ARC environment download.")
    parser.add_argument("--snapshots", choices=("on", "off"), default="on")
    run(parser.parse_args())
