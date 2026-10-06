import hashlib
import io
import json
import os
import tempfile
import uuid
from datetime import timedelta
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import registration
from files.models import File, Segment
from storage.backends import LocalSegmentStore, get_segment_store

from . import services
from .models import Blob, Node, NodeInvite, Operator, Replica
from .testing import SWARM_KEY, FakeNetworkMixin, b64, make_node, node_with_token

TAG = 16


def make_blob(size=1000, **kwargs):
    data = os.urandom(8)
    return Blob.objects.create(
        content_id=f"cid-{uuid.uuid4().hex}", size=size, sha256=hashlib.sha256(data).hexdigest(), **kwargs
    )


def live_replicas(blob):
    return list(Replica.objects.filter(blob=blob, state__in=Replica.LIVE_STATES).select_related("node__operator"))


@override_settings(PLACEMENT_HEADROOM_BYTES=0, REPLICA_COUNT=3, REQUIRE_DISTINCT_OPERATORS=True)
class PlacementTests(TestCase):
    def test_three_copies_with_three_different_operators(self):
        for name in ("Ama", "Bongani", "Chipo", "Dumi"):
            make_node(name)
        blob = make_blob()
        result = services.place_blobs()
        replicas = live_replicas(blob)
        self.assertEqual(result.assigned, 3)
        self.assertEqual(len(replicas), 3)
        self.assertEqual(len({r.node.operator_id for r in replicas}), 3)
        self.assertTrue(all(r.state == Replica.State.ASSIGNED for r in replicas))

    def test_never_two_copies_with_one_operator(self):
        make_node("Ama", "ama-1")
        make_node("Ama", "ama-2")
        make_node("Bongani")
        blob = make_blob()
        result = services.place_blobs()
        self.assertEqual(len(live_replicas(blob)), 2)
        self.assertEqual(result.short, [blob.content_id])

    @override_settings(REQUIRE_DISTINCT_OPERATORS=False)
    def test_same_operator_allowed_when_configured(self):
        make_node("Ama", "ama-1")
        make_node("Ama", "ama-2")
        make_node("Bongani")
        blob = make_blob()
        services.place_blobs()
        self.assertEqual(len(live_replicas(blob)), 3)

    def test_skips_offline_disabled_and_inactive(self):
        make_node("Offline", online=False)
        disabled = make_node("Disabled")
        Node.objects.filter(pk=disabled.pk).update(status=Node.Status.DISABLED)
        inactive = make_node("Inactive")
        Operator.objects.filter(pk=inactive.operator_id).update(is_active=False)
        stale = make_node("Stale")
        Node.objects.filter(pk=stale.pk).update(last_seen_at=timezone.now() - timedelta(hours=1))
        good = [make_node(n) for n in ("Ama", "Bongani", "Chipo")]

        blob = make_blob()
        services.place_blobs()
        self.assertEqual({r.node_id for r in live_replicas(blob)}, {n.id for n in good})

    def test_needs_room_and_counts_copies_already_on_their_way(self):
        tight = make_node("Tight", capacity=1500)
        for name in ("Ama", "Bongani"):
            make_node(name)
        first, second = make_blob(1000), make_blob(1000)
        result = services.place_blobs()
        holders = {b.pk: {r.node_id for r in live_replicas(b)} for b in (first, second)}
        # The tight node has room for one blob, not two, so the second waits for a third copy.
        self.assertEqual(sum(tight.id in nodes for nodes in holders.values()), 1)
        self.assertEqual(result.short, [second.content_id])

    def test_prefers_the_nodes_with_the_most_room(self):
        make_node("Small", capacity=2000)
        big = [make_node(n, capacity=10**9) for n in ("Ama", "Bongani", "Chipo")]
        blob = make_blob(1000)
        services.place_blobs()
        self.assertEqual({r.node_id for r in live_replicas(blob)}, {n.id for n in big})

    def test_repeat_passes_add_nothing(self):
        for name in ("Ama", "Bongani", "Chipo", "Dumi"):
            make_node(name)
        make_blob()
        services.place_blobs()
        self.assertEqual(services.place_blobs().assigned, 0)
        self.assertEqual(Replica.objects.count(), 3)

    def test_deleted_blobs_are_not_placed(self):
        for name in ("Ama", "Bongani", "Chipo"):
            make_node(name)
        make_blob(deleted_at=timezone.now())
        self.assertEqual(services.place_blobs().assigned, 0)

    def test_tops_up_after_a_copy_is_lost(self):
        nodes = [make_node(n) for n in ("Ama", "Bongani", "Chipo", "Dumi")]
        blob = make_blob()
        services.place_blobs()
        holder = live_replicas(blob)[0]
        Replica.objects.filter(pk=holder.pk).update(state=Replica.State.LOST)
        services.place_blobs()
        replicas = live_replicas(blob)
        self.assertEqual(len(replicas), 3)
        self.assertNotIn(holder.node_id, {r.node_id for r in replicas})
        self.assertEqual(len(nodes), 4)


