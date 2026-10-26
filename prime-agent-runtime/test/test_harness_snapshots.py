from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rlm.harness import HarnessState
from test_harness import FACTORY_DAG, PYTHON_REFERENCE
from test_repl import ReplProcess, one, stream_text


def comparable(value):
    if isinstance(value, dict):
        return {key: comparable(item) for key, item in value.items() if key not in ("created_at", "updated_at")}
    if isinstance(value, list):
        return [comparable(item) for item in value]
    return value


class HarnessSnapshotsTest(unittest.TestCase):
    def test_off_on_and_failed_snapshots_preserve_entries_returns_and_overview(self):
        results = []
        for mode in ("0", "1", "failed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                (directory / "settings.json").write_text('{"factory":{"enabled":true}}')
                if mode == "failed":
                    (directory / "snapshots").write_text("blocked")
                with patch.dict(os.environ, {
                    "PRIME_AGENT_HARNESS_SNAPSHOTS": "0" if mode == "0" else "1",
                    "PRIME_AGENT_CODING_AGENT_DIR": str(directory),
                }), patch("sys.stderr", new_callable=io.StringIO) as diagnostics:
                    state = HarnessState(directory / "harness_state.json")
                    outcomes, states, overviews = [], [], []
                    for kind in ("prompt", "memory", "skill", "subagent", "factory"):
                        kwargs = {}
                        if kind == "skill":
                            kwargs = {"reference": PYTHON_REFERENCE, "arguments": {}}
                        elif kind == "factory":
                            kwargs = {"arguments": {"dag": FACTORY_DAG}}
                        entry = state.create(kind, kind, "v1", id=kind, **kwargs)
                        outcomes.append((entry.id, entry.content, entry.version))
                        states.append(json.loads(state.file_path.read_text()))
                        overviews.append(state.overview().replace(str(directory), "<directory>"))
                        entry = state.update(kind, kind, kind, "v2", **kwargs)
                        outcomes.append((entry.id, entry.content, entry.version))
                        states.append(json.loads(state.file_path.read_text()))
                        overviews.append(state.overview().replace(str(directory), "<directory>"))
                        outcomes.append(state.delete(kind, kind))
                        states.append(json.loads(state.file_path.read_text()))
                        overviews.append(state.overview().replace(str(directory), "<directory>"))
                    state = HarnessState(state.file_path)
                    state.record_refinement("test", ["finished"], id="record")
                    states.append(json.loads(state.file_path.read_text()))
                    results.append((outcomes, comparable(states), overviews))
                    if mode == "1":
                        snapshots = sorted((directory / "snapshots").glob("*.json"))
                        self.assertEqual(len(snapshots), 16)
                        self.assertEqual(
                            [json.loads(path.read_text())["state"] for path in snapshots], states
                        )
                    elif mode == "0":
                        self.assertFalse((directory / "snapshots").exists())
                    else:
                        self.assertEqual(diagnostics.getvalue().count("harness snapshot error:"), 16)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_failed_live_write_still_raises_without_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"PRIME_AGENT_HARNESS_SNAPSHOTS": "1"}
        ):
            directory = Path(temp_dir)
            state = HarnessState(directory / "harness_state.json")
            with patch("os.replace", side_effect=OSError("live failure")):
                with self.assertRaisesRegex(OSError, "live failure"):
                    state.create_memory("Lesson", "text")
            self.assertFalse((directory / "snapshots").exists())

    def test_failed_snapshot_diagnostics_do_not_enter_repl_tool_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "snapshots").write_text("blocked")
            repl = ReplProcess({
                "PRIME_AGENT_HARNESS_SNAPSHOTS": "1",
                "RLM_HARNESS_STATE_DIR": str(directory),
            })
            try:
                repl.ready()
                events = repl.execute("snapshot-failure", (
                    "from rlm.harness import get_harness_state\n"
                    "state = get_harness_state()\n"
                    "state.create_memory('Lesson', 'saved', id='lesson')\n"
                    "print(state.get('memory', 'lesson').content)"
                ))
                self.assertEqual(one(events, "done")["status"], "ok")
                self.assertEqual(stream_text(events, "stdout"), "saved\n")
                self.assertEqual(stream_text(events, "stderr"), "")
                self.assertFalse(any(event.get("event") == "error" for event in events))
                repl.shutdown()
            finally:
                repl.close()
