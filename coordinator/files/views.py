"""
File and segment endpoints.

The browser creates a file record, PUTs each encrypted segment (in parallel, in
any order, retrying freely), then marks the file complete. Every segment is
checked against the SHA-256 the browser sends, so corruption in transit is caught
before anything is stored.
"""

import hashlib
import logging
import re

from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Sum
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from storage.backends import SegmentNotFound, get_segment_store

from .models import File, Segment
from .serializers import FileCreateSerializer, FileManifestSerializer, FileSummarySerializer, SegmentSerializer

logger = logging.getLogger(__name__)

SHA256_HEADER = "X-Content-SHA256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _owned_file(request, file_id, *, with_segments=False):
    queryset = File.objects.filter(owner=request.user)
    if with_segments:
        queryset = queryset.prefetch_related(Prefetch("segments", queryset=Segment.objects.order_by("index")))
    return get_object_or_404(queryset, id=file_id)


def _manifest(file):
    file = File.objects.prefetch_related(Prefetch("segments", queryset=Segment.objects.order_by("index"))).get(
        pk=file.pk
    )
    return FileManifestSerializer(file).data


class FileListCreateView(APIView):
    def get(self, request):
        files = File.objects.filter(owner=request.user).annotate(
            stored_bytes=Sum("segments__size"), stored_segments=Count("segments")
        )
        return Response(FileSummarySerializer(files, many=True).data)

    def post(self, request):
        serializer = FileCreateSerializer(data=request.data, context={"owner": request.user})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                file = serializer.save()
        except IntegrityError:
            return Response({"id": ["A file with this ID already exists."]}, status=status.HTTP_409_CONFLICT)
        return Response(_manifest(file), status=status.HTTP_201_CREATED)


class FileDetailView(APIView):
    def get(self, request, file_id):
        file = _owned_file(request, file_id, with_segments=True)
        return Response(FileManifestSerializer(file).data)

    def delete(self, request, file_id):
        file = _owned_file(request, file_id)
        content_ids = set(file.segments.values_list("content_id", flat=True))

        with transaction.atomic():
            file.delete()
            still_used = set(
                Segment.objects.filter(content_id__in=content_ids).values_list("content_id", flat=True)
            )
            orphaned = content_ids - still_used
            # Only remove bytes once the database change is committed. In M2 this
            # becomes "schedule unpin on every node holding a replica".
            transaction.on_commit(lambda: _delete_blobs(orphaned))

        return Response(status=status.HTTP_204_NO_CONTENT)


def _delete_blobs(content_ids):
    store = get_segment_store()
    for content_id in content_ids:
        try:
            store.delete(content_id)
        except Exception:  # noqa: BLE001 - a failed cleanup must never fail the request
            logger.exception("Failed to delete segment %s", content_id)


class SegmentView(APIView):
    def put(self, request, file_id, index):
        file = _owned_file(request, file_id)
        if file.status != File.Status.UPLOADING:
            return Response({"detail": "This file is already complete."}, status=status.HTTP_409_CONFLICT)
        if index >= file.segment_count:
            return Response({"detail": "Segment index out of range."}, status=status.HTTP_404_NOT_FOUND)

        claimed = request.headers.get(SHA256_HEADER, "").strip().lower()
        if not _SHA256_RE.match(claimed):
            return Response(
                {"detail": f"Send the segment's SHA-256 as a hex {SHA256_HEADER} header."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        body = request.body
        minimum, maximum = file.expected_segment_bounds(index)
        if not minimum <= len(body) <= maximum:
            return Response(
                {"detail": f"Segment {index} must be between {minimum} and {maximum} bytes, got {len(body)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actual = hashlib.sha256(body).hexdigest()
        if actual != claimed:
            return Response(
                {"detail": "SHA-256 mismatch: the segment was corrupted in transit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = Segment.objects.filter(file=file, index=index).first()
        if existing:
            return self._already_uploaded(existing, actual)

        content_id = get_segment_store().put(body)
        try:
            with transaction.atomic():
                segment = Segment.objects.create(
                    file=file, index=index, size=len(body), sha256=actual, content_id=content_id
                )
        except IntegrityError:
            # Another request stored this index at the same moment.
            return self._already_uploaded(Segment.objects.get(file=file, index=index), actual)

        return Response(SegmentSerializer(segment).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _already_uploaded(existing, sha256):
        if existing.sha256 == sha256:
            # Retries of the same bytes are fine; uploads can resume blindly.
            return Response(SegmentSerializer(existing).data, status=status.HTTP_200_OK)
        return Response(
            {"detail": "A different segment was already uploaded at this index."},
            status=status.HTTP_409_CONFLICT,
        )

    def get(self, request, file_id, index):
        file = _owned_file(request, file_id)
        segment = get_object_or_404(Segment, file=file, index=index)
        try:
            handle = get_segment_store().open(segment.content_id)
        except SegmentNotFound:
            logger.error("Segment %s is recorded but missing from the store", segment.content_id)
            return Response({"detail": "Segment is temporarily unavailable."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        response = FileResponse(handle, content_type="application/octet-stream")
        response[SHA256_HEADER] = segment.sha256
        response["Cache-Control"] = "private, no-store"
        response["Access-Control-Expose-Headers"] = SHA256_HEADER
        return response


class FileCompleteView(APIView):
    def post(self, request, file_id):
        file = _owned_file(request, file_id, with_segments=True)
        if file.status == File.Status.COMPLETE:
            return Response(FileManifestSerializer(file).data)

        manifest = FileManifestSerializer(file).data
        if manifest["missing_segments"]:
            return Response(
                {"detail": "Some segments are still missing.", "missing_segments": manifest["missing_segments"]},
                status=status.HTTP_409_CONFLICT,
            )

        File.objects.filter(pk=file.pk).update(status=File.Status.COMPLETE, completed_at=timezone.now())
        return Response(_manifest(file))
