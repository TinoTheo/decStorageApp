from django.conf import settings
from django.db import models


class File(models.Model):
    """
    One uploaded file. The ID is generated in the browser before encryption so it
    can be bound into every ciphertext (see docs/design.md, "What the AAD binds").
    Name and file key arrive already encrypted; the server can't read either.
    """

    class Status(models.TextChoices):
        UPLOADING = "uploading"
        COMPLETE = "complete"

    id = models.UUIDField(primary_key=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="files")
    encrypted_name = models.TextField()
    wrapped_file_key = models.CharField(max_length=128)
    segment_size = models.PositiveIntegerField()
    segment_count = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADING)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"])]

    def __str__(self):
        return str(self.id)

    def plaintext_size(self, segments=None):
        """
        Size of the original file. Not a secret: it follows directly from the
        ciphertext lengths the server already holds.
        """
        segments = segments if segments is not None else self.segments.all()
        total = sum(s.size for s in segments)
        return max(total - settings.GCM_TAG_BYTES * len(segments), 0)

    def expected_segment_bounds(self, index):
        """Allowed ciphertext size range for the segment at `index`."""
        tag = settings.GCM_TAG_BYTES
        full = self.segment_size + tag
        if index < self.segment_count - 1:
            return full, full
        # The last segment carries 1..segment_size bytes, or 0 for an empty file.
        minimum = tag if self.segment_count == 1 else tag + 1
        return minimum, full


class Segment(models.Model):
    """
    One encrypted segment. `content_id` is whatever the active segment store
    returned (a CID); `sha256` is the coordinator's own integrity check.
    In M2 a Replica table records which storage nodes hold each segment.
    """

    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="segments")
    index = models.PositiveIntegerField()
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    content_id = models.CharField(max_length=128, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["index"]
        constraints = [models.UniqueConstraint(fields=["file", "index"], name="unique_segment_per_file")]

    def __str__(self):
        return f"{self.file_id}#{self.index}"
