import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import write_json
from finalization import finalize


def state(content=None):
    entries = {kind: {} for kind in ("prompt", "memory", "skill", "subagent", "factory")}
    if content is not None:
        entries["memory"]["lesson"] = {"content": content}
    return {"schema": 1, "entries": entries, "refinements": []}


def base_report(*, forced=False):
    return {"stop_reason": "time_limit", "limits": {"seconds": 7200, "max_actions": None},
            "shutdown": {"forced": forced, "writers_stopped": True, "errors": []},
            "runner_error": None, "worker_exit_code": 0, "model_requests": 0}


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.agent = self.root / "agent"
        self.agent.mkdir()
        self.relative = "session-artifacts/root/harness/harness_state.json"
        self.live = self.agent / self.relative
        self.live.parent.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self, content, name="0001.json"):
        directory = self.live.parent / "snapshots"
        directory.mkdir(exist_ok=True)
        write_json(directory / name, {"schema": 2, "source": "agent_refine", "state": content})

    def test_unchanged_initial_state_is_saved_without_a_synthetic_snapshot(self):
        initial = state()
        write_json(self.live, initial)
        result = finalize(self.root, {self.relative: initial}, base_report())
        self.assertTrue(result["final_state_saved"])
        self.assertTrue(result["logging_complete"])
        self.assertEqual(result["snapshots"], 0)
        self.assertEqual(result["changed_harness_stores"], [])
        self.assertEqual(json.loads((self.root / "final-harness-states.json").read_text()), {self.relative: initial})

    def test_normal_refinement_snapshot_is_preserved_without_duplication(self):
        final = state("learned")
        write_json(self.live, final)
        self.snapshot(final)
        result = finalize(self.root, {}, base_report())
        self.assertTrue(result["logging_complete"])
        self.assertEqual(result["snapshots"], 1)
        self.assertEqual(result["recovered_final_snapshots"], [])
        self.assertEqual(result["changed_harness_stores"], [self.relative])

    def test_interruption_between_live_save_and_snapshot_preserves_final_state(self):
        initial, final = state("before"), state("after")
        write_json(self.live, final)
        self.snapshot(initial)
        result = finalize(self.root, {self.relative: initial}, base_report(forced=True))
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["final_state_saved"])
        self.assertFalse(result["logging_complete"])
        self.assertEqual(json.loads((self.root / "final-harness-states.json").read_text()), {self.relative: final})
        self.assertEqual(len(result["recovered_final_snapshots"]), 1)
        recovery = json.loads((self.root / result["recovered_final_snapshots"][0]).read_text())
        self.assertEqual(recovery["source"], "run_end")
        self.assertEqual(recovery["state"], final)
        self.assertIsNone(recovery["refinement_id"])
        self.assertEqual(recovery["session_id"], "root")
        self.assertEqual(recovery["timestamp"], result["final_state_captured_at"])
        self.assertEqual((self.root / result["recovered_final_snapshots"][0]).stat().st_mode & 0o777, 0o600)

    def test_missing_snapshots_for_changed_local_and_global_stores_are_recovered(self):
        final = state("saved")
        write_json(self.live, final)
        global_store = self.agent / "harness_state.json"
        write_json(global_store, final)
        result = finalize(self.root, {}, base_report())
        self.assertEqual(result["harness_stores"], 2)
        self.assertEqual(len(result["recovered_final_snapshots"]), 2)
        self.assertFalse(result["logging_complete"])
        scopes = {json.loads((self.root / path).read_text())["scope"] for path in result["recovered_final_snapshots"]}
        self.assertEqual(scopes, {"local", "global"})

    def test_truncated_and_corrupt_middle_records_do_not_destroy_the_report(self):
        final = state("saved")
        write_json(self.live, final)
        self.snapshot(final)
        path = self.agent / "sessions.jsonl"
        raw = b'{"type":"session"}\nnot-json\n{"message":{"role":"assistant","stopReason":"error","errorMessage":"provider failed"}}\n{"message":'
        path.write_bytes(raw)
        (self.root / "model-requests.jsonl").write_text(
            '{"event":"finished","usage":{"input_tokens":7,"output_tokens":3}}\n{"event":'
        )
        result = finalize(self.root, {}, base_report())
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["final_state_saved"])
        self.assertEqual(result["model_errors"], ["provider failed"])
        self.assertEqual(result["usage"]["output_tokens"], 3)
        self.assertEqual(len(result["log_errors"]), 3)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(json.loads((self.root / "result.json").read_text()), result)

    def test_invalid_usage_shape_is_reported_without_losing_final_state(self):
        (self.root / "model-requests.jsonl").write_text(
            '{"usage":{"output_tokens":"not a number"}}\n'
            '{"usage":{"output_tokens":9,"input_tokens":4}}\n'
        )
        result = finalize(self.root, {}, base_report())
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["final_state_saved"])
        self.assertEqual(result["usage"]["output_tokens"], 9)
        self.assertEqual(len(result["log_errors"]), 1)

    def test_refinement_failure_is_distinct_from_a_declining_review(self):
        (self.root / "console.jsonl").write_text(json.dumps({
            "type": "session_event", "event": {"type": "refine_failed", "error": "reviewer failed"},
        }) + "\n")
        result = finalize(self.root, {}, base_report())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["refinement_errors"][0]["error"], "reviewer failed")
        self.assertEqual(result["snapshots"], 0)
        self.assertTrue(result["logging_complete"])

    def test_corrupt_store_does_not_hide_other_stores_or_claim_final_state_saved(self):
        self.live.write_text('{"schema":')
        write_json(self.agent / "harness_state.json", state())
        result = finalize(self.root, {}, base_report())
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["final_state_saved"])
        self.assertEqual(json.loads((self.root / "final-harness-states.json").read_text()), {"harness_state.json": state()})
        self.assertEqual(len(result["state_errors"]), 1)

    def test_final_state_write_error_still_produces_an_explicit_failure_report(self):
        original = write_json

        def fail_final(path, value):
            if path.name == "final-harness-states.json":
                raise OSError("fixture disk failure")
            original(path, value)

        with patch("finalization.write_json", side_effect=fail_final):
            result = finalize(self.root, {}, base_report())
        self.assertFalse(result["final_state_saved"])
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue((self.root / "result.json").exists())

    def test_atomic_write_failure_keeps_the_previous_file_intact(self):
        path = self.root / "report.json"
        write_json(path, {"old": True})
        with patch("contracts.os.replace", side_effect=OSError("fixture publish failure")):
            with self.assertRaises(OSError):
                write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(list(self.root.glob(".report.json.*")), [])

    def test_final_state_is_not_claimed_stable_while_writers_remain(self):
        report = base_report()
        report["shutdown"].update(writers_stopped=False, forced=True)
        result = finalize(self.root, {}, report)
        self.assertFalse(result["final_state_saved"])
        self.assertFalse(result["logging_complete"])

    def test_optional_action_limit_has_an_explicit_stop_reason(self):
        (self.root / "environment").mkdir()
        write_json(self.root / "environment/metrics.json", {"status": "TIMEOUT", "run_total_actions": 5000})
        report = base_report()
        report.update(stop_reason="environment_exit", limits={"seconds": 7200, "max_actions": 5000})
        result = finalize(self.root, {}, report)
        self.assertEqual(result["stop_reason"], "action_limit")
        self.assertEqual(result["status"], "stopped")
