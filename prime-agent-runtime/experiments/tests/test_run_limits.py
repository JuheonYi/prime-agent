import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from contracts import RunLimits
from envs.arc import ArcEnvironment
from model_gateway import validate_payload
from run import DEFAULT_LIMITS, configure_agent, parse_arguments


class RunLimitsTests(unittest.TestCase):
    def arguments(self):
        return [
            "--output", "/unused/result", "--binary", "/unused/agent",
            "--credential-helper", "/unused/helper", "--auth-dir", "/unused/auth",
            "--kernel", "/unused/kernel", "--python-home", "/unused/python", "--data", "/unused/data",
        ]

    def test_seconds_is_required_and_action_budget_defaults_to_unlimited(self):
        _, limits = parse_arguments(self.arguments() + ["--seconds", "7200"])
        self.assertEqual(asdict(limits), {"seconds": 7200, "max_actions": None})
        _, bounded = parse_arguments(self.arguments() + ["--seconds", "7200", "--max-actions", "5000"])
        self.assertEqual(asdict(bounded), {"seconds": 7200, "max_actions": 5000})

    def test_native_model_default_has_no_experiment_output_override(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            configure_agent(directory)
            model = json.loads((directory / "models.json").read_text())["providers"]["github-copilot"]["models"][0]
            self.assertNotIn("maxTokens", model)
        payload = {"model": "gpt-5.6-sol", "input": [], "stream": True,
                   "store": False, "max_output_tokens": 128000}
        self.assertEqual(validate_payload(payload), 128000)
        del payload["max_output_tokens"]
        self.assertIsNone(validate_payload(payload))

    def test_invalid_limits_fail_before_creating_or_running_anything(self):
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_arguments(self.arguments() + ["--seconds", value])
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunLimits(seconds=value)
            with self.subTest(max_actions=value), self.assertRaises(ValueError):
                RunLimits(seconds=60, max_actions=value)

    def test_data_root_supplies_paths_and_explicit_paths_take_precedence(self):
        root = Path("/new machine/primeagent")
        with patch.dict(os.environ, {"DATA_ROOT": str(root)}):
            args, limits = parse_arguments(["--output", "/unused/result"])
            overridden, _ = parse_arguments(["--output", "/unused/result", "--kernel", "/explicit/kernel"])
        self.assertEqual(limits, DEFAULT_LIMITS)
        self.assertEqual(args.binary, root / "build/target/debug/prime-agent")
        self.assertEqual(args.auth_dir, root / "copilot-native/agent")
        self.assertEqual(args.data, root / "benchmarks/arc-agi-3")
        self.assertEqual(overridden.kernel, Path("/explicit/kernel"))

    def test_original_game_runner_crosses_the_old_action_budget_until_win(self):
        class Game:
            count = 0

            def observation(self):
                return {"state": "WIN" if self.count == 1101 else "NOT_FINISHED",
                        "score": int(self.count == 1101), "frame": [[[0]]], "available_actions": [1]}

            def reset(self, task=None):
                self.received_limit = task["max_actions"]
                return self.observation()

            def step(self, payload):
                self.count += 1
                return self.observation(), 0, self.count == 1101

        for limit, expected, stopped in ((None, 1101, False), (500, 500, False), (None, 3, True)):
            with self.subTest(limit=limit, stopped=stopped), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "logs.txt").write_text("")
                environment = ArcEnvironment("fixture-version", root, root, max_actions=limit)
                game = Game()
                environment.env = game

                def plan(*args, **kwargs):
                    return {"hint": "", "plan": "", "actions": [{"name": "ACTION1", "data": {}}] * 20}

                def check_stop():
                    if game.count >= 3:
                        raise TimeoutError("fixture deadline")

                if stopped:
                    with self.assertLogs("prolong_agent.environment.runner", level="ERROR"):
                        metrics = environment.run(plan, check_stop=check_stop)
                else:
                    metrics = environment.run(plan)
                self.assertEqual(game.received_limit, math.inf if limit is None else limit)
                self.assertEqual(metrics["run_total_actions"], expected)
                self.assertEqual(metrics["status"], "ERROR" if stopped else "COMPLETED_RUN" if limit is None else "TIMEOUT")
                self.assertEqual(json.loads((root / "metrics.json").read_text()),
                                 json.loads(json.dumps(metrics, allow_nan=False)))
