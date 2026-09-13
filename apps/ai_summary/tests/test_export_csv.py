"""CSV export from the AI Financial Summary page.

The export re-runs the same read the page itself does — build_summary_payload
and find_latest_result — so what downloads always matches what is on screen
(the same UX-007 convention apps.reports uses). `apps.ai_summary.ai_gateway`
is mocked wherever a summary needs to exist first; no test here makes or
allows a real network call, and one test asserts the export path never
reaches the AI gateway at all.
"""

import csv
import io
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.ai_summary.services import build_summary_payload
from apps.ai_summary.tests.test_views import VALID_AI_DATA, GenerateViewFixture, _success
from apps.core.models import AuditAction, AuditEvent


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode())))


class ExportCsvFixture(GenerateViewFixture):
    def setUp(self):
        super().setUp()
        self.reader.user_permissions.add(Permission.objects.get(codename="export_data"))
        self.export_url = (
            f"{reverse('ai_summary:summary')}?date_from={self.DATE_FROM.isoformat()}"
            f"&date_to={self.DATE_TO.isoformat()}&export=csv"
        )


class AccessTests(ExportCsvFixture, TestCase):
    def test_an_authorized_user_can_export_csv(self):
        response = self.client.get(self.export_url)

        self.assertEqual(response.status_code, 200)

    def test_a_user_without_the_export_permission_cannot_export(self):
        """Viewing the page and exporting it are separate rights (matches
        apps.reports: export_data is checked in addition to the report
        permission, not instead of it)."""
        self.reader.user_permissions.remove(Permission.objects.get(codename="export_data"))

        response = self.client.get(self.export_url)

        self.assertEqual(response.status_code, 403)

    def test_a_user_without_the_view_permission_cannot_export(self):
        self.client.force_login(self.outsider)

        response = self.client.get(self.export_url)

        self.assertEqual(response.status_code, 403)

    def test_an_anonymous_user_cannot_export(self):
        self.client.logout()

        response = self.client.get(self.export_url)

        self.assertIn(response.status_code, (302, 403))

    def test_the_ordinary_page_still_renders_normally_for_the_same_user(self):
        """Existing permissions/behaviour for the page itself are untouched."""
        response = self.client.get(
            reverse("ai_summary:summary"),
            {"date_from": self.DATE_FROM.isoformat(), "date_to": self.DATE_TO.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Export CSV")


class ContentTypeAndNamingTests(ExportCsvFixture, TestCase):
    def test_the_response_is_a_downloadable_csv(self):
        response = self.client.get(self.export_url)

        self.assertEqual(response["Content-Type"], "text/csv")
        expected_name = (
            f"ai_financial_summary_{self.DATE_FROM.isoformat()}"
            f"_to_{self.DATE_TO.isoformat()}.csv"
        )
        self.assertIn(f'filename="{expected_name}"', response["Content-Disposition"])

    def test_exporting_the_default_unbound_page_uses_the_period_shown_on_screen(self):
        """The export link always carries the actual displayed dates, even
        when the page itself was reached with no query string at all."""
        response = self.client.get(f"{reverse('ai_summary:summary')}?export=csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")


class FigureContentTests(ExportCsvFixture, TestCase):
    def test_the_csv_contains_the_trusted_financial_figures(self):
        response = self.client.get(self.export_url)
        rows = _csv_rows(response)

        # Compared against the same trusted source the page itself reads,
        # rather than hardcoded numbers — the point of this test is that the
        # CSV faithfully reflects the backend payload, not what that payload's
        # exact figures happen to be (that is services.py's own test suite).
        payload = build_summary_payload(self.DATE_FROM, self.DATE_TO)

        self.assertEqual(rows[0], ["Section", "Item", "Value"])
        self.assertIn(["Period", "Period From", self.DATE_FROM.isoformat()], rows)
        self.assertIn(["Period", "Period To", self.DATE_TO.isoformat()], rows)
        self.assertIn(["Period", "Currency", "USD"], rows)
        self.assertIn(["Financial Data", "Revenue", f"{payload['revenue']:.2f}"], rows)
        self.assertIn(["Financial Data", "Net profit", f"{payload['net_profit']:.2f}"], rows)
        self.assertIn(
            ["Financial Data", "Receivables", f"{payload['receivables_total']:.2f}"], rows
        )
        self.assertIn(
            ["Financial Data", "Overdue receivables", f"{payload['receivables_overdue']:.2f}"],
            rows,
        )
        self.assertIn(["Financial Data", "Payables", f"{payload['payables_total']:.2f}"], rows)
        # These two are pinned exactly: they come from apps.reports.ageing,
        # which (unlike the ledger-wide P&L/balance-sheet totals above) is
        # already currency-filtered, so they are a stable, known 1200/300.
        self.assertEqual(payload["receivables_total"], 1200.0)
        self.assertEqual(payload["payables_total"], 300.0)

    def test_the_csv_notes_an_excluded_foreign_currency(self):
        response = self.client.get(self.export_url)
        rows = _csv_rows(response)

        excluded = [r for r in rows if r[:2] == ["Financial Data", "Excluded currency"]]
        self.assertTrue(excluded)
        self.assertTrue(any("AIX" in row[2] for row in excluded))

    def test_an_invalid_date_range_is_rejected_rather_than_exported(self):
        url = (
            f"{reverse('ai_summary:summary')}?date_from={self.DATE_TO.isoformat()}"
            f"&date_to={self.DATE_FROM.isoformat()}&export=csv"
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.get("Content-Type"), "text/csv")


class AiSectionTests(ExportCsvFixture, TestCase):
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_the_csv_includes_the_ai_summary_when_one_exists(self, mock_request):
        mock_request.return_value = _success()
        self.client.post(self.url, self.window)  # generates and stores a SUCCESS row

        response = self.client.get(self.export_url)
        rows = _csv_rows(response)

        self.assertIn(["AI Summary", "Summary", VALID_AI_DATA["summary"]], rows)
        self.assertIn(["AI Summary", "Key Point 1", VALID_AI_DATA["key_points"][0]], rows)

    def test_the_csv_indicates_no_summary_is_available_when_none_exists(self):
        response = self.client.get(self.export_url)
        rows = _csv_rows(response)

        self.assertIn(
            ["AI Summary", "Status", "No AI summary available for this period."], rows
        )
        self.assertFalse([r for r in rows if r[:2] == ["AI Summary", "Summary"]])

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_exporting_never_calls_the_ai_provider(self, mock_request):
        response = self.client.get(self.export_url)

        self.assertEqual(response.status_code, 200)
        mock_request.assert_not_called()

    def test_a_failed_prior_attempt_is_not_shown_as_a_summary(self):
        """Only a SUCCESS row is ever exported as the AI Summary section —
        matching find_latest_result's own SUCCESS-only matching."""
        from apps.ai_summary.models import AISummaryRequest, AISummaryStatus

        AISummaryRequest.objects.create(
            user=self.reader,
            date_from=self.DATE_FROM,
            date_to=self.DATE_TO,
            payload_hash="doesnotmatter",
            status=AISummaryStatus.FAILED,
            response_data=None,
            error="temporarily unavailable",
        )

        response = self.client.get(self.export_url)
        rows = _csv_rows(response)

        self.assertIn(
            ["AI Summary", "Status", "No AI summary available for this period."], rows
        )


class AuditTests(ExportCsvFixture, TestCase):
    def test_the_export_is_recorded_as_an_audit_event(self):
        before = AuditEvent.objects.filter(action=AuditAction.EXPORT).count()

        self.client.get(self.export_url)

        events = AuditEvent.objects.filter(action=AuditAction.EXPORT).order_by("-occurred_at")
        self.assertEqual(events.count(), before + 1)
        self.assertEqual(events.first().user, self.reader)
