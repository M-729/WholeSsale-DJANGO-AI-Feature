"""Models for the AI Financial Summary feature.

One model: a record of each attempt to call the AI provider. It exists for
two reasons — an audit trail of AI usage (who asked, when, whether it worked),
and a way to recognise "the same request again" so an accidental double-click
or page refresh does not spend a second AI call explaining figures that have
not changed (see ``views.find_reusable_result``).

Deliberately not stored here: the payload sent to the AI (only its hash — the
figures behind it are already reproducible on demand from ``apps.reports``,
so a second copy would only be one more place for them to go stale), any raw
AI response text (only the already-validated structured fields), and nothing
provider-related at all — no API key, no request id, no raw HTTP response.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class AISummaryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class AISummaryRequest(models.Model):
    """One attempt to generate an AI explanation of a financial period."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ai_summary_requests",
    )
    date_from = models.DateField()
    date_to = models.DateField()
    #: SHA-256 hex digest of the canonical JSON payload sent to the AI. Two
    #: requests for the same dates can still differ — the ledger moved
    #: between them — so the hash, not the date range alone, is what a later
    #: request has to match to be treated as "the same".
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(
        max_length=10, choices=AISummaryStatus.choices, default=AISummaryStatus.PENDING
    )
    #: The AI's validated structured response (summary/key_points/warnings/
    #: recommendations) — never the raw provider payload, and never set
    #: unless status is SUCCESS.
    response_data = models.JSONField(null=True, blank=True)
    #: The same safe, user-facing message the request flow returned. Never a
    #: raw provider error, stack trace, or credential.
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "ai_summary_request"
        ordering = ["-created_at", "-id"]
        indexes = [
            # What `find_reusable_result` looks up on every request, before
            # ever calling the AI provider.
            models.Index(
                fields=["user", "date_from", "date_to", "payload_hash"],
                name="ix_ai_summary_dedup",
            ),
        ]

    def __str__(self):
        return f"AI summary for {self.date_from}..{self.date_to} ({self.status})"
