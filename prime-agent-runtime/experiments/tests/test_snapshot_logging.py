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
        for game_status, run_status in (
            ("COMPLETED_RUN", "completed"),
            ("ERROR", "failed"),
            ("NOT_FINISHED", "stopped"),
        ):
            for error_path in (None, "kernel-stderr.log", "agent/logs/worker-root.stderr.log"):
                with self.subTest(game=game_status, error_path=error_path), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "run"
                    args = SimpleNamespace(
                        output=root, task="ls20",
                        **{name: Path("/unused") for name in (
                            "binary", "credential_helper", "auth_dir", "kernel", "python_home", "data",
                        )},
                    )
                    final = {"status": game_status, "run_total_actions": 1}
                    errors = ["harness snapshot error: disk full"] if error_path else []

                    def finish_environment(output):
                        if error_path:
                            path = output / error_path
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(errors[0] + "\n")
                        return final

                    with (
                        patch("run.ModelGateway") as gateway_type,
                        patch("run.multiprocessing.get_context") as process_context,
                        patch("run.importlib.metadata.distribution") as distribution,
                        patch("run.command", return_value=["preflight"]),
                        patch("run.subprocess.run", return_value=SimpleNamespace(
                            returncode=0, stdout="preflight passed", stderr="",
                        )),
                        patch("run.subprocess.check_output", return_value="fixture-revision\n"),
                        patch("run.environment_result", side_effect=finish_environment),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        distribution.return_value.read_text.return_value = json.dumps({
                            "vcs_info": {"commit_id": PROLONG_REVISION},
                        })
                        gateway = gateway_type.return_value
                        gateway.count = 0
                        gateway.exhausted = None
                        gateway.thread.is_alive.return_value = False
                        process = process_context.return_value.Process.return_value
                        process.is_alive.return_value = False
                        process.exitcode = 0
                        execute(args, DEFAULT_LIMITS)
                    report = json.loads((root / "result.json").read_text())
                    self.assertEqual(
                        {key: report[key] for key in (
                            "status", "logging_complete", "snapshot_errors", "environment", "stop_reason",
                        )},
                        {
                            "status": "incomplete" if errors else run_status,
                            "logging_complete": not errors,
                            "snapshot_errors": errors,
                            "environment": final,
                            "stop_reason": "game_completed" if game_status == "COMPLETED_RUN" else "environment_exit",
                        },
                    )
