from django.conf import settings
from django.contrib import admin
from django.db.models import Count, Q

from . import services
from .models import Blob, Node, NodeInvite, Operator, Replica


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class NodeInline(admin.TabularInline):
    model = Node
    fields = ("name", "status", "last_seen_at", "used_bytes", "capacity_bytes")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True


@admin.register(Operator)
class OperatorAdmin(admin.ModelAdmin):
    list_display = ("name", "contact", "is_active", "node_count", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "contact")
    inlines = [NodeInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_nodes=Count("nodes"))

    @admin.display(description="Nodes", ordering="_nodes")
    def node_count(self, obj):
        return obj._nodes


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ("name", "operator", "status", "online", "used", "stored_copies", "last_seen_at")
    list_filter = ("status", "operator")
    search_fields = ("name", "peer_id", "operator__name")
    readonly_fields = (
        "operator",
        "peer_id",
        "registered_at",
        "last_seen_at",
        "capacity_bytes",
        "used_bytes",
        "pinned_count",
        "addresses",
        "agent_version",
        "kubo_version",
    )
    fields = ("name", "status", "status_reason") + readonly_fields
    actions = ["disable_nodes", "enable_nodes"]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("operator")
            .annotate(_stored=Count("replicas", filter=Q(replicas__state=Replica.State.STORED)))
        )

    def has_add_permission(self, request):
        return False  # nodes join with an invite: `manage.py invite_node`

    @admin.display(boolean=True)
    def online(self, obj):
        return obj.is_online

    @admin.display(description="Used / capacity")
    def used(self, obj):
        return f"{_human(obj.used_bytes)} / {_human(obj.capacity_bytes)}"

    @admin.display(description="Stored copies", ordering="_stored")
    def stored_copies(self, obj):
        return obj._stored

    @admin.action(description="Disable (no new copies; existing copies keep counting)")
    def disable_nodes(self, request, queryset):
        services.disable_nodes(list(queryset), "Disabled by an admin.")

    @admin.action(description="Re-enable")
    def enable_nodes(self, request, queryset):
        queryset.update(status=Node.Status.ACTIVE, status_reason="")


@admin.register(NodeInvite)
class NodeInviteAdmin(admin.ModelAdmin):
    list_display = ("operator", "node_name", "created_at", "expires_at", "used_at")
    fields = list_display
    readonly_fields = list_display

    def has_add_permission(self, request):
        return False  # codes are only ever shown once: `manage.py invite_node`


class ReplicaInline(admin.TabularInline):
    model = Replica
    fields = ("node", "state", "attempts", "last_error", "assigned_at", "stored_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Blob)
class BlobAdmin(admin.ModelAdmin):
    list_display = ("content_id", "size", "copies", "on_gateway", "created_at", "deleted_at")
    list_filter = ("on_gateway",)
    search_fields = ("content_id",)
    readonly_fields = ("content_id", "size", "sha256", "created_at", "on_gateway", "released_from_gateway_at", "deleted_at")
    fields = readonly_fields
    inlines = [ReplicaInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_copy_counts()

    def has_add_permission(self, request):
        return False

    @admin.display(description="Confirmed copies", ordering="stored_copies")
    def copies(self, obj):
        return f"{obj.stored_copies} / {settings.REPLICA_COUNT}"
