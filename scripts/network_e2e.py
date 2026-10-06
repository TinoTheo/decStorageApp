#!/usr/bin/env python3
"""
Network end-to-end test for Many Homes.

Starts a complete, throwaway storage network on this machine and checks that
files really end up on several independent nodes:

    coordinator + worker
    gateway IPFS node
    4 storage nodes run by 3 operators (Ama runs two), each = IPFS + real agent
    a user, encrypting and decrypting with the same client library as the browser

By default IPFS is played by the stand-in in coordinator/storage/fakekubo.py,
which speaks the Kubo RPC API. Point it at a real Kubo binary to run the same
checks on real IPFS, configured by the same script the Docker images use
(node/kubo/001-dstore.sh):

    python scripts/network_e2e.py                      (or: python tasks.py network-test)
    python scripts/network_e2e.py --ipfs /path/to/ipfs (or: IPFS_BIN=... python tasks.py network-test)

Everything else is the real code. Needs Python 3.10+ and Node.js 20+; real
mode also needs Linux or macOS (the config script is a shell script).
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = ROOT / "coordinator"
PY = sys.executable
SEGMENT = 4 * 1024 * 1024
MARKER = "PLAINTEXT-MARKER-THAT-MUST-NEVER-REACH-A-NODE"

NODES = [  # (node name, operator)
    ("ama-1", "Ama"),
    ("ama-2", "Ama"),
    ("bongani-1", "Bongani"),
    ("chipo-1", "Chipo"),
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(port, command, **params):
    query = urllib.parse.urlencode(params)
    url = f"http://127.0.0.1:{port}/api/v0/{command}" + (f"?{query}" if query else "")
    with _DIRECT.open(urllib.request.Request(url, data=b"", method="POST"), timeout=60) as response:
        return response.read()


class Network:
    def __init__(self, ipfs_bin=None):
        self.ipfs = ipfs_bin
        self.work = Path(tempfile.mkdtemp(prefix="dstore-net-"))
        self.procs = {}
        self.config_warnings = []
        self.swarm_key = secrets.token_hex(32)
        self.coordinator_port = free_port()
        self.gateway_port = free_port()
        self.node_ports = {name: free_port() for name, _ in NODES}
        self.swarm_ports = {name: free_port() for name in ["gateway", *self.node_ports]}
        self.base_url = f"http://127.0.0.1:{self.coordinator_port}"
        self.node = shutil.which("node")
        if not self.node:
            sys.exit("Node.js 20+ is needed for the network test.")
        self.env = {
            **os.environ,
            "DJANGO_DEBUG": "1",
            "DJANGO_SECRET_KEY": "network-test-" + secrets.token_hex(8),
            "DATABASE_URL": f"sqlite:///{(self.work / 'db.sqlite3').as_posix()}",
            "SEGMENT_STORE": "ipfs",
            "KUBO_API_URL": f"http://127.0.0.1:{self.gateway_port}",
            "KUBO_FETCH_TIMEOUT_SECONDS": "15" if self.ipfs else "5",
            "IPFS_SWARM_KEY": self.swarm_key,
            "GATEWAY_P2P_HOST": "127.0.0.1",
            "GATEWAY_P2P_PORT": str(self.swarm_ports["gateway"]),
            "PUBLIC_URL": self.base_url,
            "NODE_HEARTBEAT_SECONDS": "1",
            "NODE_OFFLINE_AFTER_SECONDS": "5",
            "WORKER_INTERVAL_SECONDS": "1",
            "GATEWAY_GC_INTERVAL_SECONDS": "2",
            "PLACEMENT_HEADROOM_BYTES": "0",
            "AUTH_THROTTLE_RATE": "1000/minute",
            "AUTH_USER_THROTTLE_RATE": "1000/minute",
            "LOG_LEVEL": "INFO",
        }

    @property
    def mode(self):
        return "real IPFS (Kubo)" if self.ipfs else "IPFS stand-in"

    # -- processes ------------------------------------------------------------

    def start(self, name, command, env=None, cwd=ROOT):
        log = open(self.work / f"{name}.log", "a")
        self.procs[name] = (
            subprocess.Popen([str(c) for c in command], cwd=cwd, env=env or self.env, stdout=log,
                             stderr=subprocess.STDOUT),
            log,
        )

    def stop(self, name):
        proc, log = self.procs.pop(name)
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    def stop_all(self):
        for name in list(self.procs):
            self.stop(name)

    def port_of(self, name):
        return self.gateway_port if name == "gateway" else self.node_ports[name]

    def start_kubo(self, name):
        port = self.port_of(name)
        if not self.ipfs:
            urls = [f"http://127.0.0.1:{p}" for p in [self.gateway_port, *self.node_ports.values()] if p != port]
            self.start(f"ipfs-{name}", [
                PY, COORDINATOR / "storage" / "fakekubo.py", "--port", port, "--data", self.work / f"ipfs-{name}",
                "--swarm-key", self.swarm_key, "--peers", ",".join(urls), "--storage-max", "200MB",
            ])
        else:
            self._start_real_kubo(name, port)
        wait_for(f"IPFS on {name}", lambda: rpc(port, "id"), timeout=60)

    def _start_real_kubo(self, name, port):
        """What the Docker image does: init once, run the dstore config script, start the daemon."""
        repo = self.work / f"ipfs-{name}"
        env = {**os.environ, "IPFS_PATH": str(repo),
               "PATH": f"{Path(self.ipfs).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

        def ipfs(*args):
            subprocess.run([self.ipfs, *args], env=env, check=True, capture_output=True, text=True)

        if not (repo / "config").exists():
            ipfs("init", "--profile=server")
        script_env = {
            **env,
            "DSTORE_ROLE": "gateway" if name == "gateway" else "node",
            "DSTORE_SWARM_KEY": self.swarm_key,
            "DSTORE_STORAGE_MAX": "200MB",
        }
        if name != "gateway":
            script_env["DSTORE_BOOTSTRAP"] = self.gateway_bootstrap()
        result = subprocess.run(["sh", ROOT / "node" / "kubo" / "001-dstore.sh"], env=script_env,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"IPFS config script failed on {name}:\n{result.stderr}")
        self.config_warnings += [f"{name}: {line}" for line in result.stderr.splitlines() if "warning" in line]

        # Test-only changes so several nodes fit on one machine: separate ports,
        # and allow loopback addresses, which the server profile normally blocks.
        ipfs("config", "Addresses.API", f"/ip4/127.0.0.1/tcp/{port}")
        ipfs("config", "--json", "Addresses.Swarm", json.dumps([f"/ip4/127.0.0.1/tcp/{self.swarm_ports[name]}"]))
        ipfs("config", "--json", "Swarm.AddrFilters", "[]")
        ipfs("config", "--json", "Addresses.NoAnnounce", "[]")
        self.start(f"ipfs-{name}", [self.ipfs, "daemon", "--migrate=true", "--enable-gc"],
                   env={**env, "LIBP2P_FORCE_PNET": "1"})

    def gateway_bootstrap(self):
        peer_id = json.loads(rpc(self.gateway_port, "id"))["ID"]
        return f"/ip4/127.0.0.1/tcp/{self.swarm_ports['gateway']}/p2p/{peer_id}"

    def start_node(self, name, invite=None):
        """A storage node: its IPFS plus the real agent."""
        self.start_kubo(name)
        env = {
            **self.env,
            "COORDINATOR_URL": self.base_url,
            "KUBO_API_URL": f"http://127.0.0.1:{self.node_ports[name]}",
            "STATE_DIR": str(self.work / f"agent-{name}"),
            "LOG_LEVEL": "INFO",
        }
        if invite:
            env.update(ENROLLMENT_CODE=invite["ENROLLMENT_CODE"], NODE_NAME=invite["NODE_NAME"])
        self.start(f"agent-{name}", [PY, ROOT / "node" / "agent" / "dstore_agent.py"], env=env)

    def stop_node(self, name):
        self.stop(f"agent-{name}")
        self.stop(f"ipfs-{name}")

    # -- talking to things ------------------------------------------------------

    def manage(self, *args):
        result = subprocess.run([PY, "manage.py", *args], cwd=COORDINATOR, env=self.env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"manage.py {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout

    def status(self):
        return json.loads(self.manage("network_status", "--json"))

    def placements(self):
        script = (
            "import json; from network.models import Replica; "
            "print(json.dumps([[r.blob.content_id, r.node.name, r.node.operator.name, r.state] "
            "for r in Replica.objects.select_related('blob', 'node__operator')]))"
        )
        return json.loads(self.manage("shell", "--verbosity", "0", "-c", script))

    def user(self, command, **kwargs):
        args = [self.node, ROOT / "scripts" / "net_client.mjs", command]
        for key, value in kwargs.items():
            args += [f"--{key}", str(value)]
        result = subprocess.run([str(a) for a in args], cwd=ROOT, env={**self.env, "BASE_URL": self.base_url},
                                capture_output=True, text=True)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        if result.returncode:
            raise RuntimeError(f"client {command} failed: {out}")
        return out

    def kubo_state(self, name):
        """Peer ID, pinned CIDs and every block held locally, from either kind of IPFS."""
        port = self.port_of(name)
        if not self.ipfs:
            with _DIRECT.open(f"http://127.0.0.1:{port}/fake/state", timeout=5) as response:
                return json.loads(response.read())
        pins = json.loads(rpc(port, "pin/ls", type="recursive")).get("Keys") or {}
        refs = [json.loads(line)["Ref"] for line in rpc(port, "refs/local").decode().splitlines() if line.strip()]
        return {"peer_id": json.loads(rpc(port, "id"))["ID"], "pins": sorted(pins), "blocks": sorted(refs)}

    def collect_garbage(self, name):
        rpc(self.port_of(name), "repo/gc", quiet="true")

    def repo_files(self, name):
        return [p for p in (self.work / f"ipfs-{name}").rglob("*") if p.is_file()]


def wait_for(what, check, timeout=60, interval=0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - keep polling, report the last problem
            last = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {what} (last: {last!r})")


step = 0


def check(title, fn):
    global step
    step += 1
    started = time.monotonic()
    fn()
    print(f"  ok {step:2}  {title}  ({time.monotonic() - started:.1f}s)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Many Homes network test")
    parser.add_argument("--ipfs", default=os.environ.get("IPFS_BIN"), help="Path to a real Kubo `ipfs` binary.")
    options = parser.parse_args()
    if options.ipfs and not Path(options.ipfs).is_file():
        sys.exit(f"No ipfs binary at {options.ipfs}")
    net = Network(options.ipfs)
    print(f"Many Homes network test on {net.mode} (working in {net.work})")
    user = {"user": f"net-{secrets.token_hex(3)}", "pass": "copper lantern quietly folds the map"}
    shared = {}

    try:
        net.start_kubo("gateway")
        net.collect_garbage("gateway")
        baseline = set(net.kubo_state("gateway")["blocks"])  # what a fresh, empty node holds

        def ours(name):
            """Our segments pinned on a node (real IPFS also keeps a built-in empty-folder pin)."""
            return set(net.kubo_state(name)["pins"]) & shared["cids"]

        def gateway_empty():
            return not ours("gateway") and set(net.kubo_state("gateway")["blocks"]) <= baseline

        net.manage("migrate", "--noinput")
        net.start("coordinator", [PY, "manage.py", "runserver", f"127.0.0.1:{net.coordinator_port}", "--noreload"],
                  cwd=COORDINATOR)
        net.start("worker", [PY, "manage.py", "run_worker", "--interval", "1"], cwd=COORDINATOR)
        wait_for("the coordinator", lambda: _DIRECT.open(f"{net.base_url}/health", timeout=2))

        def enroll():
            for name, operator in NODES:
                invite = json.loads(net.manage("invite_node", "--operator", operator, "--node-name", name, "--json"))
                assert invite["GATEWAY_BOOTSTRAP"].endswith(net.kubo_state("gateway")["peer_id"])
                net.start_node(name, invite)
            wait_for("4 nodes online", lambda: sum(n["online"] for n in net.status()["nodes"]) == 4)
            names = sorted((n["name"], n["operator"]) for n in net.status()["nodes"])
            assert names == sorted(NODES), names
            assert not net.config_warnings, net.config_warnings
        check("4 nodes from 3 operators enroll with single-use invites", enroll)

        def upload():
            net.user("register", **user)
            shared.update(net.user("upload", **user, size=2 * SEGMENT + 700_000, marker=MARKER))
            assert shared["segments"] == 3
        check("a user encrypts and uploads a 9 MB file (3 segments)", upload)

        def spread():
            def done():
                files = net.user("list", **user)["files"]
                return files and files[0]["copies"] == 3
            wait_for("3 confirmed copies of every segment", done)
            by_blob = {}
            for content_id, node, operator, state in net.placements():
                assert state == "stored", (content_id, node, state)
                by_blob.setdefault(content_id, []).append((node, operator))
            assert len(by_blob) == 3
            shared["cids"] = set(by_blob)
            for content_id, holders in by_blob.items():
                operators = [o for _, o in holders]
                assert len(holders) == 3 and len(set(operators)) == 3, (content_id, holders)
            shared["by_blob"] = by_blob
        check("every segment gets 3 copies, each with a different operator", spread)

        def gateway_lets_go():
            wait_for("the gateway to drop its copies", lambda: net.status()["blobs"]["on_gateway"] == 0)
            wait_for("gateway garbage collection", gateway_empty, timeout=30)
        check("the gateway releases its staging copies and holds nothing", gateway_lets_go)

        def nodes_hold_ciphertext_only():
            marker = MARKER.encode()
            pins = sum(len(ours(name)) for name, _ in NODES)
            assert pins == 9, pins
            for name, _ in NODES:
                for path in net.repo_files(name):
                    data = path.read_bytes()
                    assert marker not in data and b"field-report" not in data, f"{name}: {path.name} holds plaintext"
        check("nodes hold 9 copies in total, none containing plaintext", nodes_hold_ciphertext_only)

        def download_from_nodes():
            result = net.user("download", **user, file=shared["fileId"])
            assert (result["sha256"], result["name"]) == (shared["sha256"], "field-report.pdf"), result
        check("download pulls every segment back from the nodes and decrypts it", download_from_nodes)

        def survive_node_loss():
            victim = max(NODES, key=lambda n: sum(n[0] in [h for h, _ in v] for v in shared["by_blob"].values()))[0]
            shared["victim"] = victim
            net.stop_node(victim)
            time.sleep(3)  # let the gateway's garbage collection clear what the last download cached
            wait_for("gateway cache cleared", gateway_empty, timeout=30)
            result = net.user("download", **user, file=shared["fileId"])
            assert result["sha256"] == shared["sha256"]
        check("with a node switched off, the file still downloads intact", survive_node_loss)

        def delete_everywhere():
            net.user("delete", **user, file=shared["fileId"])
            alive = [n for n in net.node_ports if n != shared["victim"]]
            wait_for("online nodes to unpin", lambda: all(not ours(n) for n in alive))
            left = {state for *_, state in net.placements()}
            assert left <= {"releasing"}, left  # only the switched-off node still owes unpins
        check("deleting the file makes every online node drop its copies", delete_everywhere)

        def offline_node_catches_up():
            net.start_node(shared["victim"])
            wait_for("the returning node to unpin", lambda: not ours(shared["victim"]))
            wait_for("the network to forget the file", lambda: net.status()["blobs"]["being_deleted"] == 0)
            assert net.placements() == []
            assert net.user("list", **user)["files"] == []
        check("the switched-off node comes back and drops its copies too", offline_node_catches_up)

        print(f"\nAll {step} network checks passed on {net.mode}.")
    except Exception:
        print("\nNetwork test FAILED. Last lines of each log:", file=sys.stderr)
        for log in sorted(net.work.glob("*.log")):
            lines = log.read_text(errors="replace").splitlines()[-12:]
            print(f"--- {log.name}", *lines, sep="\n", file=sys.stderr)
        raise
    finally:
        net.stop_all()
        shutil.rmtree(net.work, ignore_errors=True)


if __name__ == "__main__":
    main()
