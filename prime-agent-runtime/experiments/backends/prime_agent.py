"""PRO-LONG backend over an existing daemon session, retaining kernel and subagents."""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path

from prolong_agent.agent.base import BaseAgent

from model_gateway import MODEL
from sandbox import command


class PrimeAgentBackend(BaseAgent):
    def __init__(self, binary: Path, repo: Path, kernel: Path, python_home: Path,
                 output: Path, io: Path, deadline: float, *, faux: dict | None = None):
        super().__init__(grid_mode="hex", log_window=None, workspace="/workspace")
        self.binary, self.repo, self.kernel, self.python_home = binary, repo, kernel, python_home
        self.output, self.io, self.deadline = output, io, deadline
        self.faux = faux
        self.process = None
        self.events = queue.Queue()
        self.session_id = None
        self.calls = 0
        self.rpc_id = 0
        self.active_session_id = None
        self.session_file = None
        self.session_offset = 0
        self.connection = None
        self.input = None
        self.output_stream = None
        self.daemon_log = None
        self.log = None
        self.stderr = None
        self.reader = None

    def _start(self, available_actions):
        task_prompt = self._build_system_prompt(available_actions)
        (self.output / "task-prompt.txt").write_text(task_prompt)
        arguments = ["/app/prime-agent", "--mode", "daemon", "--daemon-socket", "/run/agent/eval.sock"]
        launch = command(self.binary, self.repo, self.kernel, self.python_home,
                         self.output, self.io, arguments)
        self.log = (self.output / "console.jsonl").open("w")
        self.stderr = (self.output / "stderr.log").open("w")
        self.daemon_log = (self.output / "daemon.log").open("w")
        self.process = subprocess.Popen(launch, stdin=subprocess.DEVNULL, stdout=self.daemon_log,
                                        stderr=self.stderr)
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        while True:
            try:
                self.connection.connect(str(self.output / "agent/eval.sock"))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if self.process.poll() is not None or time.monotonic() >= self.deadline:
                    raise RuntimeError("Prime daemon failed to start; see stderr.log")
                time.sleep(0.02)
        self.input = self.connection.makefile("r")
        self.output_stream = self.connection.makefile("w")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        hello = self._next()
        if hello.get("type") != "daemon_hello":
            raise RuntimeError("invalid Prime daemon handshake")
        config = {
            "cwd": "/work", "sessionDir": "/run/agent/sessions", "provider": "github-copilot",
            "model": MODEL, "thinking": "medium", "appendSystemPrompt": [task_prompt],
        }
        if self.faux is not None:
            config.update(provider="faux", model="faux-1", thinking="off")
            (self.output / "agent/faux.json").write_text(json.dumps({"engine": "faux", **self.faux}))
            config["script"] = "/run/agent/faux.json"
            if "child" in self.faux:
                (self.output / "agent/faux-child.json").write_text(json.dumps({"engine": "faux", **self.faux["child"]}))
                config["childScript"] = "/run/agent/faux-child.json"
        created = self._rpc("create", config=config, telemetryDisabled=True)["data"]
        self.active_session_id = created.get("activeSessionId") or created.get("id")
        if not self.active_session_id or not created.get("sessionFile"):
            raise RuntimeError("daemon did not return a persistent session identity")
        self.session_id = Path(created["sessionFile"]).stem
        self.session_file = self.output / "agent/sessions" / f"{self.session_id}.jsonl"
        self._rpc("attach")
        (self.output / "backend.json").write_text(json.dumps({
            "session_id": self.session_id, "mode": "persistent-daemon-session", "action_cap": self._action_cap,
            "upstream_prompt": "BaseAgent._build_system_prompt", "base_prompt_replaced": False,
            "autonomous_driver": "PRO-LONG GameRunner; native tool loop remains unchanged",
        }, indent=2))

    def _read(self):
        try:
            for line in self.input:
                self.log.write(line)
                self.log.flush()
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    self.events.put(RuntimeError("invalid Prime Agent daemon output; see console.jsonl"))
        finally:
            self.events.put(EOFError("Prime Agent daemon connection closed"))

    def _next(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("experiment time limit")
        try:
            event = self.events.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("experiment time limit") from None
        if isinstance(event, BaseException):
            raise event
        return event

    def _rpc(self, kind, **values):
        self.rpc_id += 1
        request_id = f"backend-{self.rpc_id}"
        command = {"type": kind, **values}
        if self.active_session_id and kind not in ("create", "shutdown"):
            command["activeSessionId"] = self.active_session_id
        self.output_stream.write(json.dumps({
            "type": "command", "id": request_id,
            "protocol": {"name": "prime-agent.daemon", "version": 7}, "command": command,
        }) + "\n")
        self.output_stream.flush()
        while True:
            event = self._next()
            if event.get("type") == "response" and event.get("id") == request_id:
                if not event.get("success"):
                    raise RuntimeError(f"Prime Agent {kind} failed: {event.get('error', 'unknown error')}")
                return event

    def analyze(self, log_path: Path, action_num: int, retry_nudge: str = "", **kwargs):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("experiment time limit")
        sandbox = self.output / "workspace"
        self._sync_history(log_path, sandbox)
        self._add_current_board(log_path, kwargs)
        first = self.process is None
        if first:
            self._start(self._available_actions(kwargs))
        self._clear_files(sandbox, "actions.json")
        prompt = self._build_prompt(log_path.name, first, action_num=action_num, **kwargs)
        if retry_nudge:
            prompt += f"\n\n{retry_nudge}"
        self.calls += 1
        with (self.output / "prompts.jsonl").open("a") as stream:
            stream.write(json.dumps({"call": self.calls, "action": action_num, "prompt": prompt}) + "\n")
        self._rpc("prompt_and_wait", message=prompt)
        self._rpc("wait_for_idle")
        text = ""
        usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
        with self.session_file.open() as stream:
            stream.seek(self.session_offset)
            for line in stream:
                message = json.loads(line).get("message", {})
                if message.get("role") == "assistant":
                    if message.get("stopReason") in ("error", "aborted"):
                        raise RuntimeError(message.get("errorMessage") or "model request failed")
                    metrics = message.get("usage", {})
                    usage["input_tokens"] += metrics.get("input", 0)
                    usage["cached_tokens"] += metrics.get("cacheRead", 0)
                    usage["output_tokens"] += metrics.get("output", 0)
                    answer = "".join(block.get("text", "") for block in message.get("content", [])
                                     if block.get("type") == "text")
                    if answer:
                        text = answer
            self.session_offset = stream.tell()
        action_file = sandbox / "actions.json"
        actions = self._parse_actions_json_text(action_file.read_text(), self._action_cap) if action_file.exists() else []
        hint, plan = self._split_response(text)
        return {"hint": hint, "plan": plan, "actions": actions,
                "meta": {"output": text, **usage, "model": MODEL, "cumulative": False}}

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.connection is not None:
                self.connection.shutdown(socket.SHUT_RDWR)
            if self.reader is not None:
                self.reader.join(timeout=5)
        for stream in (self.input, self.output_stream, self.connection, self.log, self.stderr, self.daemon_log):
            if stream is not None:
                stream.close()
