#!/usr/bin/env python3
"""
A stand-in for Kubo (IPFS), for tests only. Never used in production.

It speaks the slice of the Kubo RPC API that dstore uses (id, version, add,
cat, pin/add, pin/rm, pin/ls, repo/gc, repo/stat) with the same request
shapes and error messages, so the coordinator and the node agent can't tell
the difference. Instead of libp2p and bitswap, instances fetch blocks from
each other over plain HTTP, and only from peers that present the same swarm
key, mimicking a private IPFS network. Every fetched block is checked against
its CID, as bitswap does.

Run one per simulated machine:

    python fakekubo.py --port 5101 --data /tmp/node1 --swarm-key KEY \
        --peers http://127.0.0.1:5100,http://127.0.0.1:5102 --storage-max 1GB

Stop the process to simulate a node going offline; its data stays on disk.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NOT_PINNED = "not pinned or pinned indirectly"


def cid_for(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "b" + base64.b32encode(bytes([0x01, 0x55, 0x12, 0x20]) + digest).decode().lower().rstrip("=")


def parse_size(text) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*", str(text), re.I)
    if not match:
        raise ValueError(f"bad size: {text}")
    number, unit = float(match.group(1)), match.group(2).upper().rstrip("B")
    return int(number * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit])


def parse_timeout(text, default):
    if not text:
        return default
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", text)
    if not match:
        return default
    value = float(match.group(1))
    return value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[match.group(2)]


class Repo:
    """Blocks on disk plus a pin set, guarded by one lock."""

    def __init__(self, root: Path, storage_max: int):
        self.root = root
        self.blocks = root / "blocks"
        self.blocks.mkdir(parents=True, exist_ok=True)
        self.storage_max = storage_max
        self.lock = threading.RLock()
        identity = root / "identity.json"
        if identity.exists():
            self.peer_id = json.loads(identity.read_text())["peer_id"]
        else:
            self.peer_id = "12D3KooW" + base64.b32encode(secrets.token_bytes(30)).decode().rstrip("=")[:44]
            identity.write_text(json.dumps({"peer_id": self.peer_id}))
        self.pins_file = root / "pins.json"
        self.pins = set(json.loads(self.pins_file.read_text())) if self.pins_file.exists() else set()

    def _save_pins(self):
        tmp = self.pins_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.pins)))
        os.replace(tmp, self.pins_file)

    def has(self, cid):
        return (self.blocks / cid).exists()

    def get(self, cid):
        try:
            return (self.blocks / cid).read_bytes()
        except FileNotFoundError:
            return None

    def size(self):
        return sum(p.stat().st_size for p in self.blocks.iterdir() if p.is_file())

    def put(self, data: bytes) -> str:
        cid = cid_for(data)
        with self.lock:
            if self.has(cid):
                return cid
            if self.size() + len(data) > self.storage_max:
                raise RuntimeError("datastore: storage limit reached (StorageMax)")
            tmp = self.blocks / f".{cid}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, self.blocks / cid)
        return cid

    def pin(self, cid):
        with self.lock:
            self.pins.add(cid)
            self._save_pins()

    def unpin(self, cid):
        with self.lock:
            if cid not in self.pins:
                return False
            self.pins.discard(cid)
            self._save_pins()
            return True

    def gc(self):
        removed = []
        with self.lock:
            for path in self.blocks.iterdir():
                if path.is_file() and not path.name.startswith(".") and path.name not in self.pins:
                    path.unlink()
                    removed.append(path.name)
        return removed


class FakeKubo:
    def __init__(self, data_dir, *, swarm_key="", peers=(), storage_max="10GB", port=0):
        self.repo = Repo(Path(data_dir), parse_size(storage_max))
        self.swarm_key_hash = hashlib.sha256(swarm_key.encode()).hexdigest()
        self.peers = [p.rstrip("/") for p in peers if p]
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = None

    @property
    def api_url(self):
        return f"http://127.0.0.1:{self.port}"

    # -- running ------------------------------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # -- fetching from peers (the "bitswap" stand-in) -------------------------

    def fetch(self, cid, timeout):
        deadline = time.monotonic() + timeout
        while True:
            for peer in self.peers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                request = urllib.request.Request(
                    f"{peer}/fake/blocks/{cid}", headers={"X-Swarm-Key": self.swarm_key_hash}
                )
                try:
                    with urllib.request.urlopen(request, timeout=min(remaining, 10)) as response:
                        data = response.read()
                except (urllib.error.URLError, OSError, TimeoutError):
                    continue  # that peer is down or doesn't have it
                if cid_for(data) == cid:  # never accept a block that doesn't match its CID
                    self.repo.put(data)
                    return data
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    # -- HTTP -----------------------------------------------------------------

    def _handler(self):
        kubo = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", content_type="application/json"):
                if isinstance(body, (dict, list)):
                    body = (json.dumps(body) + "\n").encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _error(self, message, code=500):
                self._send(code, {"Message": message, "Code": 0, "Type": "error"})

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path.startswith("/fake/blocks/"):
                    if self.headers.get("X-Swarm-Key") != kubo.swarm_key_hash:
                        return self._send(403, b"wrong swarm key", "text/plain")
                    data = kubo.repo.get(url.path.rsplit("/", 1)[-1])
                    if data is None:
                        return self._send(404, b"", "application/octet-stream")
                    return self._send(200, data, "application/octet-stream")
                if url.path == "/fake/state":
                    return self._send(
                        200,
                        {
                            "peer_id": kubo.repo.peer_id,
                            "pins": sorted(kubo.repo.pins),
                            "blocks": sorted(p.name for p in kubo.repo.blocks.iterdir() if not p.name.startswith(".")),
                        },
                    )
                if url.path.startswith("/api/v0/"):
                    return self._error("405 - Method Not Allowed", 405)
                return self._send(404, b"404 page not found", "text/plain")

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                args = query.get("arg", [])
                param = lambda name, default=None: (query.get(name) or [default])[0]  # noqa: E731
                command = url.path.removeprefix("/api/v0/")
                body = self._body()
                try:
                    handler = getattr(self, "cmd_" + command.replace("/", "_"), None)
                    if handler is None:
                        return self._error(f"unknown command \"{command}\"", 404)
                    return handler(args, param, body)
                except RuntimeError as exc:
                    return self._error(str(exc))

            # Each cmd_ mirrors one Kubo RPC command.

            def cmd_id(self, args, param, body):
                peer = kubo.repo.peer_id
                self._send(200, {
                    "ID": peer,
                    "Addresses": [f"/ip4/127.0.0.1/tcp/{kubo.port}/p2p/{peer}"],
                    "AgentVersion": "fakekubo/0.1",
                    "ProtocolVersion": "ipfs/0.1.0",
                })

            def cmd_version(self, args, param, body):
                self._send(200, {"Version": "0.0.0-fake", "Commit": "", "Repo": "16", "System": sys.platform})

            def cmd_add(self, args, param, body):
                content_type = self.headers.get("Content-Type", "")
                match = re.search(r"boundary=([^;]+)", content_type)
                if not match:
                    return self._error("file argument 'path' is required", 400)
                boundary = match.group(1).strip('"').encode()
                start = body.find(b"\r\n\r\n", body.find(b"--" + boundary)) + 4
                end = body.rfind(b"\r\n--" + boundary)
                if start < 4 or end < start:
                    return self._error("malformed multipart body", 400)
                data = body[start:end]
                cid = kubo.repo.put(data)
                if param("pin", "true") == "true":
                    kubo.repo.pin(cid)
                self._send(200, json.dumps({"Name": "segment", "Hash": cid, "Size": str(len(data))}).encode() + b"\n")

            def cmd_cat(self, args, param, body):
                if not args:
                    return self._error("argument \"ipfs-path\" is required", 400)
                cid = args[0]
                data = kubo.repo.get(cid)
                if data is None:
                    data = kubo.fetch(cid, parse_timeout(param("timeout"), 30))
                if data is None:
                    return self._error("context deadline exceeded")
                self._send(200, data, "text/plain")

            def cmd_pin_add(self, args, param, body):
                if not args:
                    return self._error("argument \"ipfs-path\" is required", 400)
                cid = args[0]
                if not kubo.repo.has(cid) and kubo.fetch(cid, parse_timeout(param("timeout"), 600)) is None:
                    return self._error("pin: context deadline exceeded")
                kubo.repo.pin(cid)
                self._send(200, {"Pins": [cid]})

            def cmd_pin_rm(self, args, param, body):
                if not args:
                    return self._error("argument \"ipfs-path\" is required", 400)
                if not kubo.repo.unpin(args[0]):
                    return self._error(NOT_PINNED)
                self._send(200, {"Pins": [args[0]]})

            def cmd_pin_ls(self, args, param, body):
                if args:
                    if args[0] not in kubo.repo.pins:
                        return self._error(f"path '{args[0]}' is not pinned")
                    return self._send(200, {"Keys": {args[0]: {"Type": "recursive"}}})
                self._send(200, {"Keys": {cid: {"Type": "recursive"} for cid in kubo.repo.pins}})

            def cmd_repo_gc(self, args, param, body):
                lines = b"".join(json.dumps({"Key": {"/": cid}}).encode() + b"\n" for cid in kubo.repo.gc())
                self._send(200, lines)

            def cmd_repo_stat(self, args, param, body):
                blocks = [p for p in kubo.repo.blocks.iterdir() if p.is_file() and not p.name.startswith(".")]
                self._send(200, {
                    "RepoSize": sum(p.stat().st_size for p in blocks),
                    "StorageMax": kubo.repo.storage_max,
                    "NumObjects": len(blocks),
                    "RepoPath": str(kubo.repo.root),
                    "Version": "fs-repo@16",
                })

        return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--swarm-key", default="")
    parser.add_argument("--peers", default="", help="Comma-separated base URLs of other fake nodes.")
    parser.add_argument("--storage-max", default="10GB")
    options = parser.parse_args()
    kubo = FakeKubo(
        options.data,
        swarm_key=options.swarm_key,
        peers=options.peers.split(","),
        storage_max=options.storage_max,
        port=options.port,
    )
    print(f"fakekubo {kubo.repo.peer_id} on {kubo.api_url}", flush=True)
    try:
        kubo.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
