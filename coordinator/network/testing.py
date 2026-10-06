"""Helpers shared by the network and storage tests."""

import base64
import os
import tempfile
import uuid

from django.core.cache import cache
from django.test import override_settings

from storage.backends import get_segment_store
from storage.fakekubo import FakeKubo

from .models import Node, Operator

SWARM_KEY = "ab" * 32


class FakeNetworkMixin:
    """
    Gives each test a fresh stand-in IPFS gateway and points the coordinator at it.
    `self.gateway` is the FakeKubo; `self.spawn_kubo()` adds more (for nodes).
    """

    segment_size = 1024

    def setUp(self):
        super().setUp()
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._kubos = []
        self.gateway = self.spawn_kubo("gateway")
        self._settings = override_settings(
            SEGMENT_SIZE=self.segment_size,
            MAX_FILE_SIZE=100 * self.segment_size,
            SEGMENT_STORE={
                "BACKEND": "storage.backends.KuboSegmentStore",
                "OPTIONS": {"api_url": self.gateway.api_url, "fetch_timeout": 2},
            },
            KUBO_API_URL=self.gateway.api_url,
            IPFS_SWARM_KEY=SWARM_KEY,
            PLACEMENT_HEADROOM_BYTES=0,
            REPLICA_COUNT=3,
            REQUIRE_DISTINCT_OPERATORS=True,
        )
        self._settings.enable()
        get_segment_store.cache_clear()

    def tearDown(self):
        self._settings.disable()
        get_segment_store.cache_clear()
        for kubo in self._kubos:
            kubo.stop()
        self._tmp.cleanup()
        super().tearDown()

    def spawn_kubo(self, name, peers=(), storage_max="1GB"):
        kubo = FakeKubo(os.path.join(self._tmp.name, name), swarm_key=SWARM_KEY, peers=peers, storage_max=storage_max)
        self._kubos.append(kubo.start())
        return kubo


def make_node(operator_name, name=None, *, capacity=10**9, used=0, online=True, peer_id=None):
    from django.utils import timezone

    operator, _ = Operator.objects.get_or_create(name=operator_name)
    _, token_hash = Node.new_token()
    return Node.objects.create(
        operator=operator,
        name=name or f"{operator_name}-node",
        peer_id=peer_id or f"12D3KooW{uuid.uuid4().hex}",
        token_hash=token_hash,
        capacity_bytes=capacity,
        used_bytes=used,
        last_seen_at=timezone.now() if online else None,
    )


def node_with_token(operator_name, name=None, **kwargs):
    node = make_node(operator_name, name, **kwargs)
    raw, token_hash = Node.new_token()
    Node.objects.filter(pk=node.pk).update(token_hash=token_hash)
    node.refresh_from_db()
    return node, raw


def b64(n):
    return base64.b64encode(os.urandom(n)).decode()
