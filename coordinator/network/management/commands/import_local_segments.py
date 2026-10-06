"""
Moves segments staged on local disk (the Locked Box setup) into IPFS, so a
coordinator that switches from SEGMENT_STORE=local to SEGMENT_STORE=ipfs keeps
every existing file. Safe to run more than once.
"""

import hashlib

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from files.models import Segment
from network.models import Blob
from storage.backends import LocalSegmentStore, SegmentNotFound, SegmentStoreError, get_segment_store


class Command(BaseCommand):
    help = "Copy segments from the local staging folder into the gateway's IPFS node."

    def add_arguments(self, parser):
        parser.add_argument("--staging-dir", required=True, help="The old STAGING_DIR (local store folder).")

    def handle(self, *args, **options):
        store = get_segment_store()
        if not store.supports_network:
            raise CommandError("Set SEGMENT_STORE=ipfs first; this copies segments into IPFS.")
        local = LocalSegmentStore(options["staging_dir"])

        moved = skipped = missing = 0
        for blob in Blob.objects.live().order_by("pk"):
            if not local.holds(blob.content_id):
                skipped += 1  # already in IPFS, or never staged locally
                continue
            try:
                data = local.read(blob.content_id, max_bytes=settings.SEGMENT_SIZE + settings.GCM_TAG_BYTES)
            except SegmentNotFound:
                missing += 1
                continue
            if hashlib.sha256(data).hexdigest() != blob.sha256:
                self.stderr.write(f"Skipping {blob.content_id}: local copy is damaged.")
                missing += 1
                continue
            try:
                new_id = store.put(data)
            except SegmentStoreError as exc:
                raise CommandError(f"IPFS refused {blob.content_id}: {exc}") from exc

            with transaction.atomic():
                if new_id != blob.content_id:
                    Segment.objects.filter(content_id=blob.content_id).update(content_id=new_id)
                    Blob.objects.filter(pk=blob.pk).update(content_id=new_id, on_gateway=True)
                else:
                    Blob.objects.filter(pk=blob.pk).update(on_gateway=True)
            moved += 1

        self.stdout.write(f"Moved {moved} segment(s) into IPFS; {skipped} skipped; {missing} missing or damaged.")
        if moved:
            self.stdout.write("The local staging folder can be archived once `network_status` shows them replicated.")
