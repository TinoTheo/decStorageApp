"""
Segment stores.

The rest of the coordinator only talks to `get_segment_store()`, so moving from
staging on the coordinator's disk (M1) to pinning on storage nodes (M2) is a
settings change plus a new backend class, not a rewrite.

Every backend stores opaque encrypted bytes. None of them can read file contents.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.module_loading import import_string

from .cid import cid_v1_raw, is_cid_v1_raw


class SegmentNotFound(Exception):
    pass


class BaseSegmentStore:
    """Interface every segment store implements."""

    def put(self, data: bytes) -> str:
        """Store encrypted bytes and return their content ID."""
        raise NotImplementedError

    def open(self, content_id: str):
        """Return a readable binary file object. Raises SegmentNotFound."""
        raise NotImplementedError

    def exists(self, content_id: str) -> bool:
        raise NotImplementedError

    def delete(self, content_id: str) -> None:
        """Remove the bytes. Must not fail if they are already gone."""
        raise NotImplementedError


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

    def open(self, content_id: str):
        try:
            return open(self._path(content_id), "rb")
        except FileNotFoundError as exc:
            raise SegmentNotFound(content_id) from exc

    def exists(self, content_id: str) -> bool:
        try:
            return self._path(content_id).exists()
        except SegmentNotFound:
            return False

    def delete(self, content_id: str) -> None:
        try:
            self._path(content_id).unlink(missing_ok=True)
        except SegmentNotFound:
            pass


@lru_cache(maxsize=1)
def get_segment_store() -> BaseSegmentStore:
    config = settings.SEGMENT_STORE
    backend = import_string(config["BACKEND"])
    return backend(**config.get("OPTIONS", {}))
