"""Small environment lifecycle contract; tools and task semantics stay environment-owned."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class RunLimits:
    seconds: int
    max_actions: int | None = None

    def __post_init__(self):
        if type(self.seconds) is not int or self.seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        if self.max_actions is not None and (type(self.max_actions) is not int or self.max_actions <= 0):
            raise ValueError("max_actions must be a positive integer when supplied")


SHUTDOWN_GRACE_SECONDS = 10


def write_json(path: Path, value: Any) -> None:
    """Publish a complete, owner-readable report without exposing a partial file."""
    content = json.dumps(value, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def process_start(pid: int) -> int | None:
    """Identify a live Linux process without confusing PID reuse with our child."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return None if fields[0] == "Z" else int(fields[19])


class Environment(Protocol):
    def prepare(self) -> None: ...
    def run(self, agent: Any, *, check_stop: Callable[[], None] | None = None) -> dict[str, Any]: ...
    def evaluate(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def capture_states(agent_dir: Path) -> dict[str, Any]:
    return {
        str(path.relative_to(agent_dir)): json.loads(path.read_text())
        for path in sorted(agent_dir.rglob("harness_state.json"))
    }


def snapshot_errors(root: Path) -> list[str]:
    paths = (
        list(root.rglob("kernel-stderr.log"))
        + list(root.glob("stderr*.log"))
        + list(root.rglob("worker-*.stderr.log"))
    )
    return [
        line for path in paths for line in path.read_text().splitlines()
        if "harness snapshot error:" in line
    ]
