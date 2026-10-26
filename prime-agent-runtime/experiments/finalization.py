"""Capture committed harness state before parsing fallible experiment logs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from contracts import write_json


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_record(path: Path, root: Path, error: Exception, *, line: int | None = None, kind="read_error") -> dict:
    return {"file": str(path.relative_to(root)), "line": line, "kind": kind,
            "error": f"{type(error).__name__}: {error}"}


def read_json(path: Path, root: Path, errors: list[dict]) -> dict | None:
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, ValueError) as error:
        errors.append(error_record(path, root, error))
        return None


def read_jsonl(path: Path, root: Path, errors: list[dict]):
    try:
        with path.open("rb") as stream:
            for number, raw in enumerate(stream, 1):
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError("expected a JSON object")
                except (ValueError, UnicodeError) as error:
                    kind = "truncated_record" if not raw.endswith(b"\n") else "invalid_record"
                    errors.append(error_record(path, root, error, line=number, kind=kind))
                    continue
                yield number, row
    except OSError as error:
        errors.append(error_record(path, root, error))


def capture_harness(root: Path, initial: dict, stop_reason: str, *, writers_stopped: bool) -> dict:
    agent = root / "agent"
    states, state_errors, snapshot_errors = {}, [], []
    changed, recovered = [], []
    captured_at = timestamp()
    for path in sorted(agent.rglob("harness_state.json")):
        state = read_json(path, root, state_errors)
        if state is None:
            continue
        if state.get("schema") != 1 or not isinstance(state.get("entries"), dict) or not isinstance(state.get("refinements"), list):
            state_errors.append(error_record(path, root, ValueError("invalid harness state schema")))
            continue
        states[str(path.relative_to(agent))] = state
    saved = False
    try:
        write_json(root / "final-harness-states.json", states)
        saved = not state_errors and writers_stopped
    except OSError as error:
        state_errors.append(error_record(root / "final-harness-states.json", root, error))

    empty = {"schema": 1, "entries": {kind: {} for kind in ("prompt", "memory", "skill", "subagent", "factory")},
             "refinements": []}
    for relative, state in states.items():
        path = agent / relative
        different = state != initial.get(relative, empty)
        if different:
            changed.append(relative)
        snapshots = sorted((path.parent / "snapshots").glob("*.json"))
        last = None
        for snapshot_path in snapshots:
            snapshot = read_json(snapshot_path, root, snapshot_errors)
            if snapshot is not None and not isinstance(snapshot.get("state"), dict):
                snapshot_errors.append(error_record(snapshot_path, root, ValueError("missing snapshot state")))
                snapshot = None
            last = snapshot
        if (snapshots and (last is None or last["state"] != state)) or (different and not snapshots):
            snapshot_errors.append(error_record(path, root, ValueError("latest snapshot does not match committed state"),
                                                kind="snapshot_gap"))
            parts = path.parts
            session_id = parts[parts.index("session-artifacts") + 1] if "session-artifacts" in parts else None
            snapshot_path = path.parent / "snapshots" / f"{time.time_ns():020d}-{uuid4().hex}.json"
            try:
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(snapshot_path, {
                    "schema": 2, "timestamp": captured_at, "source": "run_end",
                    "session_id": session_id, "scope": "local" if session_id else "global",
                    "refinement_id": None, "stop_reason": stop_reason,
                    "writers_stopped": writers_stopped, "state": state,
                })
                recovered.append(str(snapshot_path.relative_to(root)))
            except OSError as error:
                snapshot_errors.append(error_record(snapshot_path, root, error))
    for relative in initial.keys() - states.keys():
        changed.append(relative)
        state_errors.append(error_record(agent / relative, root, ValueError("initial harness store missing or unreadable")))
    return {
        "final_state_saved": saved and not state_errors, "final_state_captured_at": captured_at,
        "harness_stores": len(states), "changed_harness_stores": changed,
        "recovered_final_snapshots": recovered, "state_errors": state_errors,
        "snapshot_errors": snapshot_errors,
        "snapshots": len(list(agent.rglob("snapshots/*.json"))),
    }


def model_usage(log: Path, errors: list[dict] | None = None):
    errors = [] if errors is None else errors
    rows = list(read_jsonl(log, log.parent, errors)) if log.exists() else []
    completed = []
    for line, row in rows:
        if "usage" not in row:
            continue
        usage = row["usage"]
        details = usage.get("input_tokens_details", {}) if isinstance(usage, dict) else None
        values = [usage.get("input_tokens", 0), usage.get("output_tokens", 0)] if isinstance(usage, dict) else []
        if isinstance(details, dict):
            values.append(details.get("cached_tokens", 0))
        if not isinstance(usage, dict) or not isinstance(details, dict) or any(type(value) is not int or value < 0 for value in values):
            errors.append(error_record(log, log.parent, ValueError("invalid model usage"), line=line))
            continue
        completed.append(row)
    return {
        "responses_with_usage": len(completed),
        "input_tokens": sum(row["usage"].get("input_tokens", 0) for row in completed),
        "cached_input_tokens": sum(row["usage"].get("input_tokens_details", {}).get("cached_tokens", 0) for row in completed),
        "output_tokens": sum(row["usage"].get("output_tokens", 0) for row in completed),
        "note": "Completed responses only; interrupted or malformed records may have unreported usage. Billing is not inferred.",
    }


def collect_diagnostics(root: Path) -> dict:
    log_errors, model_errors, refinement_errors, snapshot_errors = [], [], [], []
    for path in (root / "agent").rglob("*.jsonl"):
        for _, row in read_jsonl(path, root, log_errors):
            message = row.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("stopReason") == "error":
                model_errors.append(message.get("errorMessage", "model request failed"))
    console = root / "console.jsonl"
    if console.exists():
        for line, row in read_jsonl(console, root, log_errors):
            event = row.get("event", row)
            if isinstance(event, dict) and event.get("type") == "refine_failed":
                refinement_errors.append({"file": str(console.relative_to(root)), "line": line,
                                          "error": event.get("error", "refinement failed")})
    paths = set(root.rglob("kernel-stderr.log")) | set(root.glob("stderr*.log")) | set(root.rglob("worker-*.stderr.log"))
    for path in sorted(paths):
        try:
            with path.open("rb") as stream:
                for number, raw in enumerate(stream, 1):
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeError as error:
                        log_errors.append(error_record(path, root, error, line=number))
                        continue
                    if "harness snapshot error:" in line:
                        snapshot_errors.append(error_record(path, root, RuntimeError(line.strip()), line=number,
                                                            kind="snapshot_write_error"))
                    elif re.search(r"refinement.*failed|refinement.*error", line, re.IGNORECASE):
                        refinement_errors.append({"file": str(path.relative_to(root)), "line": number, "error": line.strip()})
        except OSError as error:
            log_errors.append(error_record(path, root, error))
    usage = model_usage(root / "model-requests.jsonl", log_errors)
    return {"log_errors": log_errors, "model_errors": model_errors, "refinement_errors": refinement_errors,
            "snapshot_errors": snapshot_errors, "usage": usage}


def environment_result(root: Path, errors: list[dict]):
    path = root / "environment/metrics.json"
    if path.exists():
        result = read_json(path, root, errors)
        if result is not None and isinstance(result.get("status"), str) and isinstance(result.get("run_total_actions"), int):
            return result
        if result is not None:
            errors.append(error_record(path, root, ValueError("invalid environment metrics")))
    log = root / "environment/logs.txt"
    actions = []
    if log.exists():
        try:
            with log.open() as stream:
                for line in stream:
                    match = re.match(r"^Action (\d+) \|.*?Score: (\d+)", line)
                    if match:
                        actions.append((int(match[1]), int(match[2])))
        except (OSError, UnicodeError) as error:
            errors.append(error_record(log, root, error))
    return {"status": "INTERRUPTED", "partial_metrics": True,
            "run_total_actions": max((action for action, _ in actions), default=0),
            "final_score": max((score for _, score in actions), default=0),
            "note": "Partial progress parsed from the original PRO-LONG log."}


def finalize(root: Path, initial: dict, report: dict) -> dict:
    shutdown = report["shutdown"]
    harness = capture_harness(root, initial, report["stop_reason"], writers_stopped=shutdown["writers_stopped"])
    diagnostics = collect_diagnostics(root)
    diagnostics["snapshot_errors"] = harness.pop("snapshot_errors") + diagnostics["snapshot_errors"]
    final = environment_result(root, diagnostics["log_errors"])
    if report["stop_reason"] == "environment_exit":
        if final["status"] == "COMPLETED_RUN":
            report["stop_reason"] = "game_completed"
        elif report["limits"]["max_actions"] is not None and final["run_total_actions"] >= report["limits"]["max_actions"]:
            report["stop_reason"] = "action_limit"
    failed = bool(report.get("runner_error") or diagnostics["model_errors"] or diagnostics["refinement_errors"]
                  or (report["stop_reason"] == "environment_exit" and report.get("worker_exit_code") not in (None, 0))
                  or (final["status"] == "ERROR" and report["stop_reason"] == "environment_exit"))
    complete = not (harness["state_errors"] or diagnostics["snapshot_errors"] or diagnostics["log_errors"]
                    or shutdown["forced"] or shutdown["errors"] or not shutdown["writers_stopped"])
    report.update(harness)
    report.update(diagnostics)
    report.update(environment=final, logging_complete=complete)
    report["status"] = "incomplete" if not complete else (
        "failed" if failed else "completed" if final["status"] == "COMPLETED_RUN" else "stopped"
    )
    write_json(root / "result.json", report)
    return report
