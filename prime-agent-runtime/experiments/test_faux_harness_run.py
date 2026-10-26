import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from arc_bridge import ArcBridge
from faux_harness_run import capture_states, check_tool_errors, snapshot_errors


class FauxHarnessRunTest(unittest.TestCase):
    def test_empty_task_and_snapshot_failures_are_reported_out_of_band(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(capture_states(root), {})
            (root / "kernel-stderr.log").write_text("harness snapshot error: permission denied\n")
            self.assertEqual(snapshot_errors(root), ["harness snapshot error: permission denied"])
            self.assertEqual(capture_states(root), {})

    def test_observations_serialize_numpy_frames(self):
        with tempfile.TemporaryDirectory() as directory, patch("arc_bridge.Arcade") as arcade:
            arcade.return_value.make.return_value.reset.return_value = SimpleNamespace(
                state=SimpleNamespace(value="NOT_FINISHED"),
                levels_completed=0,
                frame=[np.zeros((64, 64), dtype=np.int8)],
            )
            bridge = ArcBridge(Path(directory), Path(directory))
            try:
                visible = json.loads(json.dumps(bridge.visible_state()))
                self.assertEqual(visible["frame"], [[[0] * 64 for _ in range(64)]])
            finally:
                bridge.server.server_close()

    def test_kernel_error_status_fails_even_when_is_error_is_false(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {"message": {"role": "toolResult", "isError": False, "details": {"status": "error"}}}
            (root / "session.jsonl").write_text(json.dumps(entry) + "\n")
            with self.assertRaisesRegex(AssertionError, "tool errors"):
                check_tool_errors(root, root)
            self.assertEqual(json.loads((root / "tool-errors.json").read_text())[0]["entry"], entry)


if __name__ == "__main__":
    unittest.main()
