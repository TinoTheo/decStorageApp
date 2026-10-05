from django.contrib import admin

from .models import AuthToken, KeyBundle


@admin.register(KeyBundle)
class KeyBundleAdmin(admin.ModelAdmin):
    list_display = ("user", "kdf", "kdf_iterations", "created_at", "updated_at")
    search_fields = ("user__username",)
    # Wrapped keys are useless without the user's passphrase, but there's no reason
    # to show or edit them in the admin either.
    fields = ("user", "kdf", "kdf_iterations", "created_at", "updated_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "last_used_at", "expires_at")
    search_fields = ("user__username",)
    fields = ("user", "created_at", "last_used_at", "expires_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False
