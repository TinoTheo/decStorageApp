import base64
import hashlib
import os
import tempfile
import uuid

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.tests import registration
from storage.backends import get_segment_store

from .models import File, Segment

SEGMENT = 1024  # small segments keep the tests fast
TAG = 16


def b64(n):
    return base64.b64encode(os.urandom(n)).decode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FilesTestCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.overrides = override_settings(
            SEGMENT_SIZE=SEGMENT,
            MAX_FILE_SIZE=100 * SEGMENT,
            SEGMENT_STORE={"BACKEND": "storage.backends.LocalSegmentStore", "OPTIONS": {"root": self.tmp.name}},
        )
        self.overrides.enable()
        get_segment_store.cache_clear()
        self.token = self.sign_up("thandi")
        self.auth(self.token)

    def tearDown(self):
        self.overrides.disable()
        get_segment_store.cache_clear()
        self.tmp.cleanup()

    def sign_up(self, username):
        response = self.client.post("/api/auth/register", registration(username), format="json")
        assert response.status_code == 201, response.data
        return response.data["token"]

    def auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

    def create_file(self, size, **overrides):
        payload = {
            "id": str(uuid.uuid4()),
            "encrypted_name": b64(40),
            "wrapped_file_key": b64(61),
            "size": size,
            "segment_size": SEGMENT,
        }
        payload.update(overrides)
        return self.client.post("/api/files/", payload, format="json")

    def put_segment(self, file_id, index, data, digest=None):
        return self.client.put(
            f"/api/files/{file_id}/segments/{index}",
            data=data,
            content_type="application/octet-stream",
            HTTP_X_CONTENT_SHA256=digest if digest is not None else sha(data),
        )

    def upload_all(self, file_id, size):
        blobs = []
        count = max(1, -(-size // SEGMENT))
        for index in range(count):
            plaintext_len = min(SEGMENT, size - index * SEGMENT)
            data = os.urandom(plaintext_len + TAG)
            self.assertEqual(self.put_segment(file_id, index, data).status_code, 201)
            blobs.append(data)
        return blobs


class CreateFileTests(FilesTestCase):
    def test_creates_manifest_with_segment_count(self):
        response = self.create_file(2 * SEGMENT + 5)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["segment_count"], 3)
        self.assertEqual(response.data["status"], "uploading")
        self.assertEqual(response.data["missing_segments"], [0, 1, 2])

    def test_empty_file_has_one_segment(self):
        self.assertEqual(self.create_file(0).data["segment_count"], 1)

    def test_rejects_wrong_segment_size_and_oversized_files(self):
        self.assertEqual(self.create_file(10, segment_size=SEGMENT * 2).status_code, 400)
        self.assertEqual(self.create_file(101 * SEGMENT).status_code, 400)

    def test_duplicate_ids_conflict_even_across_users(self):
        file_id = self.create_file(10).data["id"]
        self.auth(self.sign_up("sipho"))
        self.assertEqual(self.create_file(10, id=file_id).status_code, 409)

    def test_requires_sign_in(self):
        self.client.credentials()
        self.assertEqual(self.create_file(10).status_code, 401)
        self.assertEqual(self.client.get("/api/files/").status_code, 401)


class SegmentUploadTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.file_id = self.create_file(SEGMENT + 100).data["id"]

    def test_stores_segment_under_its_cid(self):
        data = os.urandom(SEGMENT + TAG)
        response = self.put_segment(self.file_id, 0, data)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["content_id"].startswith("bafkrei"))
        self.assertEqual(get_segment_store().read(response.data["content_id"], max_bytes=len(data)), data)

    def test_rejects_hash_mismatch_and_missing_hash(self):
        data = os.urandom(SEGMENT + TAG)
        self.assertEqual(self.put_segment(self.file_id, 0, data, digest=sha(b"other")).status_code, 400)
        self.assertEqual(self.put_segment(self.file_id, 0, data, digest="").status_code, 400)
        self.assertFalse(Segment.objects.exists())

    def test_enforces_segment_sizes(self):
        # Every segment but the last must be full.
        self.assertEqual(self.put_segment(self.file_id, 0, os.urandom(SEGMENT)).status_code, 400)
        # The last segment carries 1..SEGMENT bytes of plaintext.
        self.assertEqual(self.put_segment(self.file_id, 1, os.urandom(TAG)).status_code, 400)
        self.assertEqual(self.put_segment(self.file_id, 1, os.urandom(SEGMENT + TAG + 1)).status_code, 400)
        self.assertEqual(self.put_segment(self.file_id, 1, os.urandom(100 + TAG)).status_code, 201)

    def test_index_out_of_range(self):
        self.assertEqual(self.put_segment(self.file_id, 2, os.urandom(100 + TAG)).status_code, 404)

    def test_retries_are_idempotent_but_changes_conflict(self):
        data = os.urandom(SEGMENT + TAG)
        self.assertEqual(self.put_segment(self.file_id, 0, data).status_code, 201)
        self.assertEqual(self.put_segment(self.file_id, 0, data).status_code, 200)
        self.assertEqual(self.put_segment(self.file_id, 0, os.urandom(SEGMENT + TAG)).status_code, 409)
        self.assertEqual(Segment.objects.count(), 1)

    def test_segments_can_arrive_in_any_order(self):
        self.assertEqual(self.put_segment(self.file_id, 1, os.urandom(100 + TAG)).status_code, 201)
        manifest = self.client.get(f"/api/files/{self.file_id}").data
        self.assertEqual(manifest["missing_segments"], [0])


