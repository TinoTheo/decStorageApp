"""
A small client for the Kubo (IPFS) RPC API, using only the standard library.

Only the handful of calls the coordinator needs. Every Kubo RPC call is an HTTP
POST to /api/v0/<command>; errors come back as HTTP 500 with a JSON body like
{"Message": "...", "Code": 0, "Type": "error"}.

The RPC API is an admin interface. It must never be reachable from the
internet: in docker compose it listens only on the internal network.
"""

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

# Fixed so the same bytes always produce the same CID, on every node and every
# Kubo version we run: CIDv1, raw leaves, 1 MiB chunks, SHA-256.
ADD_PARAMS = {
    "cid-version": "1",
    "raw-leaves": "true",
    "chunker": "size-1048576",
    "hash": "sha2-256",
    "pin": "true",
    "quieter": "true",
}


class KuboError(Exception):
    """Kubo answered with an error, or couldn't be reached."""

    def __init__(self, message, *, unreachable=False):
        super().__init__(message)
        self.unreachable = unreachable


def _error_message(body: bytes) -> str:
    try:
        return json.loads(body.decode("utf-8")).get("Message") or body.decode("utf-8", "replace")
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace")[:300]


# The RPC API is local (same host or same Docker network): never go through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class KuboClient:
    def __init__(self, api_url, timeout=30):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _call(self, command, args=(), params=None, body=None, content_type=None, timeout=None, max_bytes=None):
        query = [("arg", a) for a in args] + list((params or {}).items())
        url = f"{self.api_url}/api/v0/{command}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=body if body is not None else b"", method="POST")
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                if max_bytes is None:
                    return response.read()
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise KuboError(f"{command} returned more than {max_bytes} bytes")
                if response.headers.get("X-Stream-Error"):
                    raise KuboError(f"{command} failed mid-stream: {response.headers['X-Stream-Error']}")
                return data
        except urllib.error.HTTPError as exc:
            raise KuboError(f"{command}: {_error_message(exc.read())}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise KuboError(f"{command}: Kubo unreachable ({exc})", unreachable=True) from exc

    @staticmethod
    def _json_lines(raw: bytes):
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    # -- calls ---------------------------------------------------------------

    def id(self):
        return json.loads(self._call("id"))

    def version(self):
        return json.loads(self._call("version")).get("Version", "")

    def add(self, data: bytes) -> str:
        boundary = "dstore" + secrets.token_hex(12)
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="segment"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        lines = self._json_lines(
            self._call("add", params=ADD_PARAMS, body=body, content_type=f"multipart/form-data; boundary={boundary}")
        )
        if not lines or "Hash" not in lines[-1]:
            raise KuboError("add returned no CID")
        return lines[-1]["Hash"]

    def cat(self, cid: str, *, timeout: int, max_bytes: int) -> bytes:
        # Kubo's own timeout stops it searching the network; ours is a backstop.
        return self._call("cat", [cid], params={"timeout": f"{timeout}s"}, timeout=timeout + 5, max_bytes=max_bytes)

    def pin_rm(self, cid: str) -> None:
        try:
            self._call("pin/rm", [cid])
        except KuboError as exc:
            if not exc.unreachable and "not pinned" in str(exc):
                return  # already gone: that's the outcome we wanted
            raise

    def is_pinned(self, cid: str) -> bool:
        try:
            keys = json.loads(self._call("pin/ls", [cid], params={"type": "recursive"})).get("Keys") or {}
        except KuboError as exc:
            if not exc.unreachable and "not pinned" in str(exc):
                return False
            raise
        return cid in keys

    def repo_gc(self) -> int:
        return len(self._json_lines(self._call("repo/gc", params={"quiet": "true"}, timeout=600)))

    def repo_stat(self):
        return json.loads(self._call("repo/stat", params={"size-only": "true"}))
