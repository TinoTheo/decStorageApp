"""
Everything that decides where copies live and keeps that true over time.

The web request path only calls `register_blob` (on upload) and `delete_file`
(on delete). Everything else runs in the background worker
(`manage.py run_worker`), one pass at a time, so there's a single place where
copies are assigned, confirmed, released and cleaned up.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from storage.backends import SegmentStoreError, get_segment_store

from .models import Blob, Node, Replica

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Called from the upload and delete paths


def register_blob(content_id, size, sha256):
    """
    Record a freshly stored segment. A blob that was on its way out (its last
    file was deleted) is brought back, because the gateway now holds it again.
    """
    blob, created = Blob.objects.get_or_create(content_id=content_id, defaults={"size": size, "sha256": sha256})
    if not created and (blob.deleted_at is not None or not blob.on_gateway):
        Blob.objects.filter(pk=blob.pk).update(deleted_at=None, on_gateway=True, released_from_gateway_at=None)
    return blob


def delete_file(file):
    """
    Delete a file and start dropping any blobs no other file uses: every node
    holding a copy is told to unpin it, and the gateway drops its staging copy.
    """
    from files.models import Segment

    with transaction.atomic():
        content_ids = set(file.segments.values_list("content_id", flat=True))
        file.delete()
        still_used = set(Segment.objects.filter(content_id__in=content_ids).values_list("content_id", flat=True))
        orphaned = content_ids - still_used
        if orphaned:
            _start_release(Blob.objects.filter(content_id__in=orphaned))
            transaction.on_commit(lambda: release_gateway_copies(Blob.objects.filter(content_id__in=orphaned)))


def _start_release(blobs):
    now = timezone.now()
    blob_ids = list(blobs.values_list("id", flat=True))
    Blob.objects.filter(id__in=blob_ids, deleted_at__isnull=True).update(deleted_at=now)
    Replica.objects.filter(blob_id__in=blob_ids, state__in=Replica.LIVE_STATES).update(
        state=Replica.State.RELEASING, updated_at=now
    )
    # Nothing to unpin on a node we no longer trust to hold the data.
    Replica.objects.filter(blob_id__in=blob_ids, state=Replica.State.LOST).delete()


def release_gateway_copies(blobs):
    """Unpin the gateway's staging copy. Failures are retried by the worker."""
    store = get_segment_store()
    released = 0
    for blob in blobs.filter(on_gateway=True):
        try:
            store.release(blob.content_id)
        except SegmentStoreError:
            logger.warning("Could not release %s from the gateway; will retry", blob.content_id, exc_info=True)
            continue
        Blob.objects.filter(pk=blob.pk).update(on_gateway=False, released_from_gateway_at=timezone.now())
        released += 1
    return released


# ---------------------------------------------------------------------------
# Placement


@dataclass
class _Candidate:
    node: Node
    free_bytes: int


@dataclass
class PlacementResult:
    assigned: int = 0
    short: list = field(default_factory=list)  # content IDs still missing copies for lack of nodes


def _candidates(now):
    """Eligible nodes with how much room each has left, counting copies already on their way."""
    pending = dict(
        Replica.objects.filter(state=Replica.State.ASSIGNED)
        .values("node_id")
        .annotate(total=Sum("blob__size"))
        .values_list("node_id", "total")
    )
    result = []
    for node in Node.objects.eligible(now).select_related("operator"):
        free = node.capacity_bytes - node.used_bytes - (pending.get(node.id) or 0)
        result.append(_Candidate(node=node, free_bytes=free))
    return result


