"""Use the pinned PRO-LONG ARC environment and GameRunner without rewriting their policy."""

from __future__ import annotations

from dataclasses import asdict
import logging
from functools import wraps
from math import inf
from pathlib import Path

from arc_agi import Arcade, OperationMode
from prolong_agent.environment import ArcAgi3Env
from prolong_agent.environment.runner import GameRunner
from contracts import write_json

PROLONG_REVISION = "9d2f2d46fea8759ed494ce5b0166c7004a2e97c4"


class ArcEnvironment:
    def __init__(self, task: str, data: Path, output: Path, *, max_actions: int | None = None):
        self.task, self.data, self.output = task, data, output
        self.max_actions = inf if max_actions is None else max_actions
        self.env = None
        self.arcade = None
        self.card = None
        self.metrics = None

    def prepare(self):
        self.output.mkdir()
        logger = logging.getLogger("arc-evaluation")
        logger.setLevel(logging.WARNING)
        self.arcade = Arcade(operation_mode=OperationMode.OFFLINE,
                             environments_dir=str(self.data), logger=logger)
        self.card = self.arcade.open_scorecard(tags=["prime-agent", "prolong-adapter"])
        self.env = ArcAgi3Env.from_arcade(
            arcade=self.arcade, game_id=self.task, scorecard_id=self.card,
            # The pinned upstream uses this only as a numeric comparison bound.
            max_actions=self.max_actions,
        )
        (self.output / "logs.txt").write_text("")

    def run(self, agent, *, check_stop=None):
        if self.env is None:
            raise RuntimeError("environment not prepared")
        original_step = self.env.step
        if check_stop is not None:
            @wraps(original_step)
            def step(*args, **kwargs):
                check_stop()
                return original_step(*args, **kwargs)
            self.env.step = step
        runner = GameRunner(
            env=self.env, game_id=self.task, agent_name="prime-agent",
            max_actions_per_game=self.max_actions,
            prompts_log_path=self.output / "logs.txt", agent=agent,
            log_post_board=True, agent_kwargs={"grid_mode": "hex"},
        )
        try:
            self.metrics = runner.run()
        finally:
            self.env.step = original_step
        report = self.evaluate()
        write_json(self.output / "metrics.json", report)
        return report

    def evaluate(self):
        if self.metrics is None:
            raise RuntimeError("PRO-LONG has not returned evaluation metrics")
        return asdict(self.metrics)

    def close(self):
        if self.env is not None:
            self.env.close()
        if self.arcade is not None and self.card is not None:
            self.arcade.close_scorecard(self.card)
