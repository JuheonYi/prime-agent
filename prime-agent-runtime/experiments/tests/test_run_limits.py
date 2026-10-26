import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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

    def test_requested_ten_minutes_and_thousand_actions_parse_without_old_ceiling(self):
        _, limits = parse_arguments(self.arguments() + ["--seconds", "600", "--max-actions", "1000"])
        self.assertEqual(limits.seconds, 600)
        self.assertEqual(limits.max_actions, 1000)

    def test_one_configuration_reaches_model_payload_and_original_game_runner(self):
        _, limits = parse_arguments(self.arguments() + [
            "--seconds", "17", "--max-actions", "3", "--max-model-requests", "5",
            "--max-output-tokens-per-request", "1024", "--max-model-output-tokens", "4096",
            "--model-concurrency", "2", "--action-cap", "2",
        ])
        self.assertEqual(limits.action_cap, 2)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            configure_agent(directory, limits)
            model = json.loads((directory / "models.json").read_text())["providers"]["github-copilot"]["models"][0]
            self.assertEqual(model["maxTokens"], 1024)
            env = ArcEnvironment("ls20", directory, directory / "environment", limits)
            self.assertIs(env.limits, limits)
        payload = {"model": "gpt-5.6-sol", "input": [], "stream": True,
                   "store": False, "max_output_tokens": 1024}
        self.assertEqual(validate_payload(payload, limits), 1024)
        with self.assertRaises(ValueError):
            validate_payload(payload | {"max_output_tokens": 1025}, limits)

    def test_invalid_limits_fail_before_creating_or_running_anything(self):
        for flag in ("--seconds", "--max-actions", "--max-model-requests", "--model-concurrency"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_arguments(self.arguments() + [flag, "0"])
        with self.assertRaises(ValueError):
            replace(DEFAULT_LIMITS, max_model_output_tokens=1)
