"""The only module in this project that talks to an AI provider over the network.

The provider is Groq, called through its OpenAI-compatible Chat Completions
endpoint — same request/response shape as OpenAI, just a different host and
API key, which is why switching providers here changed nothing about the
payload, the prompt, the structured-output schema, or ``validate_ai_response``.

Mirrors ``apps.payments.stripe_gateway``: one module owns the network call and
translates the provider's shape into something the rest of the app can trust,
so nothing above this file ever touches a raw HTTP response or a raw API key.

Two things this module is deliberately not allowed to do, because the whole
feature depends on it:

* It never computes a financial figure. ``apps.ai_summary.services`` hands it
  a payload that is already the backend's own numbers; this module only asks
  an AI model to put those numbers into words and checks that the words it
  gets back are shaped the way they were asked to be.
* It never lets a provider failure become an unhandled exception. Every public
  function here returns an :class:`AIResult` — a request that could not be
  made, timed out, came back malformed, or failed validation all look the same
  to the caller: ``success=False`` and a short, safe ``error`` message, with
  the technical detail logged server-side instead.
"""

import json
import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: Long enough for a short chat completion, short enough that a hung request
#: never leaves a report page waiting indefinitely.
REQUEST_TIMEOUT_SECONDS = 15

CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"

#: openai/gpt-oss-20b is a reasoning model: it spends completion tokens on an
#: internal reasoning pass before it ever writes the requested JSON, and those
#: reasoning tokens count against the same budget as the answer. 700 was tight
#: enough that Groq sometimes hit the ceiling mid-reasoning and never emitted
#: a valid document at all ("max completion tokens reached before generating
#: a valid document") — this raises the ceiling and asks the model to spend as
#: little of it as possible on reasoning, since none of that reasoning is ever
#: shown to a user.
MAX_COMPLETION_TOKENS = 1200
REASONING_EFFORT = "low"
#: Keeps the model's reasoning out of `message.content` entirely, so content
#: is always just the JSON document asked for — never the JSON preceded or
#: interleaved with chain-of-thought text.
REASONING_FORMAT = "hidden"

#: Keeps the response short and its cost predictable regardless of what the
#: model is tempted to write.
SUMMARY_MAX_LENGTH = 600
ITEM_MAX_LENGTH = 200
MAX_LIST_ITEMS = 5

REQUIRED_FIELDS = ("summary", "key_points", "warnings", "recommendations")
LIST_FIELDS = ("key_points", "warnings", "recommendations")

# Messages safe to show a user. The real cause — a timeout, a 500, a bad key —
# is logged, never returned, so nothing about the provider ever reaches the UI.
ERROR_NOT_CONFIGURED = "The AI financial summary is not configured."
ERROR_UNAVAILABLE = (
    "The AI financial summary is temporarily unavailable. Please try again shortly."
)
ERROR_INVALID_RESPONSE = (
    "The AI could not produce a valid summary this time. Please try again."
)

SYSTEM_PROMPT = (
    "You are a financial explainer for a business accounting system.\n"
    "Use only the financial information provided in the input. Never invent "
    "figures or facts, never claim to have performed accounting operations, "
    "and never recommend specific journal entries or accounting transactions.\n"
    "Explain the financial situation in simple language for a business owner "
    "who is not an accountant. Use the supplied period comparison when it is "
    "useful, and say plainly if the data is insufficient to draw a conclusion.\n"
    "Keep the summary to 2-4 short sentences. Keep each list to at most 3 "
    "concise items. Do not provide lengthy reasoning.\n"
    "Return ONLY the requested JSON structure."
)

#: Restricted to the subset of JSON Schema that structured-output APIs
#: reliably enforce (object/array/string types, `required`,
#: `additionalProperties: false`). Length and count limits are not encoded
#: here — they are enforced by `validate_ai_response` instead, so this feature
#: is not depending on a provider correctly honouring a keyword it might
#: silently ignore.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "financial_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_points": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
            "required": list(REQUIRED_FIELDS),
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True, slots=True)
class AIResult:
    """What every function in this module returns — never an exception."""

    success: bool
    data: dict | None = None
    error: str | None = None


def is_enabled() -> bool:
    """Read the key itself rather than a cached flag, so there is one answer."""
    return bool(settings.GROQ_API_KEY)


#: HTTP statuses worth telling apart in the logs. Anything not listed here
#: falls back to a generic "provider unavailable" — the point is distinguishing
#: an operator problem (bad key, no quota) from a transient one worth just
#: retrying, not building an exhaustive map of Groq's error codes.
_STATUS_CATEGORIES = {
    400: "bad request",
    401: "authentication failure",
    403: "authentication failure",
    404: "bad request",
    422: "bad request",
    429: "quota/rate-limit failure",
}


def _status_category(status_code: int) -> str:
    if status_code in _STATUS_CATEGORIES:
        return _STATUS_CATEGORIES[status_code]
    if status_code >= 500:
        return "provider unavailable"
    return "unexpected response"


