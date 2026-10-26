"""Opt-in copies of successfully saved harness states; never a state writer."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ERROR_PREFIX = "harness snapshot error:"
ENV_NAME = "PRIME_AGENT_HARNESS_SNAPSHOTS"


def record(
    state_path: Path,
    state: dict[str, Any],
    *,
    scope: str,
    source: str,
    session_id: str | None,
    refinement_id: str | None = None,
) -> None:
    if os.environ.get(ENV_NAME) != "1":
        return
    directory = state_path.parent / "snapshots"
    try:
        identifier = f"{time.time_ns():020d}-{uuid4().hex}"
        temporary = directory / f"{identifier}.tmp"
        directory.mkdir(exist_ok=True)
        payload = {
            "schema": 2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "session_id": session_id,
            "scope": scope,
            "refinement_id": refinement_id,
            "state": state,
        }
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
            os.replace(temporary, directory / f"{identifier}.json")
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as error:
        message = f"{ERROR_PREFIX} {state_path}: {error}\n"
        # The REPL's original host-stderr channel never enters a tool result.
        repl = sys.modules.get("rlm.repl")
        descriptor = getattr(repl, "_host_stderr_fd", -1)
        if descriptor >= 0:
            os.write(descriptor, message.encode("utf-8"))
        else:
            sys.stderr.write(message)
