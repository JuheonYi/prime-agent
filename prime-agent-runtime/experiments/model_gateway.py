"""Copilot-only HTTP gateway on a private Unix socket; no general proxy route."""

from __future__ import annotations

import json
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import requests

MODEL = "gpt-5.6-sol"
ALLOWED_KEYS = {
    "model", "input", "instructions", "stream", "store", "max_output_tokens",
    "temperature", "top_p", "reasoning", "include", "tools", "tool_choice",
    "parallel_tool_calls", "text", "prompt_cache_key", "prompt_cache_retention",
    "truncation", "metadata",
}
HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.48.1", "Editor-Version": "vscode/1.136.1",
    "Editor-Plugin-Version": "copilot-chat/0.48.1", "Copilot-Integration-Id": "vscode-chat",
    "Content-Type": "application/json", "Accept-Encoding": "identity",
}


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) - ALLOWED_KEYS:
        raise ValueError("unsupported request fields")
    if payload.get("model") != MODEL:
        raise ValueError("model is not allowed")
    if payload.get("store") is not False or payload.get("stream") is not True:
        raise ValueError("only non-stored streaming requests are allowed")
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("server-side tools including web search are forbidden")
    choice = payload.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") != "function":
        raise ValueError("server-side tool selection is forbidden")
    tokens = payload.get("max_output_tokens")
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ValueError("max_output_tokens must be a positive integer when supplied")
    items = payload.get("input", [])
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                    if isinstance(content, dict) and content.get("type") in ("input_image", "input_file"):
                        image = content.get("image_url", "")
                        if content.get("type") != "input_image" or not isinstance(image, str) or not image.startswith("data:image/"):
                            raise ValueError("remote images and files are forbidden")
    return tokens


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        connection, _ = super().get_request()
        connection.settimeout(15)
        return connection, ("local", 0)


class ModelGateway:
    def __init__(self, socket: Path, helper: Path, agent_dir: Path, log: Path):
        self.socket, self.helper, self.agent_dir, self.log = socket, helper, agent_dir, log
        self.deadline = 0.0
        self.count = 0
        self.lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                self.send_error(403, "CONNECT forbidden")

            def do_GET(self):
                self.send_error(404)

            def do_POST(self):
                if self.path != "/responses":
                    self.send_error(403, "route forbidden")
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    self.send_error(411)
                    return
                try:
                    length = int(lengths[0])
                    if length <= 0:
                        raise ValueError("request size")
                    payload = json.loads(self.rfile.read(length))
                    tokens = validate_payload(payload)
                except (ValueError, TypeError):
                    self.send_error(400, "invalid or forbidden model request")
                    return
                with gateway.lock:
                    if time.monotonic() >= gateway.deadline:
                        self.send_error(429, "experiment time limit")
                        return
                    gateway.count += 1
                    number = gateway.count
                    with gateway.log.open("a") as stream:
                        stream.write(json.dumps({"event": "started", "request": number,
                                                 "model": MODEL, "output_cap": tokens}) + "\n")
                started = time.monotonic()
                record = {"event": "finished", "request": number, "model": MODEL, "reasoning": payload.get("reasoning"),
                          "output_cap": tokens, "status": None}
                sent_headers = False
                usage = None
                try:
                    credential = gateway.credentials()
                    headers = dict(HEADERS, Authorization=f"Bearer {credential['access']}")
                    for name in ("X-ACP-Model-Request-ID", "Idempotency-Key", "session_id", "x-client-request-id"):
                        if name in self.headers:
                            headers[name] = self.headers[name]
                    remaining = gateway.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("experiment deadline")
                    with requests.Session() as session:
                        session.trust_env = False
                        with session.post(credential["endpoint"] + "/responses", json=payload,
                                          headers=headers, stream=True, allow_redirects=False,
                                          timeout=(min(10, remaining), remaining)) as response:
                            record["status"] = response.status_code
                            self.send_response(response.status_code)
                            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                            self.send_header("Connection", "close")
                            self.end_headers()
                            sent_headers = True
                            self.close_connection = True
                            pending = b""
                            for chunk in response.iter_content(chunk_size=1024):
                                if time.monotonic() >= gateway.deadline:
                                    raise TimeoutError("experiment deadline")
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                pending += chunk
                                while b"\n" in pending:
                                    line, pending = pending.split(b"\n", 1)
                                    if line.startswith(b"data:") and line[5:].lstrip().startswith(b"{"):
                                        event = json.loads(line[5:])
                                        if event.get("type") == "response.completed":
                                            usage = event.get("response", {}).get("usage")
                except (requests.RequestException, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    record["error_type"] = type(error).__name__
                    if not sent_headers:
                        self.send_error(502, "native Copilot transport failed")
                finally:
                    record["seconds"] = time.monotonic() - started
                    with gateway.lock, gateway.log.open("a") as stream:
                        if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
                            record["usage"] = usage
                        stream.write(json.dumps(record) + "\n")

        self.server = UnixServer(str(socket), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def credentials(self):
        result = subprocess.run([str(self.helper), str(self.agent_dir)], capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError("native credential resolution failed")
        credential = json.loads(result.stdout)
        url = urlsplit(credential["endpoint"])
        if url.scheme != "https" or url.hostname != "api.enterprise.githubcopilot.com" or url.port is not None or url.path:
            raise ValueError("unapproved Copilot endpoint")
        return credential

    def start(self, deadline: float):
        self.deadline = deadline
        self.thread.start()

    def close(self):
        self.deadline = 0
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.socket.unlink(missing_ok=True)
