"""
Agent edge cases. The happy path (register, pin, unpin, catch up after being
offline) is covered end to end by scripts/network_e2e.py.

    python -m unittest discover -s node/agent -p "test_*.py"
"""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "coordinator" / "storage"))

import dstore_agent  # noqa: E402
from fakekubo import FakeKubo  # noqa: E402


class StubCoordinator:
    """Answers every request with a fixed status and body, and records what it got."""

    def __init__(self, status=200, body=None):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                payload = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.kubo = FakeKubo(f"{self.tmp.name}/ipfs").start()
        self.servers = [self.kubo]

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self.tmp.cleanup()

    def agent(self, coordinator_url, **env):
        return dstore_agent.Agent({
            "COORDINATOR_URL": coordinator_url,
            "KUBO_API_URL": self.kubo.api_url,
            "STATE_DIR": f"{self.tmp.name}/state",
            **env,
        })

    def coordinator(self, status=200, body=None):
        stub = StubCoordinator(status, body)
        self.servers.append(stub)
        return stub

    def test_refused_invite_is_reported_not_retried_blindly(self):
        stub = self.coordinator(403, {"detail": "That invite code is invalid, expired or already used."})
        agent = self.agent(stub.url, ENROLLMENT_CODE="dsi_old")
        peer_id, addresses = agent._wait_for_kubo()
        with self.assertRaises(dstore_agent.Fatal) as ctx:
            agent.register(peer_id, addresses)
        self.assertIn("invalid, expired or already used", str(ctx.exception))
        self.assertFalse(agent.state_path.exists())

    def test_registration_needs_an_invite_code(self):
        agent = self.agent(self.coordinator().url)
        with self.assertRaises(dstore_agent.Fatal):
            agent.register(*agent._wait_for_kubo())

    def test_registration_saves_a_private_state_file(self):
        stub = self.coordinator(201, {"node_id": 7, "token": "dsn_x", "name": "n", "operator": "Ama"})
        agent = self.agent(stub.url, ENROLLMENT_CODE="dsi_ok", NODE_NAME="harare-1")
        peer_id, addresses = agent._wait_for_kubo()
        agent.register(peer_id, addresses)
        saved = json.loads(agent.state_path.read_text())
        self.assertEqual((saved["token"], saved["peer_id"]), ("dsn_x", peer_id))
        if sys.platform != "win32":
            self.assertEqual(agent.state_path.stat().st_mode & 0o777, 0o600)
        path, body = stub.requests[0]
        self.assertEqual(path, "/api/nodes/register")
        self.assertEqual((body["code"], body["name"], body["peer_id"]), ("dsi_ok", "harare-1", peer_id))
        self.assertGreater(body["capacity_bytes"], 0)

    def test_tasks_pin_unpin_and_report(self):
        stub = self.coordinator(200, {})
        agent = self.agent(stub.url)
        agent.state = {"token": "dsn_x"}
        source = FakeKubo(f"{self.tmp.name}/source").start()
        self.servers.append(source)
        cid = source.repo.put(b"encrypted bytes")
        self.kubo.peers = [source.api_url]

        agent._run_task({"id": 1, "action": "pin", "cid": cid, "size": 15})
        self.assertIn(cid, self.kubo.repo.pins)
        agent._run_task({"id": 2, "action": "unpin", "cid": cid})
        agent._run_task({"id": 3, "action": "unpin", "cid": cid})  # already unpinned: still a success
        self.assertNotIn(cid, self.kubo.repo.pins)
        self.assertEqual(
            [(path, body["ok"]) for path, body in stub.requests],
            [("/api/nodes/tasks/1", True), ("/api/nodes/tasks/2", True), ("/api/nodes/tasks/3", True)],
        )

    def test_failed_pins_are_reported(self):
        stub = self.coordinator(200, {})
        agent = self.agent(stub.url, PIN_TIMEOUT_SECONDS="1")
        agent.state = {"token": "dsn_x"}
        agent._run_task({"id": 9, "action": "pin", "cid": "bafkreinobodyhasthisxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
        path, body = stub.requests[0]
        self.assertEqual((path, body["ok"]), ("/api/nodes/tasks/9", False))
        self.assertIn("deadline", body["error"])

    def test_malformed_and_duplicate_tasks_are_skipped(self):
        agent = self.agent(self.coordinator().url)
        started = []
        agent.workers.submit = lambda fn, task: started.append(task["id"])
        agent._dispatch([
            {"id": 1, "action": "pin", "cid": "bafkreiaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            {"id": 1, "action": "pin", "cid": "bafkreiaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            {"id": 2, "action": "rm -rf", "cid": "bafkreiaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            {"id": 3, "action": "pin", "cid": "../../etc/passwd"},
        ])
        self.assertEqual(started, [1])


if __name__ == "__main__":
    unittest.main()
