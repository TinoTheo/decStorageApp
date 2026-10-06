"""
The API storage nodes talk to.

Nodes always call the coordinator, never the other way round, so a node behind
a home router or a mobile connection works without any port forwarding:

1. register   — once, with the operator's single-use invite code
2. heartbeat  — every few seconds: "here's my status, what should I do?"
3. tasks/<id> — "I finished pinning (or unpinning) this, here's how it went"
"""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.throttling import AuthIPRateThrottle

from .authentication import IsNode, NodeTokenAuthentication
from .models import Node, NodeInvite, Replica
from .serializers import NodeStatusSerializer, RegisterSerializer, TaskResultSerializer
from .services import mark_node_lost, record_task_result, tasks_for

STATUS_FIELDS = ("capacity_bytes", "used_bytes", "pinned_count", "agent_version", "kubo_version", "addresses")


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AuthIPRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invite = NodeInvite.redeemable(data["code"])
        if invite is None:
            return Response(
                {"detail": "That invite code is invalid, expired or already used. Ask for a new one."},
                status=status.HTTP_403_FORBIDDEN,
            )

        raw_token, token_hash = Node.new_token()
        now = timezone.now()
        try:
            with transaction.atomic():
                claimed = NodeInvite.objects.filter(pk=invite.pk, used_at__isnull=True).update(used_at=now)
                if not claimed:
                    raise IntegrityError("invite already used")
                node = Node.objects.create(
                    operator=invite.operator,
                    name=data["name"] or invite.node_name or f"node-{data['peer_id'][-6:]}",
                    peer_id=data["peer_id"],
                    token_hash=token_hash,
                    last_seen_at=now,
                    **{field: data[field] for field in STATUS_FIELDS},
                )
        except IntegrityError:
            if Node.objects.filter(peer_id=data["peer_id"]).exists():
                return Response(
                    {"detail": "This IPFS identity is already registered as another node."},
                    status=status.HTTP_409_CONFLICT,
                )
            return Response({"detail": "That invite code was just used."}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                "node_id": node.id,
                "name": node.name,
                "operator": node.operator.name,
                "token": raw_token,
                "heartbeat_seconds": settings.NODE_HEARTBEAT_SECONDS,
            },
            status=status.HTTP_201_CREATED,
        )


class NodeEndpoint(APIView):
    authentication_classes = [NodeTokenAuthentication]
    permission_classes = [IsNode]


class HeartbeatView(NodeEndpoint):
    def post(self, request):
        node = request.auth
        serializer = NodeStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data["peer_id"] != node.peer_id:
            # A new IPFS identity means the node's storage was reset, so nothing
            # it confirmed before can be trusted to still be there.
            mark_node_lost(node, "Its IPFS identity changed (storage reset?). Re-enroll it with a new invite.")
            return Response(
                {"detail": "This node's IPFS identity changed. Its copies were marked lost; re-enroll it."},
                status=status.HTTP_409_CONFLICT,
            )

        Node.objects.filter(pk=node.pk).update(last_seen_at=timezone.now(), **{f: data[f] for f in STATUS_FIELDS})
        return Response(
            {
                "tasks": tasks_for(node),
                "heartbeat_seconds": settings.NODE_HEARTBEAT_SECONDS,
                "status": node.status,
                "status_reason": node.status_reason,
            }
        )


class TaskResultView(NodeEndpoint):
    def post(self, request, replica_id):
        replica = get_object_or_404(Replica.objects.select_related("blob"), pk=replica_id, node=request.auth)
        serializer = TaskResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        outcome = record_task_result(replica, data["action"], data["ok"], data["error"])
        return Response({"outcome": outcome})
