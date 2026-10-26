import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from contracts import RunLimits, write_json
from envs.arc import PROLONG_REVISION
from run import DEFAULT_LIMITS, execute, stop_worker


def run_faux_game(args, limits, root, io_directory, deadline, stop_requested, backend_identity):
    from backends.prime_agent import PrimeAgentBackend
    from run import run_game

    def backend(*values, **keywords):
        keywords["faux"] = {"responses": [{"content": [{
            "type": "toolCall", "name": "python_repl", "arguments": {"code": """
import asyncio
rlm.harness.create_memory("Deadline", "saved by the real kernel", id="deadline")
await asyncio.sleep(60)
"""},
        }]}]}
        (root / "agent/models.json").write_text(json.dumps({"providers": {"faux": {
            "api": "faux", "baseUrl": "http://localhost:0", "apiKey": "fixture",
            "models": [{"id": "faux-1", "contextWindow": 128000}],
        }}}))
        (root / "agent/settings.json").write_text(json.dumps({
            "defaultProvider": "faux", "defaultModel": "faux-1",
            "allowedModels": ["faux/faux-1"], "telemetry": {"enabled": False},
        }))
        return PrimeAgentBackend(*values, **keywords)

    with patch("run.PrimeAgentBackend", side_effect=backend):
        run_game(args, limits, root, io_directory, deadline, stop_requested, backend_identity)


class ShutdownTests(unittest.TestCase):
    def test_worker_can_finish_a_committed_save_before_parent_capture(self):
        stop_requested = Mock()
        process = Mock(pid=123)
        process.is_alive.return_value = True
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "harness_state.json"

            def settle(timeout):
                stop_requested.set.assert_called_once()
                write_json(path, {"committed": True})
                process.is_alive.return_value = False

            process.join.side_effect = settle
            outcome = stop_worker(process, stop_requested, [0, 0])
            self.assertEqual(outcome, {"forced": False, "writers_stopped": True, "errors": []})
            self.assertEqual(json.loads(path.read_text()), {"committed": True})
            process.terminate.assert_not_called()

    def test_forced_shutdown_is_bounded_and_never_signals_a_reused_pid(self):
        process = Mock(pid=123)
        process.is_alive.side_effect = [True, True, False]
        with patch("run.process_start", return_value=999), patch("run.os.kill") as kill:
            outcome = stop_worker(process, Mock(), [456, 12345])
        self.assertTrue(outcome["forced"])
        self.assertTrue(outcome["writers_stopped"])
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        kill.assert_not_called()

    def test_preflight_failure_and_user_interruption_still_finalize(self):
        for event, expected in (("preflight", "runner_error"), ("interrupt", "user_interrupt")):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                args = SimpleNamespace(
                    output=root, task="ls20", data_root=None,
                    **{key: Path("/unused") for key in ("binary", "credential_helper", "auth_dir", "kernel", "python_home", "data")},
                )
                with (
                    patch("run.ModelGateway") as gt,
                    patch("run.multiprocessing.get_context") as context,
                    patch("run.importlib.metadata.distribution") as distribution,
                    patch("run.subprocess.check_output", return_value="fixture\n"),
                    patch("run.command", return_value=["fixture"]),
                    patch("run.subprocess.run", return_value=SimpleNamespace(
                        returncode=1 if event == "preflight" else 0, stdout="", stderr="fixture",
                    )),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    distribution.return_value.read_text.return_value = json.dumps({
                        "vcs_info": {"commit_id": PROLONG_REVISION},
                    })
                    context.return_value.Array.return_value = [0, 0]
                    process = context.return_value.Process.return_value
                    process.exitcode = 0
                    process.is_alive.side_effect = [KeyboardInterrupt(), False, False] if event == "interrupt" else [False]
                    gt.return_value.count = 0
                    gt.return_value.close.return_value = []
                    report = execute(args, DEFAULT_LIMITS)
                self.assertEqual(report["stop_reason"], expected)
                self.assertTrue(report["final_state_saved"])
                self.assertTrue((root / "final-harness-states.json").exists())
                self.assertEqual(json.loads((root / "result.json").read_text()), report)

    def test_real_parent_deadline_finalizes_a_faux_worker_without_forcing_refinement(self):
        data = Path("/data/juheon/primeagent")
        with tempfile.TemporaryDirectory(prefix="prime-parent-deadline-") as temporary:
            root = Path(temporary) / "run"
            args = SimpleNamespace(
                output=root, task="ls20-9607627b", data_root=None,
                binary=data / "build/target/debug/prime-agent",
                credential_helper=data / "build/target/debug/examples/copilot_credentials",
                auth_dir=Path("/unused"), kernel=data / "envs/kernel",
                python_home=data / "envs/python", data=data / "benchmarks/arc-agi-3",
            )
            with (
                patch("run.run_game", run_faux_game),
                patch("run.ModelGateway.credentials", return_value={
                    "access": "fixture", "endpoint": "https://api.enterprise.githubcopilot.com",
                }),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                report = execute(args, RunLimits(seconds=5))
            self.assertEqual(report["stop_reason"], "time_limit")
            self.assertEqual(report["model_requests"], 0)
            self.assertTrue(report["final_state_saved"], report)
            self.assertEqual(report["snapshots"], 1)
            self.assertFalse(report["shutdown"]["forced"], report["shutdown"])
            self.assertTrue(report["shutdown"]["writers_stopped"])
            states = json.loads((root / "final-harness-states.json").read_text())
            self.assertEqual(next(iter(states.values()))["entries"]["memory"]["deadline"]["content"],
                             "saved by the real kernel")
