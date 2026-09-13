"""The AI provider boundary: request_ai_summary and validate_ai_response.

No test here makes a real network call. `requests.post` is mocked throughout,
and the module never falls back to a real request when a key happens to be
missing — a missing GROQ_API_KEY is exactly what one of the tests below
asserts short-circuits before `requests.post` is even reached.

The provider is Groq's OpenAI-compatible Chat Completions endpoint; several
tests below exist specifically to pin that down (the URL, the key used, the
model, the structured-output schema, the system prompt) so a future provider
swap has to touch this file on purpose rather than by accident.
"""

import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.ai_summary import ai_gateway

VALID_CONTENT = json.dumps(
    {
        "summary": "Revenue grew and the business stayed profitable this period.",
        "key_points": ["Revenue was $1,200.", "Net profit was $900."],
        "warnings": ["One invoice is overdue."],
        "recommendations": ["Follow up on the overdue invoice."],
    }
)


def _groq_response(*, status_code=200, content=VALID_CONTENT, text=""):
    response = Mock()
    response.status_code = status_code
    response.text = text or content
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


@override_settings(GROQ_API_KEY="test-key", AI_SUMMARY_MODEL="openai/gpt-oss-20b")
class RequestAiSummaryTests(SimpleTestCase):
    """Transport-level behaviour: what happens around the HTTP call itself."""

    @override_settings(GROQ_API_KEY="")
    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_missing_api_key_returns_a_safe_error_without_calling_the_provider(self, post):
        result = ai_gateway.request_ai_summary({"period": {"from": "2020-01-01"}})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_NOT_CONFIGURED)
        self.assertIsNone(result.data)
        post.assert_not_called()

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_successful_response_is_returned(self, post):
        post.return_value = _groq_response()

        result = ai_gateway.request_ai_summary({"period": {"from": "2020-01-01"}})

        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.data["key_points"], ["Revenue was $1,200.", "Net profit was $900."]
        )
        # The URL, key, and model actually used are Groq's and the configured
        # ones — not hardcoded, and not still pointed at OpenAI.
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["timeout"], ai_gateway.REQUEST_TIMEOUT_SECONDS)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_structured_output_schema_is_sent(self, post):
        post.return_value = _groq_response()

        ai_gateway.request_ai_summary({})

        _, kwargs = post.call_args
        response_format = kwargs["json"]["response_format"]
        self.assertEqual(response_format, ai_gateway.RESPONSE_FORMAT)
        self.assertEqual(response_format["type"], "json_schema")
        schema = response_format["json_schema"]["schema"]
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"summary", "key_points", "warnings", "recommendations"},
        )

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_safety_system_prompt_is_sent_unchanged(self, post):
        post.return_value = _groq_response()

        ai_gateway.request_ai_summary({})

        _, kwargs = post.call_args
        system_message = kwargs["json"]["messages"][0]
        self.assertEqual(system_message["role"], "system")
        self.assertEqual(system_message["content"], ai_gateway.SYSTEM_PROMPT)
        for instruction in (
            "Use only the financial information provided",
            "Never invent",
            "never claim to have performed accounting operations",
            "never recommend specific journal entries",
            "simple language",
            "period comparison",
            "insufficient",
            "Return ONLY the requested JSON structure",
        ):
            self.assertIn(instruction, ai_gateway.SYSTEM_PROMPT)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_system_prompt_asks_for_a_short_response(self, post):
        """Directly targets the reasoning model's tendency to run long."""
        post.return_value = _groq_response()

        ai_gateway.request_ai_summary({})

        self.assertIn("2-4 short sentences", ai_gateway.SYSTEM_PROMPT)
        self.assertIn("at most 3", ai_gateway.SYSTEM_PROMPT)
        self.assertIn("Do not provide lengthy reasoning", ai_gateway.SYSTEM_PROMPT)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_completion_token_budget_and_reasoning_params_are_sent(self, post):
        """A reasoning model spends tokens on hidden reasoning before the
        answer, which is what made 700 tokens too tight in practice."""
        post.return_value = _groq_response()

        ai_gateway.request_ai_summary({})

        _, kwargs = post.call_args
        body = kwargs["json"]
        self.assertEqual(body["max_tokens"], ai_gateway.MAX_COMPLETION_TOKENS)
        self.assertGreaterEqual(body["max_tokens"], 1200)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["reasoning_format"], "hidden")

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_timeout_is_handled_without_raising(self, post):
        import requests

        post.side_effect = requests.Timeout("timed out")

        result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_connection_failure_is_handled_without_raising(self, post):
        import requests

        post.side_effect = requests.ConnectionError("no route to host")

        result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_non_200_http_status_is_handled_without_raising(self, post):
        post.return_value = _groq_response(status_code=500, text="internal server error")

        result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_an_authentication_failure_is_logged_distinctly(self, post):
        """A bad/revoked key must be diagnosable server-side without a raw error."""
        post.return_value = _groq_response(status_code=401, text="Invalid API Key")

        with self.assertLogs("apps.ai_summary.ai_gateway", level="WARNING") as logs:
            result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)
        self.assertTrue(any("authentication failure" in line for line in logs.output))
        self.assertFalse(any("test-key" in line for line in logs.output))

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_quota_or_rate_limit_failure_is_logged_distinctly(self, post):
        post.return_value = _groq_response(status_code=429, text="rate limit exceeded")

        with self.assertLogs("apps.ai_summary.ai_gateway", level="WARNING") as logs:
            result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)
        self.assertTrue(any("quota/rate-limit failure" in line for line in logs.output))

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_bad_request_is_logged_distinctly(self, post):
        post.return_value = _groq_response(status_code=400, text="bad request")

        with self.assertLogs("apps.ai_summary.ai_gateway", level="WARNING") as logs:
            result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertTrue(any("bad request" in line for line in logs.output))

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_json_validate_failed_error_is_logged_distinctly(self, post):
        """The exact failure Groq reports when a reasoning model runs out of
        completion budget mid-reasoning and never emits the document."""
        body_text = (
            '{"error": {"message": "Failed to generate JSON. Please adjust '
            'your prompt.", "type": "invalid_request_error", '
            '"code": "json_validate_failed", "failed_generation": '
            '"max completion tokens reached before generating a valid document"}}'
        )
        response = Mock()
        response.status_code = 400
        response.text = body_text
        response.json.return_value = {
            "error": {
                "message": "Failed to generate JSON. Please adjust your prompt.",
                "type": "invalid_request_error",
                "code": "json_validate_failed",
                "failed_generation": (
                    "max completion tokens reached before generating a valid document"
                ),
            }
        }
        post.return_value = response

        with self.assertLogs("apps.ai_summary.ai_gateway", level="WARNING") as logs:
            result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_UNAVAILABLE)
        self.assertTrue(any("json_validate_failed" in line for line in logs.output))
        # The raw failure detail is logged server-side, never returned.
        self.assertNotIn("failed_generation", result.error)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_a_provider_outage_is_logged_distinctly(self, post):
        post.return_value = _groq_response(status_code=503, text="service unavailable")

        with self.assertLogs("apps.ai_summary.ai_gateway", level="WARNING") as logs:
            result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertTrue(any("provider unavailable" in line for line in logs.output))

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_an_unexpected_response_shape_is_handled_without_raising(self, post):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"unexpected": "shape"}
        post.return_value = response

        result = ai_gateway.request_ai_summary({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_provider_never_sees_more_than_the_given_payload(self, post):
        """request_ai_summary serialises exactly what it is handed, nothing else."""
        post.return_value = _groq_response()
        payload = {"period": {"from": "2020-01-01", "to": "2020-01-31"}, "revenue": 100.0}

        ai_gateway.request_ai_summary(payload)

        _, kwargs = post.call_args
        user_message = kwargs["json"]["messages"][1]["content"]
        self.assertEqual(json.loads(user_message), payload)

    @patch("apps.ai_summary.ai_gateway.requests.post")
    def test_the_api_key_never_appears_in_the_returned_result(self, post):
        post.return_value = _groq_response(status_code=401, text="Invalid API Key: test-key")

        result = ai_gateway.request_ai_summary({})

        self.assertNotIn("test-key", result.error or "")
        self.assertIsNone(result.data)


class ValidateAiResponseTests(SimpleTestCase):
    """Content-level behaviour: what shape a response has to have to be trusted."""

    def test_a_well_formed_response_is_accepted_and_normalised(self):
        result = ai_gateway.validate_ai_response(VALID_CONTENT)

        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertEqual(
            set(result.data), {"summary", "key_points", "warnings", "recommendations"}
        )
        self.assertEqual(result.data["summary"], json.loads(VALID_CONTENT)["summary"])

    def test_an_empty_response_is_rejected(self):
        result = ai_gateway.validate_ai_response("")

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_malformed_json_is_rejected(self):
        result = ai_gateway.validate_ai_response("{not valid json")

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_a_json_array_instead_of_an_object_is_rejected(self):
        """Guards against a technically-valid JSON payload of the wrong shape."""
        result = ai_gateway.validate_ai_response(json.dumps(["summary", "warnings"]))

        self.assertFalse(result.success)

    def test_a_missing_required_field_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        del payload["warnings"]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_an_unexpected_extra_field_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["extra"] = "not part of the schema"

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)

    def test_a_wrong_field_type_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["summary"] = 12345  # should be a string

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_a_non_string_item_in_a_list_is_rejected(self):
        """Guards against smuggled structured content inside a list field."""
        payload = json.loads(VALID_CONTENT)
        payload["key_points"] = [{"nested": "object"}]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)

    def test_too_many_list_items_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["recommendations"] = [
            f"Point {n}" for n in range(ai_gateway.MAX_LIST_ITEMS + 1)
        ]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_the_maximum_number_of_list_items_is_accepted(self):
        payload = json.loads(VALID_CONTENT)
        payload["recommendations"] = [f"Point {n}" for n in range(ai_gateway.MAX_LIST_ITEMS)]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertTrue(result.success)

    def test_excessively_long_summary_text_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["summary"] = "x" * (ai_gateway.SUMMARY_MAX_LENGTH + 1)

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)

    def test_excessively_long_list_item_text_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["warnings"] = ["x" * (ai_gateway.ITEM_MAX_LENGTH + 1)]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)

    def test_an_empty_string_item_is_rejected(self):
        payload = json.loads(VALID_CONTENT)
        payload["warnings"] = ["   "]

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertFalse(result.success)

    def test_empty_lists_are_accepted(self):
        """Nothing to warn about is a valid, common answer, not an error."""
        payload = json.loads(VALID_CONTENT)
        payload["warnings"] = []

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertTrue(result.success)
        self.assertEqual(result.data["warnings"], [])

    def test_a_short_concise_summary_is_accepted(self):
        """What the new, brevity-focused system prompt actually asks for."""
        payload = json.loads(VALID_CONTENT)
        payload["summary"] = "Revenue rose and the business stayed profitable."
        payload["key_points"] = ["Revenue was $1,200."]
        payload["warnings"] = []
        payload["recommendations"] = []

        result = ai_gateway.validate_ai_response(json.dumps(payload))

        self.assertTrue(result.success)
        self.assertEqual(result.data["summary"], payload["summary"])

    def test_a_response_wrapped_in_a_json_markdown_fence_is_accepted(self):
        """A real-world quirk of open-weight models even in structured mode."""
        fenced = f"```json\n{VALID_CONTENT}\n```"

        result = ai_gateway.validate_ai_response(fenced)

        self.assertTrue(result.success)
        self.assertEqual(result.data["summary"], json.loads(VALID_CONTENT)["summary"])

    def test_a_response_wrapped_in_a_plain_fence_is_accepted(self):
        fenced = f"```\n{VALID_CONTENT}\n```"

        result = ai_gateway.validate_ai_response(fenced)

        self.assertTrue(result.success)

    def test_fence_stripping_does_not_mask_genuinely_invalid_json(self):
        result = ai_gateway.validate_ai_response("```json\nnot valid json\n```")

        self.assertFalse(result.success)
        self.assertEqual(result.error, ai_gateway.ERROR_INVALID_RESPONSE)
