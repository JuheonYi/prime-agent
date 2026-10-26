import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contracts import snapshot_errors
from envs.arc import PROLONG_REVISION
from run import DEFAULT_LIMITS, execute


class SnapshotLoggingTests(unittest.TestCase):
    def test_collects_snapshot_errors_from_each_host_diagnostic_channel(self):
        paths = (
            "stderr.log",
            "stderr-0.log",
            "agent/session-artifacts/root/kernel-stderr.log",
            "agent/logs/worker-root.stderr.log",
            "agent/children/child/logs/worker-child.stderr.log",
        )
        for relative in paths:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ordinary diagnostic\nharness snapshot error: disk full\n")
                self.assertEqual(snapshot_errors(root), ["harness snapshot error: disk full"])

    def test_normal_diagnostics_and_non_log_files_do_not_report_snapshot_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker-root.stderr.log").write_text("ordinary diagnostic\n")
            (root / "console.jsonl").write_text('{"text":"harness snapshot error: quoted"}\n')
            self.assertEqual(snapshot_errors(root), [])

    def test_live_report_marks_snapshot_errors_incomplete_without_changing_game_outcome(self):
        for game_status, run_status, stop_mode in (
            ("COMPLETED_RUN", "completed", None),
            ("ERROR", "failed", None),
            ("NOT_FINISHED", "stopped", None),
            ("ERROR", "stopped", "terminate"),
            ("ERROR", "stopped", "kill"),
        ):
            for error_path in (None, "kernel-stderr.log", "agent/logs/worker-root.stderr.log"):
                with self.subTest(game=game_status, error_path=error_path, stop_mode=stop_mode), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "run"
                    args = SimpleNamespace(
                        output=root, task="ls20", data_root=None,
                        **{name: Path("/unused") for name in (
                            "binary", "credential_helper", "auth_dir", "kernel", "python_home", "data",
                        )},
                    )
                    final = {"status": game_status, "run_total_actions": 1}
                    errors = ["harness snapshot error: disk full"] if error_path else []

                    def finish_environment():
                        output = root
                        (output / "environment").mkdir(exist_ok=True)
                        (output / "environment/metrics.json").write_text(json.dumps(final))
                        if error_path:
                            path = output / error_path
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(errors[0] + "\n")

                    with (
                        patch("run.ModelGateway") as gateway_type,
                        patch("run.multiprocessing.get_context") as process_context,
                        patch("run.importlib.metadata.distribution") as distribution,
                        patch("run.command", return_value=["preflight"]),
                        patch("run.subprocess.run", return_value=SimpleNamespace(
                            returncode=0, stdout="preflight passed", stderr="",
                        )),
                        patch("run.subprocess.check_output", return_value="fixture-revision\n"),
                        patch("run.time.monotonic", side_effect=[0, DEFAULT_LIMITS.seconds if stop_mode else 0,
                                                                DEFAULT_LIMITS.seconds if stop_mode else 0]),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        distribution.return_value.read_text.return_value = json.dumps({
                            "vcs_info": {"commit_id": PROLONG_REVISION},
                        })
                        gateway = gateway_type.return_value
                        gateway.count = 0
                        gateway.close.return_value = []
                        process_context.return_value.Array.return_value = [0, 0]
                        process = process_context.return_value.Process.return_value
                        process.is_alive.return_value = bool(stop_mode)
                        process.exitcode = 0
                        process.start.side_effect = finish_environment
                        if stop_mode:
                            getattr(process, stop_mode).side_effect = lambda: setattr(process.is_alive, "return_value", False)
                        execute(args, DEFAULT_LIMITS)
                        if stop_mode:
                            process.terminate.assert_called_once()
                            if stop_mode == "kill":
                                process.kill.assert_called_once()
                        gateway.close.assert_called_once()
                    report = json.loads((root / "result.json").read_text())
                    self.assertEqual(report["limits"], {"seconds": DEFAULT_LIMITS.seconds, "max_actions": None})
                    self.assertTrue(report["final_state_saved"])
                    self.assertEqual(
                        {key: report[key] for key in (
                            "status", "logging_complete", "environment", "stop_reason",
                        )},
                        {
                            "status": "incomplete" if errors or stop_mode else run_status,
                            "logging_complete": not (errors or stop_mode),
                            "environment": final,
                            "stop_reason": "time_limit" if stop_mode else (
                                "game_completed" if game_status == "COMPLETED_RUN" else "environment_exit"
                            ),
                        },
                    )
