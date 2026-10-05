import math

from django.conf import settings
from rest_framework import serializers

from accounts.fields import Base64BytesField, WrappedKeyField

from .models import File, Segment

# version byte + nonce + at least one ciphertext byte + tag
MIN_ENCRYPTED_NAME_BYTES = 1 + 12 + 1 + 16
MAX_ENCRYPTED_NAME_BYTES = 1 + 12 + 1024 + 16


class FileCreateSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    encrypted_name = Base64BytesField(min_length_bytes=MIN_ENCRYPTED_NAME_BYTES, max_length_bytes=MAX_ENCRYPTED_NAME_BYTES)
    wrapped_file_key = WrappedKeyField()
    size = serializers.IntegerField(min_value=0, help_text="Plaintext size in bytes.")
    segment_size = serializers.IntegerField()

    def validate_size(self, value):
        if value > settings.MAX_FILE_SIZE:
            raise serializers.ValidationError(f"Files are limited to {settings.MAX_FILE_SIZE} bytes.")
        return value

    def validate_segment_size(self, value):
        if value != settings.SEGMENT_SIZE:
            raise serializers.ValidationError(f"Segment size must be {settings.SEGMENT_SIZE}.")
        return value

    def create(self, validated_data):
        size = validated_data.pop("size")
        segment_count = max(1, math.ceil(size / validated_data["segment_size"]))
        return File.objects.create(owner=self.context["owner"], segment_count=segment_count, **validated_data)


class SegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Segment
        fields = ["index", "size", "sha256", "content_id"]


class FileSummarySerializer(serializers.ModelSerializer):
    size = serializers.SerializerMethodField()

    class Meta:
        model = File
        fields = [
            "id",
            "encrypted_name",
            "wrapped_file_key",
            "status",
            "size",
            "segment_size",
            "segment_count",
            "created_at",
            "completed_at",
        ]

    def get_size(self, obj):
        stored_bytes = getattr(obj, "stored_bytes", None)
        stored_segments = getattr(obj, "stored_segments", None)
        if stored_bytes is None or stored_segments is None:
            return obj.plaintext_size()
        return max((stored_bytes or 0) - settings.GCM_TAG_BYTES * stored_segments, 0)


class FileManifestSerializer(FileSummarySerializer):
    segments = SegmentSerializer(many=True, read_only=True)
    missing_segments = serializers.SerializerMethodField()

    class Meta(FileSummarySerializer.Meta):
        fields = FileSummarySerializer.Meta.fields + ["segments", "missing_segments"]

    def get_size(self, obj):
        return obj.plaintext_size(list(obj.segments.all()))

    def get_missing_segments(self, obj):
        present = {s.index for s in obj.segments.all()}
        return [i for i in range(obj.segment_count) if i not in present]