class InviteTests(TestCase):
    def test_codes_are_single_use_hashed_and_expire(self):
        operator = Operator.objects.create(name="Ama")
        invite, code = NodeInvite.issue(operator)
        self.assertNotEqual(invite.code_hash, code)
        self.assertEqual(NodeInvite.redeemable(code), invite)
        NodeInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(NodeInvite.redeemable(code))


class NodeApiTests(FakeNetworkMixin, APITestCase):
    def register(self, code, peer_id=None, **extra):
        payload = {
            "code": code,
            "name": "harare-1",
            "peer_id": peer_id or f"12D3KooW{uuid.uuid4().hex}",
            "capacity_bytes": 10**9,
            "used_bytes": 0,
            "agent_version": "0.2.0",
            "kubo_version": "0.43.1",
            "addresses": ["/ip4/10.0.0.5/tcp/4001"],
        }
        payload.update(extra)
        return self.client.post("/api/nodes/register", payload, format="json")

    def heartbeat(self, node, token, **extra):
        payload = {"peer_id": node.peer_id, "capacity_bytes": 10**9, "used_bytes": 0, "pinned_count": 0}
        payload.update(extra)
        return self.client.post("/api/nodes/heartbeat", payload, format="json", HTTP_AUTHORIZATION=f"Node {token}")

    def report(self, replica, token, action="pin", ok=True, error=""):
        return self.client.post(
            f"/api/nodes/tasks/{replica.pk}",
            {"action": action, "ok": ok, "error": error},
            format="json",
            HTTP_AUTHORIZATION=f"Node {token}",
        )

    def test_register_with_invite(self):
        operator = Operator.objects.create(name="Ama")
        _, code = NodeInvite.issue(operator, "ama-harare")
        response = self.register(code, name="")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["operator"], "Ama")
        self.assertEqual(response.data["name"], "ama-harare")
        node = Node.objects.get()
        self.assertNotIn(response.data["token"], node.token_hash)
        self.assertEqual(Node.from_token(response.data["token"]), node)
        self.assertTrue(node.is_online)

    def test_invites_work_once(self):
        _, code = NodeInvite.issue(Operator.objects.create(name="Ama"))
        self.assertEqual(self.register(code).status_code, 201)
        self.assertEqual(self.register(code).status_code, 403)
        self.assertEqual(self.register("dsi_made-up").status_code, 403)

    def test_one_ipfs_identity_one_node(self):
        operator = Operator.objects.create(name="Ama")
        _, first = NodeInvite.issue(operator)
        _, second = NodeInvite.issue(operator)
        self.assertEqual(self.register(first, peer_id="12D3KooWsame").status_code, 201)
        self.assertEqual(self.register(second, peer_id="12D3KooWsame").status_code, 409)
        # The failed attempt didn't burn the invite.
        self.assertEqual(self.register(second).status_code, 201)

    def test_node_and_user_tokens_dont_mix(self):
        node, token = node_with_token("Ama")
        user_token = self.client.post("/api/auth/register", registration("thandi"), format="json").data["token"]

        response = self.client.post(
            "/api/nodes/heartbeat", {"peer_id": node.peer_id}, format="json", HTTP_AUTHORIZATION=f"Token {user_token}"
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.client.get("/api/files/", HTTP_AUTHORIZATION=f"Node {token}").status_code, 401)
        self.assertEqual(self.heartbeat(node, "dsn_wrong").status_code, 401)

    def test_heartbeat_updates_status_and_hands_out_tasks(self):
        nodes = [node_with_token(n) for n in ("Ama", "Bongani", "Chipo")]
        blob = make_blob()
        services.place_blobs()
        node, token = nodes[0]
        response = self.heartbeat(node, token, used_bytes=1234, capacity_bytes=5555, agent_version="0.2.0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["tasks"],
            [{"id": Replica.objects.get(node=node).pk, "action": "pin", "cid": blob.content_id, "size": blob.size}],
        )
        node.refresh_from_db()
        self.assertEqual((node.used_bytes, node.capacity_bytes, node.agent_version), (1234, 5555, "0.2.0"))

    def test_unpins_come_first(self):
        node, token = node_with_token("Ama")
        Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.ASSIGNED)
        Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.RELEASING)
        actions = [t["action"] for t in self.heartbeat(node, token).data["tasks"]]
        self.assertEqual(actions, ["unpin", "pin"])

    def test_pin_results(self):
        node, token = node_with_token("Ama")
        replica = Replica.objects.create(blob=make_blob(), node=node)
        self.assertEqual(self.report(replica, token).data["outcome"], "stored")
        replica.refresh_from_db()
        self.assertEqual(replica.state, Replica.State.STORED)
        self.assertIsNotNone(replica.stored_at)

    @override_settings(PIN_MAX_ATTEMPTS=2)
    def test_failed_pins_are_retried_then_given_up(self):
        node, token = node_with_token("Ama")
        replica = Replica.objects.create(blob=make_blob(), node=node)
        self.assertEqual(self.report(replica, token, ok=False, error="timeout").data["outcome"], "retry")
        replica.refresh_from_db()
        self.assertEqual((replica.state, replica.attempts, replica.last_error), ("assigned", 1, "timeout"))
        self.assertEqual(self.report(replica, token, ok=False, error="timeout").data["outcome"], "given_up")
        replica.refresh_from_db()
        self.assertEqual(replica.state, Replica.State.RELEASING)

    def test_unpin_confirmation_removes_the_replica(self):
        node, token = node_with_token("Ama")
        replica = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.RELEASING)
        self.assertEqual(self.report(replica, token, action="unpin").data["outcome"], "released")
        self.assertFalse(Replica.objects.exists())

    def test_late_results_are_ignored(self):
        node, token = node_with_token("Ama")
        replica = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.RELEASING)
        self.assertEqual(self.report(replica, token, action="pin").data["outcome"], "ignored")
        replica.refresh_from_db()
        self.assertEqual(replica.state, Replica.State.RELEASING)

    def test_cannot_report_on_another_nodes_replica(self):
        mine, token = node_with_token("Ama")
        theirs = make_node("Bongani")
        replica = Replica.objects.create(blob=make_blob(), node=theirs)
        self.assertEqual(self.report(replica, token).status_code, 404)

    def test_identity_change_marks_copies_lost_and_disables_the_node(self):
        node, token = node_with_token("Ama")
        stored = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.STORED)
        Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.RELEASING)

        response = self.heartbeat(node, token, peer_id="12D3KooWsomeoneelse")
        self.assertEqual(response.status_code, 409)
        stored.refresh_from_db()
        node.refresh_from_db()
        self.assertEqual(stored.state, Replica.State.LOST)
        self.assertEqual(Replica.objects.filter(node=node).count(), 1)
        self.assertEqual(node.status, Node.Status.DISABLED)
        self.assertEqual(self.heartbeat(node, token, peer_id="12D3KooWsomeoneelse").status_code, 409)

    def test_disabled_nodes_still_drop_copies_but_get_no_new_ones(self):
        node, token = node_with_token("Ama")
        assigned = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.ASSIGNED)
        stored = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.STORED)
        releasing = Replica.objects.create(blob=make_blob(), node=node, state=Replica.State.RELEASING)
        services.disable_nodes([node], "Disabled by an admin.")

        assigned.refresh_from_db()
        stored.refresh_from_db()
        self.assertEqual(assigned.state, Replica.State.RELEASING)  # goes elsewhere
        self.assertEqual(stored.state, Replica.State.STORED)  # still counts

        response = self.heartbeat(node, token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "disabled")
        self.assertEqual({t["action"] for t in response.data["tasks"]}, {"unpin"})
        self.assertEqual({t["id"] for t in response.data["tasks"]}, {assigned.pk, releasing.pk})

        services.place_blobs()
        self.assertFalse(Replica.objects.filter(node=node, state=Replica.State.ASSIGNED).exists())


