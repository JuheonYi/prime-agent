import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from model_gateway import ModelGateway, validate_payload
from run import configure_agent
from sandbox import command

REPO = Path(__file__).resolve().parents[3]
DATA = Path("/data/juheon/primeagent")


class ModelPolicyTests(unittest.TestCase):
    def payload(self):
        return {"model": "gpt-5.6-sol", "input": [], "stream": True,
                "store": False, "max_output_tokens": 1024}

    def test_function_tools_are_allowed_without_payload_rewriting(self):
        payload = self.payload()
        payload["tools"] = [{"type": "function", "name": "python_repl", "parameters": {}}]
        before = json.dumps(payload)
        self.assertEqual(validate_payload(payload), 1024)
        self.assertEqual(json.dumps(payload), before)

    def test_model_search_remote_inputs_routes_and_invalid_shapes_are_rejected(self):
        cases = [
            {"model": "another-model"}, {"max_output_tokens": -1}, {"max_output_tokens": True},
            {"tools": [{"type": "web_search"}]}, {"tools": [{"type": "mcp", "server_url": "https://example.org"}]},
            {"tool_choice": {"type": "web_search"}}, {"base_url": "https://example.org"},
            {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.org/image"}]}]},
            {"input": [{"role": "user", "content": [{"type": "input_file", "file_url": "https://example.org/file"}]}]},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_payload(self.payload() | change)


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        yield b'data: {"fixture":true}\n\n'
        yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":2}}}\n\n'


class IsolationIntegrationTests(unittest.TestCase):
    def test_bwrap_exposes_only_the_fixed_model_channel(self):
        with tempfile.TemporaryDirectory(prefix="prolong-isolation-") as temporary:
            root = Path(temporary)
            for name in ("agent", "workspace", "io"):
                (root / name).mkdir()
            configure_agent(root / "agent")
            gateway = ModelGateway(root / "io/model.sock", Path("/unused"), Path("/unused"),
                                   root / "requests.jsonl")
            calls = []

            class Session:
                trust_env = True

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def post(self, url, **kwargs):
                    calls.append((url, kwargs))
                    return FakeResponse()

            code = r'''
import json, os, socket, requests
import agent_message, attach_image, compact, edit, goal, refine
assert not os.path.exists("/home/juheon/.ssh")
assert not os.path.exists("/data/juheon/primeagent/benchmarks")
assert not os.path.exists("/data/juheon/primeagent/copilot-native")
assert not os.path.exists("/var/run/docker.sock")
assert not os.path.exists("/io/env.sock")
for address in (("1.1.1.1",443),("127.0.0.1",9152)):
    try:
        socket.create_connection(address,timeout=0.2)
    except OSError:
        pass
    else:
        raise AssertionError("unauthorized network channel reachable")
payload={"model":"gpt-5.6-sol","input":[],"stream":True,"store":False,"max_output_tokens":1024}
r=requests.post("http://127.0.0.1:9151/responses",json=payload,timeout=5)
assert r.status_code==200 and '"fixture":true' in r.text
assert requests.post("http://127.0.0.1:9151/responses",json=payload,timeout=5).status_code==200
assert requests.post("http://127.0.0.1:9151/models",json=payload,timeout=5).status_code==403
payload["tools"]=[{"type":"web_search"}]
assert requests.post("http://127.0.0.1:9151/responses",json=payload,timeout=5).status_code==400
print("ISOLATION_PASSED")
'''
            try:
                gateway.start(time.monotonic() + 30)
                with patch.object(gateway, "credentials", return_value={
                    "access": "fixture-token", "endpoint": "https://api.enterprise.githubcopilot.com"
                }), patch("model_gateway.requests.Session", Session):
                    launch = command(DATA / "build/target/debug/prime-agent", REPO, DATA / "envs/kernel",
                                     DATA / "envs/python", root, root / "io",
                                     [str(DATA / "envs/kernel/bin/python"), "-c", code])
                    result = subprocess.run(launch, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn("ISOLATION_PASSED", result.stdout)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][0], "https://api.enterprise.githubcopilot.com/responses")
                self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer fixture-token")
                self.assertEqual(gateway.count, 2)
            finally:
                gateway.close()
