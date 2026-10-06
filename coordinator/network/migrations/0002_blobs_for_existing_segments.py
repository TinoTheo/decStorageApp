"""
Segments uploaded before Many Homes have no Blob row yet. Create one for each,
marked as held by the gateway, so they're treated like any new upload: placed
on nodes once the coordinator runs with the IPFS store.

Segments staged on local disk aren't in IPFS, so on a coordinator that moves
from the local store to IPFS they have to be re-added first; see
`manage.py import_local_segments` (docs/operations.md).
"""

from django.db import migrations


def create_blobs(apps, schema_editor):
    Segment = apps.get_model("files", "Segment")
    Blob = apps.get_model("network", "Blob")
    existing = set(Blob.objects.values_list("content_id", flat=True))
    new = {}
    for content_id, size, sha256 in Segment.objects.values_list("content_id", "size", "sha256"):
        if content_id not in existing and content_id not in new:
            new[content_id] = Blob(content_id=content_id, size=size, sha256=sha256, on_gateway=True)
    Blob.objects.bulk_create(new.values(), batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("network", "0001_initial"),
        ("files", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_blobs, migrations.RunPython.noop)]
