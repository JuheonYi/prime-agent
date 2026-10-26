"""Local observation/action bridge for the faux-only ARC wiring check."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
from arc_agi import Arcade, OperationMode


class ArcBridge:
    def __init__(self, environments: Path, recordings: Path, *, download: bool = False):
        logger = logging.getLogger("prime-agent.arc-pilot")
        logger.setLevel(logging.WARNING)
        if download:
            provisioner = Arcade(
                operation_mode=OperationMode.NORMAL,
                environments_dir=str(environments),
                logger=logger,
            )
            if provisioner.make("ls20") is None:
                raise RuntimeError("could not download ls20")
        self.arcade = Arcade(
            operation_mode=OperationMode.OFFLINE,
            environments_dir=str(environments),
            recordings_dir=str(recordings),
            logger=logger,
        )
        self.environment = self.arcade.make("ls20", seed=0, save_recording=True, include_frame_data=True)
        if self.environment is None:
            raise RuntimeError("ls20 unavailable; use --download-game once to provision the public environment")
        self.observation = self.environment.reset()
        self.actions = 0
        self.lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/observe":
                    self.send_error(404)
                    return
                with bridge.lock:
                    self.reply(bridge.visible_state())

            def do_POST(self) -> None:
                if self.path != "/step":
                    self.send_error(404)
                    return
                with bridge.lock:
                    if bridge.actions >= 10:
                        self.send_error(409, "faux action budget exhausted")
                        return
                    action = bridge.environment.action_space[0]
                    data = {"x": 0, "y": 0} if action.is_complex() else {}
                    bridge.observation = bridge.environment.step(action, data=data)
                    bridge.actions += 1
                    self.reply(bridge.visible_state())

            def reply(self, data: dict[str, Any]) -> None:
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def visible_state(self) -> dict[str, Any]:
        if self.observation is None:
            raise RuntimeError("ARC returned no observation")
        return {
            "state": self.observation.state.value,
            "levels_completed": self.observation.levels_completed,
            "frame": np.asarray(self.observation.frame).tolist(),
            "actions": self.actions,
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
