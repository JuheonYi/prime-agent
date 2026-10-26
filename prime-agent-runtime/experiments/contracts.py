"""Small environment lifecycle contract; tools and task semantics stay environment-owned."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RunLimits:
    seconds: int

    def __post_init__(self):
        if type(self.seconds) is not int or self.seconds <= 0:
            raise ValueError("seconds must be a positive integer")


class Environment(Protocol):
    def prepare(self) -> None: ...
    def run(self, agent: Any) -> dict[str, Any]: ...
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
