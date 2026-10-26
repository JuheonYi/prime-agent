import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prolong_agent.agent.base import BaseAgent
from prolong_agent.agent.action_queue import ActionQueue
from prolong_agent.environment.runner import GameRunner

from backends.prime_agent import PrimeAgentBackend
from envs.arc import ArcEnvironment
from faux_harness_run import check_tool_errors
from run import configure_agent

REPO = Path(__file__).resolve().parents[3]
DATA = Path("/data/juheon/primeagent")


def tool(code):
    return {"content": [{"type": "toolCall", "name": "python_repl", "arguments": {"code": code}}]}


class ProlongBackendTests(unittest.TestCase):
    def test_reuses_action_parser_and_queue_semantics(self):
        actions = BaseAgent._parse_actions_json_text(
            '{"actions":["ACTION6(12,34)","RESET","invalid","ACTION1"]}', cap=3
        )
        self.assertEqual(actions, [
            {"name": "ACTION6", "data": {"x": 12, "y": 34}},
            {"name": "RESET", "data": {}},
        ])
        queue = ActionQueue()
        queue.load(actions)
        queue.check_score(1)
        self.assertEqual(len(queue), 0)

    def test_official_game_runner_and_prompt_use_one_prime_session_without_images(self):
        with tempfile.TemporaryDirectory(prefix="prolong-prime-") as temporary:
            root = Path(temporary)
            for name in ("workspace", "agent", "io"):
                (root / name).mkdir()
            configure_agent(root / "agent")
            (root / "agent/models.json").write_text(json.dumps({"providers": {"faux": {
                "api": "faux", "baseUrl": "http://localhost:0", "apiKey": "fixture",
                "models": [{"id": "faux-1", "contextWindow": 128000, "maxTokens": 8192}],
            }}}))
            (root / "agent/settings.json").write_text(json.dumps({
                "defaultProvider": "faux", "defaultModel": "faux-1",
                "subagentDefaultModel": "faux/faux-1", "allowedModels": ["faux/faux-1"],
                "telemetry": {"enabled": False},
            }))
            faux = {"responses": [
                tool("""
import json, os, socket
from pathlib import Path
assert "[INITIAL BOARD STATE]" in Path("/workspace/logs.txt").read_text()
assert not os.path.exists("/data/juheon/primeagent/benchmarks")
try:
    socket.create_connection(("1.1.1.1",443),timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError("external network reachable")
counter = 1
rlm.harness.create_memory("Continuity", "first", id="continuity")
child = await rlm.spawn("Check child isolation.", name="worker")
await rlm.collect(child, timeout_ms=15000)
Path("/workspace/actions.json").write_text(json.dumps({"actions":["ACTION1","ACTION1"]}))
"""),
                "Inspect the grid.\n[PLAN]\nMove up twice.",
                tool("""
from pathlib import Path
import json
assert counter == 1
assert "[POST-ACTION BOARD STATE]" in Path("/workspace/logs.txt").read_text()
rlm.harness.update_memory("continuity", "Continuity", "second")
Path("/workspace/actions.json").write_text(json.dumps({"actions":["ACTION2"]}))
"""),
                "Update the plan.\n[PLAN]\nMove down.",
            ], "child": {"responses": [
                tool("""
import socket
try:
    socket.create_connection(("1.1.1.1",443),timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError("child escaped sandbox")
rlm.harness.create_memory("Child", "isolated", id="child-safe")
"""),
                "Child verified."
            ]}}
            backend = PrimeAgentBackend(DATA / "build/target/debug/prime-agent", REPO,
                                        DATA / "envs/kernel", DATA / "envs/python", root, root / "io",
                                        time.monotonic() + 60, faux=faux)
            env = ArcEnvironment("ls20-9607627b", DATA / "benchmarks/arc-agi-3",
                                 root / "environment")
            try:
                env.prepare()
                # Bound this scripted fixture, not the production experiment.
                with patch("envs.arc.GameRunner", side_effect=lambda **kwargs: GameRunner(
                    **(kwargs | {"max_actions_per_game": 3})
                )):
                    metrics = env.run(backend.analyze)
                self.assertEqual(metrics["run_total_actions"], 3)
                self.assertEqual(backend.calls, 2)
                self.assertEqual(
                    (root / "task-prompt.txt").read_text(),
                    BaseAgent(grid_mode="hex", log_window=None)._build_system_prompt(
                        ["ACTION1", "ACTION2", "ACTION3", "ACTION4", "RESET"]
                    ),
                )
                sessions = list((root / "agent/sessions").glob("*.jsonl"))
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0].stem, backend.session_id)
                check_tool_errors(root / "agent", root)
                states = list((root / "agent").rglob("harness_state.json"))
                child_deadline = time.monotonic() + 10
                while len(states) < 2 and time.monotonic() < child_deadline:
                    time.sleep(0.02)
                    states = list((root / "agent").rglob("harness_state.json"))
                self.assertEqual(len(states), 2, backend._rpc("get_rlm_children"))
                root_state = next(path for path in states if "continuity" in json.loads(path.read_text())["entries"]["memory"])
                self.assertEqual(json.loads(root_state.read_text())["entries"]["memory"]["continuity"]["content"], "second")
                self.assertEqual(len(list(root_state.parent.glob("snapshots/*.json"))), 2)
                check_tool_errors(root / "agent", root)
                for line in sessions[0].read_text().splitlines():
                    content = json.loads(line).get("message", {}).get("content", [])
                    if isinstance(content, list):
                        self.assertFalse(any(block.get("type") == "image" for block in content))
                log = (root / "environment/logs.txt").read_text()
                self.assertIn("Action 3 |", log)
                self.assertIn("[POST-ACTION BOARD STATE]", log)
            finally:
                backend.close()
                env.close()
