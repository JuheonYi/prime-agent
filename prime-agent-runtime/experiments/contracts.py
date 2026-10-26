"""Small environment lifecycle contract; tools and task semantics stay environment-owned."""

from dataclasses import dataclass, fields
import json
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RunLimits:
    seconds: int
    max_actions: int
    max_model_requests: int
    max_output_tokens_per_request: int
    max_model_output_tokens: int
    model_concurrency: int
    action_cap: int

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")
        if self.max_output_tokens_per_request > self.max_model_output_tokens:
            raise ValueError("per-request output cannot exceed the aggregate output budget")


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