def place_blobs(limit=200, now=None):
    """
    Give every live blob `REPLICA_COUNT` copies on different nodes.

    Rules, in order:
    1. Only online, active nodes whose operator is active.
    2. Never two live copies of the same blob with one operator (unless the
       network is configured to allow it).
    3. The node must have room for the blob plus a safety margin.
    4. Among the rest, prefer the nodes with the most free space, with a little
       randomness so equal nodes share the load.
    A blob that can't get all its copies yet keeps what it has and is retried on
    the next pass.
    """
    now = now or timezone.now()
    target = settings.REPLICA_COUNT
    distinct = settings.REQUIRE_DISTINCT_OPERATORS
    headroom = settings.PLACEMENT_HEADROOM_BYTES
    result = PlacementResult()

    needy = list(
        Blob.objects.live()
        .with_copy_counts()
        .filter(live_copies__lt=target)
        .order_by("created_at")[:limit]
    )
    if not needy:
        return result

    candidates = _candidates(now)
    for blob in needy:
        existing = list(Replica.objects.filter(blob=blob).select_related("node"))
        taken_nodes = {r.node_id for r in existing}
        taken_operators = {r.node.operator_id for r in existing if r.state in Replica.LIVE_STATES}
        missing = target - sum(1 for r in existing if r.state in Replica.LIVE_STATES)

        pool = [c for c in candidates if c.node.id not in taken_nodes and c.free_bytes >= blob.size + headroom]
        random.shuffle(pool)
        pool.sort(key=lambda c: c.free_bytes, reverse=True)

        chosen = []
        for candidate in pool:
            if len(chosen) == missing:
                break
            if distinct and candidate.node.operator_id in taken_operators:
                continue
            chosen.append(candidate)
            taken_operators.add(candidate.node.operator_id)

        for candidate in chosen:
            Replica.objects.create(blob=blob, node=candidate.node, state=Replica.State.ASSIGNED, assigned_at=now)
            candidate.free_bytes -= blob.size
            result.assigned += 1
        if len(chosen) < missing:
            result.short.append(blob.content_id)

    return result


# ---------------------------------------------------------------------------
# Node task results


def tasks_for(node, limit=None):
    """
    What this node should do next: unpins first (they free space), then pins.
    A disabled node only gets unpins, so deletions still reach it.
    """
    limit = limit or settings.NODE_TASK_BATCH
    releasing = list(
        Replica.objects.filter(node=node, state=Replica.State.RELEASING).select_related("blob").order_by("updated_at")[
            :limit
        ]
    )
    pins = []
    if node.status == Node.Status.ACTIVE:
        pins = list(
            Replica.objects.filter(node=node, state=Replica.State.ASSIGNED)
            .select_related("blob")
            .order_by("assigned_at")[: max(0, limit - len(releasing))]
        )
    tasks = [{"id": r.id, "action": "unpin", "cid": r.blob.content_id, "size": r.blob.size} for r in releasing]
    tasks += [{"id": r.id, "action": "pin", "cid": r.blob.content_id, "size": r.blob.size} for r in pins]
    return tasks


def record_task_result(replica, action, ok, error=""):
    """
    Apply what a node reports. Results for work the coordinator no longer wants
    (say, a pin that finished after the file was deleted) are ignored; the node
    gets the follow-up task on its next heartbeat.
    """
    now = timezone.now()
    error = (error or "")[:500]

    if action == "pin" and replica.state == Replica.State.ASSIGNED:
        if ok:
            Replica.objects.filter(pk=replica.pk, state=Replica.State.ASSIGNED).update(
                state=Replica.State.STORED, stored_at=now, last_error="", updated_at=now
            )
            return "stored"
        attempts = replica.attempts + 1
        if attempts >= settings.PIN_MAX_ATTEMPTS:
            Replica.objects.filter(pk=replica.pk).update(
                state=Replica.State.RELEASING, attempts=attempts, last_error=error, updated_at=now
            )
            return "given_up"
        Replica.objects.filter(pk=replica.pk).update(attempts=attempts, last_error=error, updated_at=now)
        return "retry"

    if action == "unpin" and replica.state == Replica.State.RELEASING:
        if ok:
            replica.delete()
            return "released"
        Replica.objects.filter(pk=replica.pk).update(last_error=error, updated_at=now)
        return "retry"

    return "ignored"


def disable_nodes(nodes, reason):
    """
    No new copies for these nodes. Copies they were about to fetch go elsewhere;
    copies they already hold keep counting until Self Repair moves them.
    """
    now = timezone.now()
    node_ids = [n.pk for n in nodes]
    with transaction.atomic():
        Node.objects.filter(pk__in=node_ids).update(status=Node.Status.DISABLED, status_reason=reason[:200])
        Replica.objects.filter(node_id__in=node_ids, state=Replica.State.ASSIGNED).update(
            state=Replica.State.RELEASING, last_error="Node disabled before it confirmed.", updated_at=now
        )


def mark_node_lost(node, reason):
    """
    The node can't be trusted to hold what it held (for example its IPFS
    identity changed, which means its storage was wiped). Its copies stop
    counting, so placement finds new homes for them, and the node is disabled
    until the operator re-enrolls it.
    """
    now = timezone.now()
    with transaction.atomic():
        Replica.objects.filter(node=node, state__in=Replica.LIVE_STATES).update(
            state=Replica.State.LOST, last_error=reason[:500], updated_at=now
        )
        Replica.objects.filter(node=node, state=Replica.State.RELEASING).delete()
        Node.objects.filter(pk=node.pk).update(status=Node.Status.DISABLED, status_reason=reason[:200])


