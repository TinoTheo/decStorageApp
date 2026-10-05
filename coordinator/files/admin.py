from django.contrib import admin

from .models import File, Segment


class SegmentInline(admin.TabularInline):
    model = Segment
    fields = ("index", "size", "sha256", "content_id", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    # Names are encrypted, so the admin shows IDs and sizes only.
    list_display = ("id", "owner", "status", "segment_count", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("id", "owner__username")
    fields = ("id", "owner", "status", "segment_size", "segment_count", "created_at", "completed_at")
    readonly_fields = fields
    inlines = [SegmentInline]

    def has_add_permission(self, request):
        return False
