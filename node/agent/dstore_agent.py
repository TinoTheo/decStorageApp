#!/usr/bin/env python3
"""
dstore node agent.

Runs next to an IPFS (Kubo) node and connects it to the dstore network:

- registers once with the operator's invite code,
- checks in with the coordinator every few seconds (always outbound, so no
  port forwarding is needed for the agent),
- pins the encrypted segments it's asked to hold and unpins the ones it's
  asked to drop, reporting each result.

Everything it stores is encrypted on the uploader's device; the agent, the node
and its operator can't read any of it.

Settings come from environment variables (see node/.env.example). Standard
library only, Python 3.10+.
"""

import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

AGENT_VERSION = "0.2.0"
CID_RE = re.compile(r"^b[a-z2-7]{20,100}$")

log = logging.getLogger("dstore-agent")


class Fatal(Exception):
    """Something only the operator can fix (bad invite, disabled node, ...)."""


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


# IPFS runs next to the agent, so its API is never reached through a proxy.
# The coordinator is, if the machine is set up to use one.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url, *, data=None, headers=None, timeout=30, direct=False):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    opener = _DIRECT if direct else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
            message = parsed.get("detail") or parsed.get("Message") or body
        except ValueError:
            message = body
        raise HttpError(exc.code, str(message)[:500]) from exc


class Kubo:
    """The few Kubo RPC calls the agent needs."""

    def __init__(self, api_url):
        self.api_url = api_url.rstrip("/")

    def call(self, command, arg=None, timeout=30, **params):
        query = ([("arg", arg)] if arg else []) + list(params.items())
        url = f"{self.api_url}/api/v0/{command}" + ("?" + urllib.parse.urlencode(query) if query else "")
        return json.loads(_request(url, data=b"", timeout=timeout, direct=True) or b"{}")

    def identity(self):
        info = self.call("id")
        return info["ID"], info.get("Addresses") or []

    def version(self):
        return self.call("version").get("Version", "")

    def usage(self):
        stat = self.call("repo/stat", **{"size-only": "true"})
        return int(stat.get("StorageMax") or 0), int(stat.get("RepoSize") or 0)

    def pinned_count(self):
        return len(self.call("pin/ls", type="recursive", quiet="true", timeout=120).get("Keys") or {})

    def pin(self, cid, timeout):
        # Kubo's own `timeout` stops it searching the network; ours is a backstop.
        url = f"{self.api_url}/api/v0/pin/add?" + urllib.parse.urlencode(
            [("arg", cid), ("recursive", "true"), ("timeout", f"{timeout}s")]
        )
        _request(url, data=b"", timeout=timeout + 15, direct=True)

    def unpin(self, cid):
        try:
            self.call("pin/rm", cid)
        except HttpError as exc:
            if "not pinned" not in exc.message:
                raise


