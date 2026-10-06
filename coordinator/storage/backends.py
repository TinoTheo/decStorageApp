"""
Segment stores: where the coordinator keeps the encrypted segments it receives.

- LocalSegmentStore keeps them on the coordinator's own disk. That's the
  Locked Box setup, and it's still what local development and the main test
  suite use. It has no network, so nothing is ever copied to nodes.
- KuboSegmentStore adds them to the gateway's IPFS node. Storage nodes fetch
  them from there over the private IPFS network, and once enough nodes confirm
  their copies the gateway lets its own copy go (see network.services).

Every store holds opaque encrypted bytes. None of them can read file contents.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.module_loading import import_string

from .cid import cid_v1_raw, is_cid_v1, is_cid_v1_raw
from .kubo import KuboClient, KuboError


class SegmentStoreError(Exception):
    """The store couldn't do what was asked. Usually temporary."""


class SegmentNotFound(SegmentStoreError):
    """The segment isn't available from this store right now."""


class BaseSegmentStore:
    #: True when nodes can fetch segments from this store (it's on the IPFS network).
    supports_network = False

    def put(self, data: bytes) -> str:
        """Store encrypted bytes and return their content ID."""
        raise NotImplementedError

    def read(self, content_id: str, *, max_bytes: int) -> bytes:
        """Return the bytes, fetching from the network if needed. Raises SegmentNotFound."""
        raise NotImplementedError

    def holds(self, content_id: str) -> bool:
        """Whether this store keeps its own copy (as opposed to fetching it on demand)."""
        raise NotImplementedError

    def release(self, content_id: str) -> None:
        """Drop this store's own copy. Must not fail if it's already gone."""
        raise NotImplementedError

    def collect_garbage(self) -> None:
        """Reclaim space from released segments, if the store needs to."""


class LocalSegmentStore(BaseSegmentStore):
    """
    Stages segments on the coordinator's disk, sharded by content ID:
    <root>/<cid[-4:-2]>/<cid[-2:]>/<cid>. Writes go to a temp file first and are
    renamed into place, so a crash never leaves a half-written segment behind.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, content_id: str) -> Path:
        if not is_cid_v1_raw(content_id):
            raise SegmentNotFound(content_id)
        return self.root / content_id[-4:-2] / content_id[-2:] / content_id

    def put(self, data: bytes) -> str:
        content_id = cid_v1_raw(data)
        path = self._path(content_id)
        if path.exists():
            return content_id
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return content_id

    def read(self, content_id: str, *, max_bytes: int) -> bytes:
        try:
            with open(self._path(content_id), "rb") as handle:
                data = handle.read(max_bytes + 1)
        except FileNotFoundError as exc:
            raise SegmentNotFound(content_id) from exc
        if len(data) > max_bytes:
            raise SegmentStoreError(f"{content_id} is larger than {max_bytes} bytes")
        return data

    def holds(self, content_id: str) -> bool:
        try:
            return self._path(content_id).exists()
        except SegmentNotFound:
            return False

    def release(self, content_id: str) -> None:
        try:
            self._path(content_id).unlink(missing_ok=True)
        except SegmentNotFound:
            pass


class KuboSegmentStore(BaseSegmentStore):
    """Segments live on the gateway's IPFS node and, from there, on storage nodes."""

    supports_network = True

    def __init__(self, api_url, fetch_timeout=60):
        self.client = KuboClient(api_url)
        self.fetch_timeout = fetch_timeout

    @staticmethod
    def _check(content_id):
        if not is_cid_v1(content_id):
            raise SegmentNotFound(content_id)

    def put(self, data: bytes) -> str:
        try:
            return self.client.add(data)
        except KuboError as exc:
            raise SegmentStoreError(str(exc)) from exc

    def read(self, content_id: str, *, max_bytes: int) -> bytes:
        self._check(content_id)
        try:
            return self.client.cat(content_id, timeout=self.fetch_timeout, max_bytes=max_bytes)
        except KuboError as exc:
            raise SegmentNotFound(str(exc)) from exc

    def holds(self, content_id: str) -> bool:
        self._check(content_id)
        try:
            return self.client.is_pinned(content_id)
        except KuboError as exc:
            raise SegmentStoreError(str(exc)) from exc

    def release(self, content_id: str) -> None:
        self._check(content_id)
        try:
            self.client.pin_rm(content_id)
        except KuboError as exc:
            raise SegmentStoreError(str(exc)) from exc

    def collect_garbage(self) -> None:
        try:
            self.client.repo_gc()
        except KuboError as exc:
            raise SegmentStoreError(str(exc)) from exc


@lru_cache(maxsize=1)
def get_segment_store() -> BaseSegmentStore:
    config = settings.SEGMENT_STORE
    backend = import_string(config["BACKEND"])
    return backend(**config.get("OPTIONS", {}))
