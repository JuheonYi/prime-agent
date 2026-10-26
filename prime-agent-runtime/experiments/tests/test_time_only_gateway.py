import contextlib
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from model_gateway import ModelGateway
from run import model_usage


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class UsageResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":4096}}}\n\n'


class RecordingSession:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def post(self, url, **kwargs):
        return UsageResponse()


@contextlib.contextmanager
def gateway_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        gateway = ModelGateway(root / "model.sock", Path("/unused"), Path("/unused"), root / "requests.jsonl")
        try:
            gateway.start(time.monotonic() + 600)
            with patch.object(gateway, "credentials", return_value={
                "access": "fixture", "endpoint": "https://api.enterprise.githubcopilot.com",
            }):
                yield gateway, root
        finally:
            gateway.close()


def request(gateway, payload=None):
    if payload is None:
        payload = {"model": "gpt-5.6-sol", "input": [], "store": False,
                   "stream": True, "max_output_tokens": 128000}
    with contextlib.closing(UnixConnection(gateway.socket)) as connection:
        connection.request("POST", "/responses", json.dumps(payload), {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        return response.status, body


class TimeOnlyGatewayTests(unittest.TestCase):
    def test_crosses_previous_request_and_token_budgets_without_stopping(self):
        with gateway_fixture() as (gateway, root), patch("model_gateway.requests.Session", RecordingSession):
            for _ in range(270):
                status, _ = request(gateway)
                self.assertEqual(status, 200)
            self.assertEqual(gateway.count, 270)
            usage = model_usage(root / "requests.jsonl")
            self.assertEqual(usage["responses_with_usage"], 270)
            self.assertEqual(usage["output_tokens"], 270 * 4096)
            self.assertGreater(usage["output_tokens"], 131072)

    def test_more_than_four_concurrent_requests_are_admitted(self):
        ready = threading.Condition()
        release = threading.Event()
        active = []

        class ConcurrentSession(RecordingSession):
            def post(self, url, **kwargs):
                with ready:
                    active.append(kwargs)
                    ready.notify_all()
                if not release.wait(timeout=10):
                    raise RuntimeError("concurrency fixture did not release")
                return UsageResponse()

        with gateway_fixture() as (gateway, _), patch("model_gateway.requests.Session", ConcurrentSession):
            with ThreadPoolExecutor(max_workers=6) as pool:
                pending = [pool.submit(request, gateway) for _ in range(6)]
                try:
                    with ready:
                        self.assertTrue(ready.wait_for(lambda: len(active) == 6, timeout=5))
                finally:
                    release.set()
                self.assertEqual([future.result()[0] for future in pending], [200] * 6)
            self.assertEqual(gateway.count, 6)

    def test_request_size_and_read_timeout_do_not_add_experiment_limits(self):
        calls = []

        class Session(RecordingSession):
            def post(self, url, **kwargs):
                calls.append(kwargs)
                return UsageResponse()

        payload = {"model": "gpt-5.6-sol", "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "x" * (4 * 1024 * 1024 + 1)},
        ]}], "store": False, "stream": True}
        with gateway_fixture() as (gateway, _), patch("model_gateway.requests.Session", Session):
            status, _ = request(gateway, payload)
            self.assertEqual(status, 200)
            self.assertEqual(calls[0]["json"], payload)
            self.assertGreater(calls[0]["timeout"][1], 90)
            self.assertLessEqual(calls[0]["timeout"][1], 600)

    def test_deadline_rejects_new_requests_without_calling_the_provider(self):
        with gateway_fixture() as (gateway, _), patch("model_gateway.requests.Session") as provider:
            gateway.deadline = time.monotonic() - 1
            status, body = request(gateway)
            self.assertEqual(status, 429)
            self.assertIn(b"experiment time limit", body)
            self.assertEqual(gateway.count, 0)
            provider.assert_not_called()

    def test_shutdown_marks_unsettled_requests_and_prevents_late_log_writes(self):
        started = threading.Event()
        release = threading.Event()

        class Session(RecordingSession):
            def post(self, url, **kwargs):
                started.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("fixture release missing")
                return UsageResponse()

        with gateway_fixture() as (gateway, root), patch("model_gateway.requests.Session", Session):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(request, gateway)
                self.assertTrue(started.wait(timeout=5))
                try:
                    errors = gateway.close(grace_seconds=0)
                    self.assertEqual(len(errors), 1)
                    before = (root / "requests.jsonl").read_bytes()
                    self.assertIn(b'"event": "interrupted"', before)
                finally:
                    release.set()
                pending.result()
                self.assertEqual((root / "requests.jsonl").read_bytes(), before)
