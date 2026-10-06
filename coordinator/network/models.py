"""
The storage network: who runs nodes, which nodes exist, and which node holds
which encrypted segment.

Vocabulary
- Operator: a person or organisation running one or more nodes. Copies of the
  same segment always go to different operators, so one operator switching off
  (or walking away) never takes every copy with them.
- Node: one machine running IPFS (Kubo) and the dstore agent.
- Blob: one encrypted segment as stored on the network, identified by its CID.
  Nothing in it is readable without the owner's keys.
- Replica: "this node should hold / holds / should drop this blob".
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Operator(models.Model):
    name = models.CharField(max_length=120, unique=True)
    contact = models.CharField(max_length=200, blank=True, help_text="Phone or email, for support and payouts.")
    is_active = models.BooleanField(default=True, help_text="Inactive operators' nodes get no new copies.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class NodeInvite(models.Model):
    """A single-use enrollment code an operator pastes into their node's settings."""

    operator = models.ForeignKey(Operator, on_delete=models.CASCADE, related_name="invites")
    code_hash = models.CharField(max_length=64, unique=True)
    node_name = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invite for {self.operator} ({'used' if self.used_at else 'unused'})"

    @classmethod
    def issue(cls, operator, node_name="", ttl=None):
        raw = "dsi_" + secrets.token_urlsafe(24)
        ttl = ttl or timedelta(hours=settings.NODE_INVITE_TTL_HOURS)
        invite = cls.objects.create(
            operator=operator, code_hash=hash_secret(raw), node_name=node_name, expires_at=timezone.now() + ttl
        )
        return invite, raw

    @classmethod
    def redeemable(cls, raw):
        return (
            cls.objects.select_related("operator")
            .filter(code_hash=hash_secret(raw), used_at__isnull=True, expires_at__gt=timezone.now())
            .first()
        )


class NodeQuerySet(models.QuerySet):
    def online(self, now=None):
        now = now or timezone.now()
        return self.filter(last_seen_at__gte=now - timedelta(seconds=settings.NODE_OFFLINE_AFTER_SECONDS))

    def eligible(self, now=None):
        """Nodes that may receive new copies right now."""
        return self.online(now).filter(status=Node.Status.ACTIVE, operator__is_active=True)


class Node(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        DISABLED = "disabled"

    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="nodes")
    name = models.CharField(max_length=80)
    peer_id = models.CharField(max_length=128, unique=True, help_text="The node's IPFS identity.")
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    status_reason = models.CharField(max_length=200, blank=True)
    capacity_bytes = models.BigIntegerField(default=0, help_text="Space the operator set aside (Kubo StorageMax).")
    used_bytes = models.BigIntegerField(default=0, help_text="Space IPFS reports as used.")
    pinned_count = models.PositiveIntegerField(default=0)
    addresses = models.JSONField(default=list, blank=True)
    agent_version = models.CharField(max_length=40, blank=True)
    kubo_version = models.CharField(max_length=40, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = NodeQuerySet.as_manager()

    class Meta:
        ordering = ["operator__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.operator})"

    @classmethod
    def new_token(cls):
        raw = "dsn_" + secrets.token_urlsafe(32)
        return raw, hash_secret(raw)

    @classmethod
    def from_token(cls, raw):
        return cls.objects.select_related("operator").filter(token_hash=hash_secret(raw)).first()

    @property
    def is_online(self):
        if self.last_seen_at is None:
            return False
        return timezone.now() - self.last_seen_at <= timedelta(seconds=settings.NODE_OFFLINE_AFTER_SECONDS)


class BlobQuerySet(models.QuerySet):
    def live(self):
        return self.filter(deleted_at__isnull=True)

    def with_copy_counts(self):
        return self.annotate(
            stored_copies=Count("replicas", filter=Q(replicas__state=Replica.State.STORED)),
            live_copies=Count("replicas", filter=Q(replicas__state__in=Replica.LIVE_STATES)),
        )


class Blob(models.Model):
    """
    One encrypted segment as the network sees it. Segment rows (in the files app)
    point here by content ID; several files could in principle share a blob, so a
    blob is only released once no segment refers to it.
    """

    content_id = models.CharField(max_length=128, unique=True)
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    on_gateway = models.BooleanField(default=True, help_text="The gateway still holds its staging copy.")
    released_from_gateway_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="No file uses it any more; copies are being dropped.")

    objects = BlobQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["deleted_at", "on_gateway"])]

    def __str__(self):
        return self.content_id


class Replica(models.Model):
    """
    One node's copy of one blob.

    assigned  → the node should fetch and pin it (the agent picks this up)
    stored    → the node confirmed it holds it
    releasing → the node should unpin it; the row is deleted once it confirms
    lost      → the node can no longer be trusted to hold it (e.g. its IPFS
                identity changed); kept for the record, never counted
    """

    class State(models.TextChoices):
        ASSIGNED = "assigned"
        STORED = "stored"
        RELEASING = "releasing"
        LOST = "lost"

    LIVE_STATES = (State.ASSIGNED, State.STORED)

    blob = models.ForeignKey(Blob, on_delete=models.CASCADE, related_name="replicas")
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="replicas")
    state = models.CharField(max_length=16, choices=State.choices, default=State.ASSIGNED)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    stored_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["blob_id", "node_id"]
        constraints = [models.UniqueConstraint(fields=["blob", "node"], name="one_replica_per_node")]
        indexes = [models.Index(fields=["node", "state"]), models.Index(fields=["state", "assigned_at"])]

    def __str__(self):
        return f"{self.blob_id}@{self.node_id} ({self.state})"
