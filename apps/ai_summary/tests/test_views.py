"""The full request flow: auth, permission, dedup, rate limit, then the AI.

`apps.ai_summary.ai_gateway` is mocked throughout — no test here makes a real
network call. A couple of tests go one level deeper and mock `requests.post`
instead, specifically to prove a real timeout and a real API key survive the
whole gateway untouched by the time they reach the view.
"""

import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.ai_summary import ai_gateway
from apps.ai_summary.models import AISummaryRequest, AISummaryStatus
from apps.ai_summary.tests.test_services import PayloadFixture

VALID_AI_DATA = {
    "summary": "The business stayed profitable this period.",
    "key_points": ["Revenue was $1,200."],
    "warnings": [],
    "recommendations": [],
}


def _success(data=None):
    return ai_gateway.AIResult(success=True, data=data or VALID_AI_DATA)


def _failure(error):
    return ai_gateway.AIResult(success=False, error=error)


def _fake_groq_response(content=None):
    response = Mock()
    response.status_code = 200
    response.text = ""
    response.json.return_value = {
        "choices": [{"message": {"content": content or json.dumps(VALID_AI_DATA)}}]
    }
    return response


class GenerateViewFixture(PayloadFixture):
    """A ledger (from PayloadFixture) plus someone allowed to summarise it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.reader = User.objects.create_user(
            id=942_001,
            username="ais-reader",
            email="ais-reader@example.com",
            password="x-test-password",
        )
        cls.reader.user_permissions.add(
            Permission.objects.get(codename="view_financial_reports")
        )
        cls.outsider = User.objects.create_user(
            id=942_002,
            username="ais-outsider",
            email="ais-outsider@example.com",
            password="x-test-password",
        )

    def setUp(self):
        # LocMemCache is process-global and outlives any one test's DB
        # transaction, so the rate limiter has to be reset by hand.
        cache.clear()
        self.client.force_login(self.reader)
        self.url = reverse("ai_summary:generate")
        self.window = {
            "date_from": self.DATE_FROM.isoformat(),
            "date_to": self.DATE_TO.isoformat(),
        }

    def tearDown(self):
        cache.clear()

    def post_json(self, data):
        """Explicitly request the JSON contract, as an API/AJAX caller would.

        A plain browser form submission (no Accept header) now gets the HTML
        page instead — covered separately in test_page.py — so every test in
        this file that inspects `response.json()` has to ask for it.
        """
        return self.client.post(self.url, data, HTTP_ACCEPT="application/json")


class AccessTests(GenerateViewFixture, TestCase):
    def test_an_unauthenticated_user_is_rejected(self):
        self.client.logout()
        response = self.post_json(self.window)
        self.assertIn(response.status_code, (302, 403))

    def test_a_user_without_the_financial_reports_permission_is_rejected(self):
        self.client.force_login(self.outsider)
        response = self.post_json(self.window)
        self.assertEqual(response.status_code, 403)


class GenerateFlowTests(GenerateViewFixture, TestCase):
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_valid_request_generates_and_stores_a_summary(self, mock_request):
        mock_request.return_value = _success()

        response = self.post_json(self.window)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"], VALID_AI_DATA)
        self.assertFalse(body["reused"])
        mock_request.assert_called_once()

        row = AISummaryRequest.objects.get()
        self.assertEqual(row.user, self.reader)
        self.assertEqual(row.status, AISummaryStatus.SUCCESS)
        self.assertEqual(row.response_data, VALID_AI_DATA)
        self.assertEqual(row.error, "")

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_an_invalid_date_range_does_not_call_the_ai_provider(self, mock_request):
        response = self.post_json(
            {"date_from": self.DATE_TO.isoformat(), "date_to": self.DATE_FROM.isoformat()}
        )

        self.assertEqual(response.status_code, 400)
        mock_request.assert_not_called()
        self.assertFalse(AISummaryRequest.objects.exists())

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_missing_dates_are_rejected_before_any_report_is_built(self, mock_request):
        response = self.post_json({})

        self.assertEqual(response.status_code, 400)
        mock_request.assert_not_called()

    @override_settings(GROQ_API_KEY="")
    def test_a_missing_api_key_does_not_call_the_provider_and_is_stored_as_failed(self):
        with patch("apps.ai_summary.ai_gateway.requests.post") as post:
            response = self.post_json(self.window)
            post.assert_not_called()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], ai_gateway.ERROR_NOT_CONFIGURED)
        row = AISummaryRequest.objects.get()
        self.assertEqual(row.status, AISummaryStatus.FAILED)
        self.assertIsNone(row.response_data)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_failed_ai_result_is_stored_safely_and_never_looks_successful(
        self, mock_request
    ):
        mock_request.return_value = _failure(ai_gateway.ERROR_UNAVAILABLE)

        response = self.post_json(self.window)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], ai_gateway.ERROR_UNAVAILABLE)
        self.assertNotIn("data", response.json())

        row = AISummaryRequest.objects.get()
        self.assertEqual(row.status, AISummaryStatus.FAILED)
        self.assertNotEqual(row.status, AISummaryStatus.SUCCESS)
        self.assertIsNone(row.response_data)
        self.assertEqual(row.error, ai_gateway.ERROR_UNAVAILABLE)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_an_invalid_ai_response_is_handled_safely(self, mock_request):
        mock_request.return_value = _failure(ai_gateway.ERROR_INVALID_RESPONSE)

        response = self.post_json(self.window)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], ai_gateway.ERROR_INVALID_RESPONSE)
        self.assertEqual(AISummaryRequest.objects.get().status, AISummaryStatus.FAILED)

    @override_settings(GROQ_API_KEY="test-key")
    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_real_provider_timeout_is_handled_end_to_end(self, post):
        import requests

        post.side_effect = requests.Timeout("timed out")

        response = self.post_json(self.window)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], ai_gateway.ERROR_UNAVAILABLE)
        self.assertEqual(AISummaryRequest.objects.get().status, AISummaryStatus.FAILED)

    def test_an_unexpected_internal_error_returns_a_safe_generic_message(self):
        with patch(
            "apps.ai_summary.ai_gateway.request_ai_summary", side_effect=RuntimeError("boom")
        ):
            response = self.post_json(self.window)

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(
            body["error"],
            "Something went wrong while generating the summary. Please try again.",
        )
        self.assertNotIn("boom", json.dumps(body))
        self.assertNotIn("Traceback", json.dumps(body))


class RateLimitTests(GenerateViewFixture, TestCase):
    @override_settings(AI_SUMMARY_RATE_LIMIT_PER_HOUR=5)
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_sixth_request_within_the_window_is_blocked(self, mock_request):
        mock_request.return_value = _success()

        # Five distinct periods so each one actually reaches the provider —
        # an identical repeat would be served from the dedup cache instead
        # and would never touch the rate limiter at all.
        for offset in range(5):
            window = {
                "date_from": self.DATE_FROM.isoformat(),
                "date_to": (self.DATE_TO - timedelta(days=offset)).isoformat(),
            }
            response = self.post_json(window)
            self.assertEqual(response.status_code, 200, response.content)

        self.assertEqual(mock_request.call_count, 5)

        sixth = {
            "date_from": self.DATE_FROM.isoformat(),
            "date_to": (self.DATE_TO - timedelta(days=5)).isoformat(),
        }
        response = self.post_json(sixth)

        self.assertEqual(response.status_code, 429)
        # The blocked request never reached the provider.
        self.assertEqual(mock_request.call_count, 5)
        self.assertFalse(
            AISummaryRequest.objects.filter(date_to=self.DATE_TO - timedelta(days=5)).exists()
        )

    @override_settings(AI_SUMMARY_RATE_LIMIT_PER_HOUR=5)
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_the_rate_limit_is_tracked_per_user(self, mock_request):
        """Blocking one user must never block another (cache key is per-user)."""
        mock_request.return_value = _success()
        for offset in range(5):
            window = {
                "date_from": self.DATE_FROM.isoformat(),
                "date_to": (self.DATE_TO - timedelta(days=offset)).isoformat(),
            }
            self.post_json(window)

        other = get_user_model().objects.create_user(
            id=942_003,
            username="ais-reader-2",
            email="ais-reader-2@example.com",
            password="x-test-password",
        )
        other.user_permissions.add(Permission.objects.get(codename="view_financial_reports"))
        self.client.force_login(other)

        response = self.post_json(self.window)
        self.assertEqual(response.status_code, 200)


class DeduplicationTests(GenerateViewFixture, TestCase):
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_an_identical_recent_request_reuses_the_stored_result(self, mock_request):
        mock_request.return_value = _success()

        first = self.post_json(self.window)
        second = self.post_json(self.window)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["reused"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["reused"])
        self.assertEqual(second.json()["data"], VALID_AI_DATA)

        mock_request.assert_called_once()
        self.assertEqual(AISummaryRequest.objects.count(), 1)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_different_date_range_calls_the_provider_again(self, mock_request):
        mock_request.return_value = _success()

        self.post_json(self.window)
        other_window = {
            "date_from": self.DATE_FROM.isoformat(),
            "date_to": (self.DATE_TO - timedelta(days=1)).isoformat(),
        }
        response = self.post_json(other_window)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reused"])
        self.assertEqual(mock_request.call_count, 2)

    def test_a_different_payload_hash_for_the_same_range_calls_the_provider(self):
        """Dedup matches on the payload hash itself, not the date range alone."""
        AISummaryRequest.objects.create(
            user=self.reader,
            date_from=self.DATE_FROM,
            date_to=self.DATE_TO,
            payload_hash="0" * 64,  # deliberately not the real hash
            status=AISummaryStatus.SUCCESS,
            response_data={
                "summary": "stale",
                "key_points": [],
                "warnings": [],
                "recommendations": [],
            },
        )

        with patch("apps.ai_summary.ai_gateway.request_ai_summary") as mock_request:
            mock_request.return_value = _success()
            response = self.post_json(self.window)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reused"])
        mock_request.assert_called_once()

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_failed_prior_attempt_is_never_reused(self, mock_request):
        """Only a SUCCESS row is eligible for reuse — a failure never is."""
        mock_request.return_value = _failure(ai_gateway.ERROR_UNAVAILABLE)
        self.post_json(self.window)

        mock_request.return_value = _success()
        response = self.post_json(self.window)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reused"])
        self.assertEqual(mock_request.call_count, 2)


class SecurityTests(GenerateViewFixture, TestCase):
    @override_settings(GROQ_API_KEY="sk-super-secret-value")
    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_api_key_never_appears_in_the_response_or_the_stored_row(self, post):
        post.return_value = _fake_groq_response()

        response = self.post_json(self.window)

        self.assertNotIn(b"sk-super-secret-value", response.content)
        row = AISummaryRequest.objects.get()
        self.assertNotIn("sk-super-secret-value", json.dumps(row.response_data or {}))
        self.assertNotIn("sk-super-secret-value", row.error)
        # And the key was in fact sent to the provider — this is not passing
        # only because the key was never used.
        _, kwargs = post.call_args
        self.assertIn("sk-super-secret-value", kwargs["headers"]["Authorization"])
