import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .backends import KuboSegmentStore, LocalSegmentStore, SegmentNotFound, SegmentStoreError
from .cid import cid_v1_raw, is_cid_v1, is_cid_v1_raw
from .kubo import KuboClient, KuboError


class CidTests(SimpleTestCase):
    def test_matches_ipfs_for_a_raw_block(self):
        # `echo -n "hello world" | ipfs add --cid-version=1 --raw-leaves` gives this CID.
        self.assertEqual(cid_v1_raw(b"hello world"), "bafkreifzjut3te2nhyekklss27nh3k72ysco7y32koao5eei66wof36n5e")

    def test_validation(self):
        self.assertTrue(is_cid_v1_raw(cid_v1_raw(b"x")))
        for bad in ["", "Qm123", "bafy" + "a" * 55, "../../etc/passwd", None]:
            self.assertFalse(is_cid_v1_raw(bad), bad)


class LocalSegmentStoreTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = LocalSegmentStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_read_release(self):
        content_id = self.store.put(b"ciphertext")
        self.assertEqual(content_id, cid_v1_raw(b"ciphertext"))
        self.assertTrue(self.store.holds(content_id))
        self.assertEqual(self.store.read(content_id, max_bytes=100), b"ciphertext")
        self.store.release(content_id)
        self.assertFalse(self.store.holds(content_id))
        self.store.release(content_id)  # releasing twice is fine

    def test_put_is_idempotent_and_leaves_no_temp_files(self):
        first = self.store.put(b"same bytes")
        second = self.store.put(b"same bytes")
        self.assertEqual(first, second)
        leftovers = [p for p in Path(self.tmp.name).rglob("*") if p.name.startswith(".incoming-")]
        self.assertEqual(leftovers, [])

    def test_rejects_paths_that_are_not_cids(self):
        with self.assertRaises(SegmentNotFound):
            self.store.read("../../etc/passwd", max_bytes=100)
        self.assertFalse(self.store.holds("../../etc/passwd"))

    def test_read_refuses_oversized_data(self):
        content_id = self.store.put(b"x" * 50)
        with self.assertRaises(SegmentStoreError):
            self.store.read(content_id, max_bytes=10)

    def test_missing_segment(self):
        with self.assertRaises(SegmentNotFound):
            self.store.read(cid_v1_raw(b"never stored"), max_bytes=100)


class KuboSegmentStoreTests(SimpleTestCase):
    """Against the stand-in Kubo, which speaks the same RPC API."""

    def setUp(self):
        from .fakekubo import FakeKubo

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.node = FakeKubo(f"{self.tmp.name}/node", swarm_key="k").start()
        self.gateway = FakeKubo(f"{self.tmp.name}/gw", swarm_key="k", peers=[self.node.api_url]).start()
        self.store = KuboSegmentStore(self.gateway.api_url, fetch_timeout=1)

    def tearDown(self):
        self.gateway.stop()
        self.node.stop()
        self.tmp.cleanup()

    def test_put_read_release_gc(self):
        content_id = self.store.put(b"ciphertext")
        self.assertTrue(is_cid_v1(content_id))
        self.assertTrue(self.store.holds(content_id))
        self.assertEqual(self.store.read(content_id, max_bytes=100), b"ciphertext")
        self.store.release(content_id)
        self.store.release(content_id)  # already released: still fine
        self.assertFalse(self.store.holds(content_id))
        self.store.collect_garbage()
        with self.assertRaises(SegmentNotFound):
            self.store.read(content_id, max_bytes=100)

    def test_reads_fall_back_to_the_network(self):
        content_id = self.node.repo.put(b"held by a node")
        self.assertEqual(self.store.read(content_id, max_bytes=100), b"held by a node")

    def test_other_swarms_cant_serve_blocks(self):
        from .fakekubo import FakeKubo

        outsider = FakeKubo(f"{self.tmp.name}/outsider", swarm_key="other").start()
        try:
            content_id = outsider.repo.put(b"from outside")
            self.gateway.peers = [outsider.api_url]
            with self.assertRaises(SegmentNotFound):
                self.store.read(content_id, max_bytes=100)
        finally:
            outsider.stop()

    def test_refuses_oversized_reads_and_bad_ids(self):
        content_id = self.store.put(b"x" * 500)
        with self.assertRaises(SegmentNotFound):
            self.store.read(content_id, max_bytes=10)
        with self.assertRaises(SegmentNotFound):
            self.store.read("../../etc/passwd", max_bytes=10)

    def test_unreachable_gateway(self):
        self.gateway.stop()
        with self.assertRaises(SegmentStoreError):
            self.store.put(b"data")
        self.gateway = type(self.gateway)(f"{self.tmp.name}/gw2").start()  # for tearDown

    def test_client_reads_kubo_errors(self):
        client = KuboClient(self.gateway.api_url)
        with self.assertRaises(KuboError) as ctx:
            client.cat("bafkreinotacid", timeout=1, max_bytes=10)
        self.assertFalse(ctx.exception.unreachable)
        self.assertEqual(client.id()["ID"], self.gateway.repo.peer_id)
        self.assertGreaterEqual(client.repo_stat()["StorageMax"], 1)
