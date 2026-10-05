import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .backends import LocalSegmentStore, SegmentNotFound
from .cid import cid_v1_raw, is_cid_v1_raw


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
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalSegmentStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_open_delete(self):
        content_id = self.store.put(b"ciphertext")
        self.assertEqual(content_id, cid_v1_raw(b"ciphertext"))
        self.assertTrue(self.store.exists(content_id))
        with self.store.open(content_id) as handle:
            self.assertEqual(handle.read(), b"ciphertext")
        self.store.delete(content_id)
        self.assertFalse(self.store.exists(content_id))
        self.store.delete(content_id)  # deleting twice is fine

    def test_put_is_idempotent_and_leaves_no_temp_files(self):
        first = self.store.put(b"same bytes")
        second = self.store.put(b"same bytes")
        self.assertEqual(first, second)
        leftovers = [p for p in Path(self.tmp.name).rglob("*") if p.name.startswith(".incoming-")]
        self.assertEqual(leftovers, [])

    def test_rejects_paths_that_are_not_cids(self):
        with self.assertRaises(SegmentNotFound):
            self.store.open("../../etc/passwd")
        self.assertFalse(self.store.exists("../../etc/passwd"))

    def test_missing_segment(self):
        with self.assertRaises(SegmentNotFound):
            self.store.open(cid_v1_raw(b"never stored"))