def _groq_error_code(response) -> str | None:
    """The provider's own error code, when the error body is JSON shaped.

    Best-effort only: a body that is not JSON, or JSON without this shape,
    just yields ``None`` and the caller falls back to the plain HTTP-status
    category.
    """
    try:
        return response.json().get("error", {}).get("code")
    except (ValueError, AttributeError):
        return None


def request_ai_summary(payload: dict) -> AIResult:
    """Ask the configured AI model to explain an already-aggregated payload.

    ``payload`` is expected to be exactly what
    ``apps.ai_summary.services.build_summary_payload`` produces: aggregated
    totals only, no customer, vendor, product or document-level data. This
    function does not know or care how it was built — it only serialises it
    into the request and never writes it anywhere.
    """
    if not is_enabled():
        logger.warning("AI summary requested but GROQ_API_KEY is not set.")
        return AIResult(success=False, error=ERROR_NOT_CONFIGURED)

    body = {
        "model": settings.AI_SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        "response_format": RESPONSE_FORMAT,
        "temperature": 0.2,
        "max_tokens": MAX_COMPLETION_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        "reasoning_format": REASONING_FORMAT,
    }

    try:
        response = requests.post(
            CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        logger.warning("AI summary request timed out: %s", exc)
        return AIResult(success=False, error=ERROR_UNAVAILABLE)
    except requests.RequestException as exc:
        # Covers connection failures, DNS errors, TLS problems, and anything
        # else requests raises that is not a timeout. Never logs the request
        # itself, so the Authorization header (and the key in it) never
        # reaches the log even indirectly through an exception's str().
        logger.warning("AI summary request failed (connection error): %s", exc)
        return AIResult(success=False, error=ERROR_UNAVAILABLE)

    if response.status_code != 200:
        # The category is what an operator actually needs at a glance — an
        # auth failure and a transient 503 call for different fixes, even
        # though both look identical to the end user. json_validate_failed is
        # called out by name because it means something specific for a
        # reasoning model: it ran out of completion budget before finishing
        # the document, which points at MAX_COMPLETION_TOKENS rather than at
        # a credentials or connectivity problem.
        error_code = _groq_error_code(response)
        category = (
            "structured output generation failed (json_validate_failed)"
            if error_code == "json_validate_failed"
            else _status_category(response.status_code)
        )
        logger.warning(
            "AI summary request failed (%s, HTTP %s): %s",
            category,
            response.status_code,
            response.text[:500],
        )
        return AIResult(success=False, error=ERROR_UNAVAILABLE)

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("AI summary response had an unexpected shape: %s", exc)
        return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

    return validate_ai_response(content)


def _strip_markdown_fence(text: str) -> str:
    """Unwrap a ```json fenced code block, if the whole response is one.

    Structured-output mode is supposed to make this unnecessary, but an
    open-weight reasoning model occasionally wraps its JSON in a markdown
    fence out of habit even so. This does not relax what counts as valid —
    a response that is not, in full, a single fenced block is returned
    untouched and still has to be valid JSON on its own; this only removes
    wrapping that is not part of the JSON itself.
    """
    stripped = text.strip()
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return stripped
    inner = stripped[3:-3].strip()
    first_line, _, rest = inner.partition("\n")
    if first_line.strip().isalpha():  # a language tag, e.g. ```json
        return rest.strip()
    return inner


def validate_ai_response(raw_text: str) -> AIResult:
    """Confirm the model's output is exactly the small, plain shape asked for.

    Deliberately does not try to prove that every number in the text traces
    back to the payload — that is a job for fragile natural-language parsing,
    not a validator. Instead it enforces the one thing that is checkable
    mechanically: the response is valid JSON, has exactly the expected fields,
    each field has the expected type, and nothing is unbounded in length or
    count. Keeping the AI from inventing figures is the prompt's job (and the
    fact that the backend, not the AI, is what everything else in this system
    reads); this function's job is only to keep a malformed or oversized
    response from ever reaching a user.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        logger.warning("AI summary response was empty.")
        return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

    try:
        parsed = json.loads(_strip_markdown_fence(raw_text))
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("AI summary response was not valid JSON: %s", exc)
        return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

    if not isinstance(parsed, dict) or set(parsed) != set(REQUIRED_FIELDS):
        logger.warning("AI summary response had unexpected fields: %r", parsed)
        return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

    summary = parsed["summary"]
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > SUMMARY_MAX_LENGTH
    ):
        logger.warning("AI summary response had an invalid 'summary' field.")
        return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

    cleaned = {"summary": summary.strip()}
    for field in LIST_FIELDS:
        items = parsed[field]
        if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
            logger.warning("AI summary response had an invalid '%s' field.", field)
            return AIResult(success=False, error=ERROR_INVALID_RESPONSE)

        checked = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or len(item) > ITEM_MAX_LENGTH:
                logger.warning("AI summary response had an invalid item in '%s'.", field)
                return AIResult(success=False, error=ERROR_INVALID_RESPONSE)
            checked.append(item.strip())
        cleaned[field] = checked

    return AIResult(success=True, data=cleaned)