class WorkerTests(FakeNetworkMixin, APITestCase):
    """The whole flow, with the stand-in IPFS gateway and simulated node agents."""

    def setUp(self):
        super().setUp()
        self.user_token = self.client.post("/api/auth/register", registration("thandi"), format="json").data["token"]
        self.nodes = [node_with_token(n) for n in ("Ama", "Bongani", "Chipo", "Dumi")]

    def as_user(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.user_token}")

    def upload(self, size, complete=True):
        self.as_user()
        file_id = str(uuid.uuid4())
        response = self.client.post(
            "/api/files/",
            {"id": file_id, "encrypted_name": b64(40), "wrapped_file_key": b64(61), "size": size,
             "segment_size": self.segment_size},
            format="json",
        )
        assert response.status_code == 201, response.data
        count = max(1, -(-size // self.segment_size))
        blobs = []
        for index in range(count if complete else count - 1):
            data = os.urandom(min(self.segment_size, size - index * self.segment_size) + TAG)
            response = self.client.put(
                f"/api/files/{file_id}/segments/{index}", data=data, content_type="application/octet-stream",
                HTTP_X_CONTENT_SHA256=hashlib.sha256(data).hexdigest(),
            )
            assert response.status_code == 201, response.data
            blobs.append(data)
        if complete:
            assert self.client.post(f"/api/files/{file_id}/complete").status_code == 200
        return file_id, blobs

    def act_as_nodes(self, ok=True):
        """Every node with tasks does them, the way the agent would."""
        for node, _ in self.nodes:
            for replica in Replica.objects.filter(node=node, state__in=[Replica.State.ASSIGNED, Replica.State.RELEASING]):
                action = "pin" if replica.state == Replica.State.ASSIGNED else "unpin"
                services.record_task_result(replica, action, ok)

    def test_upload_spreads_copies_then_the_gateway_lets_go(self):
        file_id, blobs = self.upload(2 * self.segment_size + 10)
        self.assertEqual(Blob.objects.filter(on_gateway=True).count(), 3)
        self.assertEqual(len(self.gateway.repo.pins), 3)

        self.as_user()
        self.assertEqual(self.client.get("/api/files/").data[0]["copies"], 0)

        report = services.run_pass()
        self.assertEqual(report.assigned, 9)
        self.act_as_nodes()
        report = services.run_pass()
        self.assertEqual(report.released_from_gateway, 3)
        self.assertEqual(self.gateway.repo.pins, set())

        self.as_user()
        listing = self.client.get("/api/files/").data[0]
        self.assertEqual((listing["copies"], listing["target_copies"]), (3, 3))
        self.assertEqual(self.client.get(f"/api/files/{file_id}").data["copies"], 3)

    def test_downloads_come_from_nodes_once_the_gateway_is_empty(self):
        file_id, blobs = self.upload(500)
        services.run_pass()
        # Give one real stand-in node the data, then let the gateway forget it.
        holder = self.spawn_kubo("holder")
        holder.repo.pin(holder.repo.put(blobs[0]))
        self.gateway.peers = [holder.api_url]
        self.act_as_nodes()
        services.run_pass()
        services.collect_gateway_garbage()
        self.assertFalse(self.gateway.repo.has(Segment.objects.get().content_id))

        self.as_user()
        response = self.client.get(f"/api/files/{file_id}/segments/0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, blobs[0])

    def test_unreachable_copies_mean_unavailable_not_wrong_data(self):
        file_id, _ = self.upload(500)
        services.run_pass()
        self.act_as_nodes()
        services.run_pass()
        services.collect_gateway_garbage()  # gateway empty, and no stand-in node actually holds it
        self.as_user()
        self.assertEqual(self.client.get(f"/api/files/{file_id}/segments/0").status_code, 503)

    @override_settings(ASSIGNMENT_TIMEOUT_SECONDS=60)
    def test_slow_copies_are_moved_elsewhere(self):
        self.upload(500)
        services.run_pass()
        blob = Blob.objects.get()
        first = {r.node_id for r in live_replicas(blob)}
        later = timezone.now() + timedelta(seconds=61)
        report = services.run_pass(now=later)
        self.assertEqual(report.expired, 3)
        replicas = live_replicas(blob)
        # Only one node is left that wasn't tried, so one copy moves now; the
        # rest follow once those nodes confirm their unpins.
        self.assertEqual({r.node_id for r in replicas} - first, {n.id for n, _ in self.nodes} - first)
        self.assertEqual(Replica.objects.filter(blob=blob, state=Replica.State.RELEASING).count(), 3)

    def test_delete_releases_every_copy(self):
        file_id, _ = self.upload(2 * self.segment_size)
        services.run_pass()
        self.act_as_nodes()
        services.run_pass()

        self.as_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"/api/files/{file_id}").status_code, 204)
        self.assertEqual(Replica.objects.exclude(state=Replica.State.RELEASING).count(), 0)
        self.assertEqual(Replica.objects.count(), 6)
        self.assertEqual(services.run_pass().blobs_removed, 0)  # nodes haven't confirmed yet

        self.act_as_nodes()
        self.assertEqual(services.run_pass().blobs_removed, 2)
        self.assertFalse(Blob.objects.exists())

    def test_delete_during_upload_releases_the_gateway_copy(self):
        file_id, _ = self.upload(2 * self.segment_size, complete=False)
        self.assertEqual(len(self.gateway.repo.pins), 1)
        self.as_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(f"/api/files/{file_id}")
        self.assertEqual(self.gateway.repo.pins, set())
        services.run_pass()
        self.assertFalse(Blob.objects.exists())

    @override_settings(ABANDONED_UPLOAD_AFTER_HOURS=1)
    def test_abandoned_uploads_are_cleared(self):
        self.upload(2 * self.segment_size, complete=False)
        done_id, _ = self.upload(100)
        File.objects.update(created_at=timezone.now() - timedelta(hours=2))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(services.run_pass().abandoned_uploads, 1)
        self.assertEqual(list(File.objects.values_list("id", flat=True)), [uuid.UUID(done_id)])

    def test_reupload_brings_a_deleted_blob_back(self):
        blob = make_blob(deleted_at=timezone.now(), on_gateway=False)
        services.register_blob(blob.content_id, blob.size, blob.sha256)
        blob.refresh_from_db()
        self.assertIsNone(blob.deleted_at)
        self.assertTrue(blob.on_gateway)

    def test_storage_outage_during_upload_is_retryable(self):
        self.as_user()
        file_id = str(uuid.uuid4())
        self.client.post(
            "/api/files/",
            {"id": file_id, "encrypted_name": b64(40), "wrapped_file_key": b64(61), "size": 10,
             "segment_size": self.segment_size},
            format="json",
        )
        self.gateway.stop()
        self._kubos.remove(self.gateway)
        data = os.urandom(10 + TAG)
        response = self.client.put(
            f"/api/files/{file_id}/segments/0", data=data, content_type="application/octet-stream",
            HTTP_X_CONTENT_SHA256=hashlib.sha256(data).hexdigest(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Segment.objects.exists())


class LocalModeTests(APITestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.settings_override = override_settings(
            SEGMENT_STORE={"BACKEND": "storage.backends.LocalSegmentStore", "OPTIONS": {"root": self.tmp.name}}
        )
        self.settings_override.enable()
        get_segment_store.cache_clear()

    def tearDown(self):
        self.settings_override.disable()
        get_segment_store.cache_clear()
        self.tmp.cleanup()

    def test_no_placement_and_no_copy_count_without_a_network(self):
        for name in ("Ama", "Bongani", "Chipo"):
            make_node(name)
        make_blob()
        self.assertEqual(services.run_pass().assigned, 0)
        token = self.client.post("/api/auth/register", registration("thandi"), format="json").data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.client.post(
            "/api/files/",
            {"id": str(uuid.uuid4()), "encrypted_name": b64(40), "wrapped_file_key": b64(61), "size": 10,
             "segment_size": 4 * 1024 * 1024},
            format="json",
        )
        self.assertIsNone(self.client.get("/api/files/").data[0]["copies"])


class CommandTests(FakeNetworkMixin, TestCase):
    def run_command(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        call_command(*args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_create_swarm_key(self):
        out, _ = self.run_command("create_swarm_key")
        key = out.strip().split("=", 1)[1]
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_invite_node_prints_everything_a_node_needs(self):
        out, err = self.run_command("invite_node", "--operator", "Ama", "--node-name", "harare-1")
        values = dict(line.split("=", 1) for line in out.strip().splitlines())
        self.assertEqual(values["IPFS_SWARM_KEY"], SWARM_KEY)
        self.assertEqual(values["NODE_NAME"], "harare-1")
        self.assertEqual(
            values["GATEWAY_BOOTSTRAP"], f"/ip4/127.0.0.1/tcp/4001/p2p/{self.gateway.repo.peer_id}"
        )
        self.assertIsNotNone(NodeInvite.redeemable(values["ENROLLMENT_CODE"]))
        self.assertIn("Created operator", err)

        out, err = self.run_command("invite_node", "--operator", "Ama", "--json")
        self.assertNotIn("Created operator", err)
        self.assertEqual(Operator.objects.count(), 1)
        self.assertEqual(NodeInvite.objects.count(), 2)
        self.assertIn("ENROLLMENT_CODE", json.loads(out))

    @override_settings(GATEWAY_P2P_HOST="storage.example.com")
    def test_invite_uses_dns_for_hostnames(self):
        out, _ = self.run_command("invite_node", "--operator", "Ama", "--json")
        self.assertTrue(json.loads(out)["GATEWAY_BOOTSTRAP"].startswith("/dns4/storage.example.com/tcp/4001/p2p/"))

    @override_settings(IPFS_SWARM_KEY="")
    def test_invite_needs_a_swarm_key(self):
        with self.assertRaises(CommandError):
            self.run_command("invite_node", "--operator", "Ama")
        self.assertFalse(Operator.objects.exists())

    def test_network_status(self):
        make_node("Ama")
        make_node("Bongani", online=False)
        make_blob()
        out, _ = self.run_command("network_status")
        self.assertIn("Ama", out)
        self.assertIn("offline", out)
        self.assertIn("Warning: only 1 operator", out)
        summary = json.loads(self.run_command("network_status", "--json")[0])
        self.assertEqual(summary["blobs"]["under_replicated"], 1)
        self.assertEqual(len(summary["nodes"]), 2)

    def test_run_worker_once(self):
        for name in ("Ama", "Bongani", "Chipo"):
            make_node(name)
        make_blob()
        self.run_command("run_worker", "--once")
        self.assertEqual(Replica.objects.count(), 3)

    def test_import_local_segments(self):
        staging = os.path.join(self._tmp.name, "old-staging")
        local = LocalSegmentStore(staging)
        data = os.urandom(300)
        content_id = local.put(data)
        user = self._make_user()
        file = File.objects.create(
            id=uuid.uuid4(), owner=user, encrypted_name="x", wrapped_file_key="y", segment_size=1024, segment_count=1
        )
        Segment.objects.create(file=file, index=0, size=300, sha256=hashlib.sha256(data).hexdigest(),
                               content_id=content_id)
        Blob.objects.create(content_id=content_id, size=300, sha256=hashlib.sha256(data).hexdigest())

        new_id = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
        store = get_segment_store()
        with mock.patch.object(type(store), "put", autospec=True, side_effect=lambda self, d: (
            self.client.add(d), new_id)[1]):
            out, _ = self.run_command("import_local_segments", "--staging-dir", staging)
        self.assertIn("Moved 1", out)
        self.assertEqual(Segment.objects.get().content_id, new_id)
        self.assertEqual(Blob.objects.get().content_id, new_id)
        self.assertIn(content_id, self.gateway.repo.pins)  # the bytes really went into IPFS

        out, _ = self.run_command("import_local_segments", "--staging-dir", staging)
        self.assertIn("Moved 0", out)

    def _make_user(self):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create(username="thandi")
