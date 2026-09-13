"""The AI Financial Summary page: access, the browser form flow, and its
integration into the existing dashboard and navigation.

`apps.ai_summary.ai_gateway` is mocked throughout — no test here makes a real
network call. Unlike test_views.py, every request here is a plain browser
form submission (no Accept header), which is what actually reaches the view
through a real `<form>`.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.ai_summary import ai_gateway
from apps.ai_summary.tests.test_views import (
    GenerateViewFixture,
    _failure,
    _fake_groq_response,
    _success,
)


class PageAccessTests(GenerateViewFixture, TestCase):
    def test_an_authorized_user_can_access_the_page(self):
        response = self.client.get(reverse("ai_summary:summary"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Generate AI Financial Summary")

    def test_an_unauthorized_user_cannot_access_the_page(self):
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("ai_summary:summary"))

        self.assertEqual(response.status_code, 403)

    def test_an_anonymous_user_cannot_access_the_page(self):
        self.client.logout()

        response = self.client.get(reverse("ai_summary:summary"))

        self.assertIn(response.status_code, (302, 403))


class FormSubmissionTests(GenerateViewFixture, TestCase):
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_valid_submission_reaches_the_generation_flow_and_redirects(self, mock_request):
        mock_request.return_value = _success()

        response = self.client.post(self.url, self.window)

        self.assertEqual(response.status_code, 302)
        mock_request.assert_called_once()
        self.assertIn(f"date_from={self.DATE_FROM.isoformat()}", response.url)
        self.assertIn(f"date_to={self.DATE_TO.isoformat()}", response.url)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_successful_summary_is_displayed_after_the_redirect(self, mock_request):
        mock_request.return_value = _success()

        response = self.client.post(self.url, self.window, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The business stayed profitable this period.")
        self.assertContains(response, "Revenue was $1,200.")
        self.assertContains(response, "Financial Summary")
        # The trusted figures are shown alongside the AI's text, not instead.
        self.assertContains(response, "Trusted financial figures")

    def test_an_invalid_date_range_displays_a_validation_error(self):
        response = self.client.post(
            self.url,
            {"date_from": self.DATE_TO.isoformat(), "date_to": self.DATE_FROM.isoformat()},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "before its start", status_code=400)

    @override_settings(AI_SUMMARY_RATE_LIMIT_PER_HOUR=5)
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_a_rate_limited_request_displays_the_limit_message(self, mock_request):
        mock_request.return_value = _success()
        for offset in range(5):
            window = {
                "date_from": self.DATE_FROM.isoformat(),
                "date_to": (self.DATE_TO - timedelta(days=offset)).isoformat(),
            }
            self.client.post(self.url, window)

        sixth = {
            "date_from": self.DATE_FROM.isoformat(),
            "date_to": (self.DATE_TO - timedelta(days=5)).isoformat(),
        }
        response = self.client.post(self.url, sixth)

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "reached the limit", status_code=429)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_an_unavailable_ai_provider_shows_a_message_and_the_figures(self, mock_request):
        mock_request.return_value = _failure(ai_gateway.ERROR_UNAVAILABLE)

        response = self.client.post(self.url, self.window)

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, ai_gateway.ERROR_UNAVAILABLE, status_code=503)
        # The backend figures remain available even when the AI call failed.
        self.assertContains(response, "Trusted financial figures", status_code=503)

    @override_settings(GROQ_API_KEY="")
    def test_ai_not_configured_shows_an_admin_facing_message(self):
        response = self.client.post(self.url, self.window)

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "GROQ_API_KEY", status_code=503)

    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_ai_response_content_is_safely_escaped(self, mock_request):
        malicious = "<script>alert(1)</script>"
        mock_request.return_value = _success(
            {
                "summary": malicious,
                "key_points": [malicious],
                "warnings": [],
                "recommendations": [],
            }
        )

        response = self.client.post(self.url, self.window, follow=True)

        self.assertNotIn(b"<script>alert(1)</script>", response.content)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", response.content)

    @override_settings(GROQ_API_KEY="sk-super-secret-page-key")
    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_api_key_never_appears_in_rendered_html(self, post):
        post.return_value = _fake_groq_response()

        response = self.client.post(self.url, self.window, follow=True)

        self.assertNotIn(b"sk-super-secret-page-key", response.content)


class DashboardIntegrationTests(GenerateViewFixture, TestCase):
    @patch("apps.ai_summary.ai_gateway.request_ai_summary")
    def test_the_dashboard_card_is_shown_and_never_triggers_an_ai_request(self, mock_request):
        self.reader.user_permissions.add(Permission.objects.get(codename="view_company"))

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        mock_request.assert_not_called()
        self.assertContains(response, "AI Financial Summary")
        self.assertContains(response, "View Financial Summary")
        self.assertContains(response, reverse("ai_summary:summary"))


class NavigationTests(GenerateViewFixture, TestCase):
    def test_navigation_contains_the_ai_financial_summary_link(self):
        response = self.client.get(reverse("reports:trial_balance"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Financial Summary")
        self.assertContains(response, reverse("ai_summary:summary"))
