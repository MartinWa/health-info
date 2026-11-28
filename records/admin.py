"""Django admin holds the operator's accounts and token metadata, never health records."""

from django.contrib import admin

from records.models import ApiToken

admin.site.site_header = "healthinfo"
admin.site.site_title = "healthinfo"
admin.site.index_title = "Accounts"


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "prefix", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "user__username")
    readonly_fields = tuple(field.name for field in ApiToken._meta.fields)

    def has_add_permission(self, request):
        # Tokens are issued by the owner's own /tokens page, never created by hand.
        return False