class Agent:
    def __init__(self, env):
        self.coordinator = env["COORDINATOR_URL"].rstrip("/")
        self.enrollment_code = env.get("ENROLLMENT_CODE", "").strip()
        self.node_name = env.get("NODE_NAME", "").strip()
        self.kubo = Kubo(env.get("KUBO_API_URL", "http://kubo:5001"))
        self.state_path = Path(env.get("STATE_DIR", "/data")) / "agent.json"
        self.heartbeat_override = float(env["HEARTBEAT_SECONDS"]) if env.get("HEARTBEAT_SECONDS") else None
        self.pin_timeout = int(env.get("PIN_TIMEOUT_SECONDS", "600"))
        self.workers = ThreadPoolExecutor(max_workers=int(env.get("CONCURRENT_TASKS", "3")))
        self.in_flight = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.state = self._load_state()
        self.pinned_count = 0
        self.beats = 0
        self.kubo_version = ""
        self.last_status = "active"

    # -- state ------------------------------------------------------------------

    def _load_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    # -- coordinator ------------------------------------------------------------

    def _api(self, path, payload, auth=True, timeout=30):
        headers = {"Content-Type": "application/json", "User-Agent": f"dstore-agent/{AGENT_VERSION}"}
        if auth:
            headers["Authorization"] = f"Node {self.state['token']}"
        raw = _request(f"{self.coordinator}{path}", data=json.dumps(payload).encode(), headers=headers, timeout=timeout)
        return json.loads(raw or b"{}")

    def _status(self, peer_id, addresses):
        capacity, used = self.kubo.usage()
        if self.beats % 20 == 0:
            self.pinned_count = self.kubo.pinned_count()
        return {
            "peer_id": peer_id,
            "capacity_bytes": capacity,
            "used_bytes": used,
            "pinned_count": self.pinned_count,
            "agent_version": AGENT_VERSION,
            "kubo_version": self.kubo_version,
            "addresses": addresses[:20],
        }

    def register(self, peer_id, addresses):
        if not self.enrollment_code:
            raise Fatal("This node isn't registered yet. Put the ENROLLMENT_CODE from your invite in .env.")
        payload = {"code": self.enrollment_code, "name": self.node_name, **self._status(peer_id, addresses)}
        try:
            result = self._api("/api/nodes/register", payload, auth=False)
        except HttpError as exc:
            if exc.status in (400, 403, 409):
                raise Fatal(f"Registration refused: {exc.message}") from exc
            raise
        self.state = {
            "node_id": result["node_id"],
            "token": result["token"],
            "peer_id": peer_id,
            "coordinator": self.coordinator,
        }
        self._save_state()
        log.info("Registered as node '%s' for operator '%s'.", result["name"], result["operator"])
        return result.get("heartbeat_seconds", 15)

    # -- tasks ------------------------------------------------------------------

    def _run_task(self, task):
        task_id, action, cid = task["id"], task["action"], task["cid"]
        ok, error = True, ""
        try:
            if action == "pin":
                started = time.monotonic()
                self.kubo.pin(cid, self.pin_timeout)
                log.info("Pinned %s (%d bytes) in %.1fs", cid, task.get("size", 0), time.monotonic() - started)
            else:
                self.kubo.unpin(cid)
                log.info("Unpinned %s", cid)
        except (HttpError, OSError, urllib.error.URLError, TimeoutError) as exc:
            ok, error = False, str(exc)[:500]
            log.warning("Couldn't %s %s: %s", action, cid, error)
        try:
            self._api(f"/api/nodes/tasks/{task_id}", {"action": action, "ok": ok, "error": error})
        except (HttpError, OSError, urllib.error.URLError, TimeoutError) as exc:
            # Not lost: the coordinator hands the task out again and pin/unpin are idempotent.
            log.warning("Couldn't report task %s: %s", task_id, exc)
        finally:
            with self.lock:
                self.in_flight.discard(task_id)

    def _dispatch(self, tasks):
        for task in tasks:
            if task.get("action") not in ("pin", "unpin") or not CID_RE.match(str(task.get("cid", ""))):
                log.warning("Ignoring malformed task: %r", task)
                continue
            with self.lock:
                if task["id"] in self.in_flight:
                    continue
                self.in_flight.add(task["id"])
            self.workers.submit(self._run_task, task)

    # -- main loop ----------------------------------------------------------------

    def _wait_for_kubo(self):
        delay = 1
        while not self.stopping.is_set():
            try:
                peer_id, addresses = self.kubo.identity()
                self.kubo_version = self.kubo.version()
                return peer_id, addresses
            except (HttpError, OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                log.info("Waiting for IPFS at %s (%s)", self.kubo.api_url, exc)
                self.stopping.wait(delay)
                delay = min(delay * 2, 30)
        raise SystemExit(0)

    def run(self):
        peer_id, addresses = self._wait_for_kubo()
        log.info("IPFS %s is up, peer ID %s", self.kubo_version, peer_id)

        interval = 15
        backoff = 1
        while not self.state.get("token") and not self.stopping.is_set():
            try:
                interval = self.register(peer_id, addresses)
            except Fatal as exc:
                # Waiting (rather than exiting) avoids a restart loop; fix .env and restart.
                log.error("%s Fix node/.env, then restart the node.", exc)
                self.stopping.wait(300)
            except (HttpError, OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                log.warning("Couldn't reach the coordinator to register (%s); retrying", exc)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 300)
        if self.state.get("peer_id") not in (None, peer_id):
            log.error(
                "IPFS identity changed (was %s, now %s). The storage was probably reset; "
                "the coordinator will mark this node's copies as lost.",
                self.state.get("peer_id"), peer_id,
            )

        backoff = 1
        while not self.stopping.is_set():
            try:
                peer_id, addresses = self.kubo.identity()
                status = self._status(peer_id, addresses)
                response = self._api("/api/nodes/heartbeat", status)
                self.beats += 1
                backoff = 1
                interval = response.get("heartbeat_seconds", interval)
                node_status = response.get("status", "active")
                if node_status != self.last_status:
                    if node_status == "active":
                        log.info("This node is active.")
                    else:
                        log.warning("This node is %s: %s It will only drop copies, not take new ones.",
                                    node_status, response.get("status_reason") or "")
                    self.last_status = node_status
                self._dispatch(response.get("tasks") or [])
            except HttpError as exc:
                if exc.status in (401, 403, 409):
                    log.error("The coordinator refused this node: %s", exc.message)
                    log.error("Ask the network admin for a new invite, then delete %s and restart.", self.state_path)
                    self.stopping.wait(300)
                    continue
                log.warning("Heartbeat failed (%s); retrying", exc)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                log.warning("Coordinator or IPFS unreachable (%s); retrying", exc)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            self.stopping.wait(self.heartbeat_override or interval)

        self.workers.shutdown(wait=False, cancel_futures=True)
        log.info("Agent stopped.")


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    if not os.environ.get("COORDINATOR_URL"):
        log.error("COORDINATOR_URL isn't set. Copy the settings from your invite into .env.")
        return 2
    agent = Agent(os.environ)
    signal.signal(signal.SIGTERM, lambda *_: agent.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: agent.stopping.set())
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
