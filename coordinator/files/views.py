"""
File and segment endpoints.

The browser creates a file record, PUTs each encrypted segment (in parallel, in
any order, retrying freely), then marks the file complete. Every segment is
checked against the SHA-256 the browser sends, so corruption in transit is caught
before anything is stored. Downloads are checked against the same SHA-256 before
they leave the coordinator, so a damaged copy is refused rather than served.
"""

import hashlib
import logging
import re

from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from network.models import Blob
from network.services import copies_by_content_id, delete_file, register_blob
from storage.backends import SegmentStoreError, get_segment_store

from .models import File, Segment
from .serializers import FileCreateSerializer, FileManifestSerializer, FileSummarySerializer, SegmentSerializer

logger = logging.getLogger(__name__)

SHA256_HEADER = "X-Content-SHA256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

UNAVAILABLE = {"detail": "Segment is temporarily unavailable. Try again shortly."}


def _owned_file(request, file_id, *, with_segments=False):
    queryset = File.objects.filter(owner=request.user)
    if with_segments:
        queryset = queryset.prefetch_related(Prefetch("segments", queryset=Segment.objects.order_by("index")))
    return get_object_or_404(queryset, id=file_id)


def copies_for(files):
    """
    The number of confirmed copies of each file: the lowest count across its
    segments, since a file is only as safe as its least-copied piece. None when
    the coordinator isn't on a storage network (local development).
    """
    if not get_segment_store().supports_network:
        return {f.id: None for f in files}
    pairs = list(Segment.objects.filter(file__in=files).values_list("file_id", "content_id"))
    counts = copies_by_content_id({cid for _, cid in pairs})
    result = {}
    for file_id, content_id in pairs:
        copies = counts.get(content_id, 0)
        result[file_id] = min(result.get(file_id, copies), copies)
    return {f.id: result.get(f.id, 0) for f in files}


def _manifest(file):
    file = File.objects.prefetch_related(Prefetch("segments", queryset=Segment.objects.order_by("index"))).get(
        pk=file.pk
    )
    return FileManifestSerializer(file, context={"copies": copies_for([file])}).data


class FileListCreateView(APIView):
    def get(self, request):
        files = list(
            File.objects.filter(owner=request.user).annotate(
                stored_bytes=Sum("segments__size"), stored_segments=Count("segments")
            )
        )
        return Response(FileSummarySerializer(files, many=True, context={"copies": copies_for(files)}).data)

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
        file = _owned_file(request, file_id)
        return Response(_manifest(file))

    def delete(self, request, file_id):
        # Every node holding a copy is told to drop it, and the gateway drops its own.
        delete_file(_owned_file(request, file_id))
        return Response(status=status.HTTP_204_NO_CONTENT)


def _release_quietly(content_id):
    try:
        get_segment_store().release(content_id)
    except SegmentStoreError:
        logger.warning("Couldn't release unused segment %s from the gateway", content_id, exc_info=True)


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

        try:
            content_id = get_segment_store().put(body)
        except SegmentStoreError:
            logger.exception("Could not store segment %s of %s", index, file.id)
            return Response(
                {"detail": "Storage is temporarily unavailable. Try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            with transaction.atomic():
                segment = Segment.objects.create(
                    file=file, index=index, size=len(body), sha256=actual, content_id=content_id
                )
                register_blob(content_id, len(body), actual)
        except IntegrityError:
            # Another request stored this index at the same moment.
            existing = Segment.objects.get(file=file, index=index)
            if existing.content_id != content_id and not Blob.objects.filter(content_id=content_id).exists():
                _release_quietly(content_id)
            return self._already_uploaded(existing, actual)

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
            data = get_segment_store().read(segment.content_id, max_bytes=segment.size)
        except SegmentStoreError:
            logger.error("Segment %s couldn't be fetched", segment.content_id, exc_info=True)
            return Response(UNAVAILABLE, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if len(data) != segment.size or hashlib.sha256(data).hexdigest() != segment.sha256:
            # Never hand out bytes that don't match what was uploaded.
            logger.error("Segment %s failed its integrity check on the way out", segment.content_id)
            return Response(UNAVAILABLE, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        response = HttpResponse(data, content_type="application/octet-stream")
        response[SHA256_HEADER] = segment.sha256
        response["Cache-Control"] = "private, no-store"
        response["Access-Control-Expose-Headers"] = SHA256_HEADER
        return response


class FileCompleteView(APIView):
    def post(self, request, file_id):
        file = _owned_file(request, file_id)
        if file.status == File.Status.COMPLETE:
            return Response(_manifest(file))

        manifest = _manifest(file)
        if manifest["missing_segments"]:
            return Response(
                {"detail": "Some segments are still missing.", "missing_segments": manifest["missing_segments"]},
                status=status.HTTP_409_CONFLICT,
            )

        File.objects.filter(pk=file.pk).update(status=File.Status.COMPLETE, completed_at=timezone.now())
        return Response(_manifest(file))