class CompleteAndDownloadTests(FilesTestCase):
    def test_complete_requires_every_segment(self):
        file_id = self.create_file(2 * SEGMENT).data["id"]
        self.put_segment(file_id, 0, os.urandom(SEGMENT + TAG))
        response = self.client.post(f"/api/files/{file_id}/complete")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["missing_segments"], [1])

    def test_full_round_trip(self):
        size = 2 * SEGMENT + 300
        file_id = self.create_file(size).data["id"]
        blobs = self.upload_all(file_id, size)

        done = self.client.post(f"/api/files/{file_id}/complete")
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.data["status"], "complete")
        self.assertEqual(done.data["size"], size)
        self.assertEqual([s["sha256"] for s in done.data["segments"]], [sha(b) for b in blobs])
        self.assertEqual(self.client.post(f"/api/files/{file_id}/complete").status_code, 200)  # idempotent

        for index, expected in enumerate(blobs):
            response = self.client.get(f"/api/files/{file_id}/segments/{index}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, expected)
            self.assertEqual(response["X-Content-SHA256"], sha(expected))

    def test_no_uploads_after_complete(self):
        file_id = self.create_file(10).data["id"]
        self.upload_all(file_id, 10)
        self.client.post(f"/api/files/{file_id}/complete")
        self.assertEqual(self.put_segment(file_id, 0, os.urandom(10 + TAG)).status_code, 409)

    def test_missing_blob_is_reported_not_crashed(self):
        file_id = self.create_file(10).data["id"]
        self.upload_all(file_id, 10)
        get_segment_store().release(Segment.objects.get().content_id)
        self.assertEqual(self.client.get(f"/api/files/{file_id}/segments/0").status_code, 503)


class ListAndDeleteTests(FilesTestCase):
    def test_list_shows_only_my_files_with_sizes(self):
        mine = self.create_file(SEGMENT + 7).data["id"]
        self.upload_all(mine, SEGMENT + 7)
        self.auth(self.sign_up("sipho"))
        self.create_file(10)
        self.auth(self.token)

        files = self.client.get("/api/files/").data
        self.assertEqual([f["id"] for f in files], [mine])
        self.assertEqual(files[0]["size"], SEGMENT + 7)
        self.assertIn("wrapped_file_key", files[0])

    def test_delete_removes_records_and_bytes(self):
        file_id = self.create_file(SEGMENT + 7).data["id"]
        self.upload_all(file_id, SEGMENT + 7)
        content_ids = list(Segment.objects.values_list("content_id", flat=True))

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"/api/files/{file_id}").status_code, 204)

        self.assertFalse(File.objects.exists())
        self.assertFalse(Segment.objects.exists())
        for content_id in content_ids:
            self.assertFalse(get_segment_store().holds(content_id))


class IsolationTests(FilesTestCase):
    def test_other_users_get_404_for_everything(self):
        file_id = self.create_file(10).data["id"]
        self.upload_all(file_id, 10)
        self.auth(self.sign_up("intruder"))

        self.assertEqual(self.client.get(f"/api/files/{file_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/files/{file_id}/segments/0").status_code, 404)
        self.assertEqual(self.put_segment(file_id, 0, os.urandom(10 + TAG)).status_code, 404)
        self.assertEqual(self.client.post(f"/api/files/{file_id}/complete").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/files/{file_id}").status_code, 404)
        self.assertTrue(File.objects.filter(id=file_id).exists())