# ---------------------------------------------------------------------------
# Background passes


def expire_stale_assignments(now=None):
    """A copy that hasn't arrived within the timeout is dropped and placed elsewhere."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.ASSIGNMENT_TIMEOUT_SECONDS)
    return Replica.objects.filter(state=Replica.State.ASSIGNED, assigned_at__lt=cutoff).update(
        state=Replica.State.RELEASING, last_error="Timed out before the node confirmed it.", updated_at=now
    )


def release_replicated_from_gateway():
    """Once enough nodes confirm a blob, the gateway stops holding its own copy."""
    if not get_segment_store().supports_network:
        return 0
    ready = Blob.objects.live().filter(on_gateway=True).with_copy_counts().filter(
        stored_copies__gte=settings.REPLICA_COUNT
    )
    return release_gateway_copies(Blob.objects.filter(pk__in=ready.values("pk")))


def finish_deletions():
    """Retry gateway releases for deleted blobs and drop blob rows nobody holds any more."""
    deleted = Blob.objects.filter(deleted_at__isnull=False)
    _start_release(deleted)  # idempotent: catches copies assigned after the delete
    release_gateway_copies(deleted)
    done = deleted.filter(on_gateway=False).exclude(replicas__isnull=False)
    count, _ = done.delete()
    return count


def delete_abandoned_uploads(now=None):
    from files.models import File

    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.ABANDONED_UPLOAD_AFTER_HOURS)
    stale = list(File.objects.filter(status=File.Status.UPLOADING, created_at__lt=cutoff))
    for file in stale:
        delete_file(file)
    return len(stale)


def collect_gateway_garbage():
    store = get_segment_store()
    try:
        store.collect_garbage()
    except SegmentStoreError:
        logger.warning("Gateway garbage collection failed", exc_info=True)
        return False
    return True


@dataclass
class PassReport:
    expired: int = 0
    assigned: int = 0
    short: int = 0
    released_from_gateway: int = 0
    blobs_removed: int = 0
    abandoned_uploads: int = 0

    def any(self):
        return any(vars(self).values())


def run_pass(now=None, *, include_slow=True):
    """One worker pass. Placement only runs when the store is on the network."""
    now = now or timezone.now()
    report = PassReport()
    report.expired = expire_stale_assignments(now)
    if get_segment_store().supports_network:
        placement = place_blobs(now=now)
        report.assigned = placement.assigned
        report.short = len(placement.short)
        report.released_from_gateway = release_replicated_from_gateway()
    report.blobs_removed = finish_deletions()
    if include_slow:
        report.abandoned_uploads = delete_abandoned_uploads(now)
    return report


def copies_by_content_id(content_ids):
    """How many confirmed copies each blob has, for showing users their files are safe."""
    rows = (
        Blob.objects.filter(content_id__in=content_ids)
        .with_copy_counts()
        .values_list("content_id", "stored_copies")
    )
    return dict(rows)


def network_summary(now=None):
    now = now or timezone.now()
    nodes = list(Node.objects.select_related("operator").order_by("operator__name", "name"))
    replica_counts = {}
    for node_id, state, count in (
        Replica.objects.values("node_id", "state").annotate(n=Count("id")).values_list("node_id", "state", "n")
    ):
        replica_counts.setdefault(node_id, {})[state] = count

    target = settings.REPLICA_COUNT
    blobs = Blob.objects.live().with_copy_counts()
    eligible_operators = (
        Node.objects.eligible(now).values("operator_id").distinct().count()
    )
    return {
        "target_copies": target,
        "eligible_operators": eligible_operators,
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "operator": n.operator.name,
                "status": n.status,
                "status_reason": n.status_reason,
                "online": n.is_online,
                "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
                "capacity_bytes": n.capacity_bytes,
                "used_bytes": n.used_bytes,
                "peer_id": n.peer_id,
                "replicas": replica_counts.get(n.id, {}),
            }
            for n in nodes
        ],
        "blobs": {
            "total": blobs.count(),
            "fully_replicated": blobs.filter(stored_copies__gte=target).count(),
            "under_replicated": blobs.filter(stored_copies__lt=target).count(),
            "on_gateway": Blob.objects.live().filter(on_gateway=True).count(),
            "being_deleted": Blob.objects.filter(deleted_at__isnull=False).count(),
        },
    }

