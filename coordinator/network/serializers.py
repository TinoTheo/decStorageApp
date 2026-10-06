from rest_framework import serializers

MAX_ADDRESSES = 20


class NodeStatusSerializer(serializers.Serializer):
    """What a node reports about itself on every heartbeat (and at registration)."""

    peer_id = serializers.CharField(max_length=128)
    capacity_bytes = serializers.IntegerField(min_value=0)
    used_bytes = serializers.IntegerField(min_value=0)
    pinned_count = serializers.IntegerField(min_value=0, required=False, default=0)
    agent_version = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    kubo_version = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    addresses = serializers.ListField(
        child=serializers.CharField(max_length=300), required=False, default=list, max_length=MAX_ADDRESSES
    )


class RegisterSerializer(NodeStatusSerializer):
    code = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")


class TaskResultSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["pin", "unpin"])
    ok = serializers.BooleanField()
    error = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
