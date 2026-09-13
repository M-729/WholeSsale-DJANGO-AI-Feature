"""Admin registration for apps.ai_summary.

Mirrors AuditEventAdmin (apps/core/admin.py): a record of an AI request is an
audit trail, so it is readable here and nowhere editable or deletable through
the application.
"""

from django.contrib import admin

from apps.ai_summary.models import AISummaryRequest


@admin.register(AISummaryRequest)
class AISummaryRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "date_from", "date_to", "status")
    list_filter = ("status", "created_at")
    search_fields = ("user__username", "user__email", "payload_hash")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AISummaryRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
