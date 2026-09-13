"""The request flow: turns a POST into a validated, rate-limited AI summary,
and the page that shows the result.

This module owns none of the two things it coordinates — it never computes a
financial figure (that is ``apps.ai_summary.services``) and it never talks to
the AI provider itself (that is ``apps.ai_summary.ai_gateway``). What it adds
is everything that only makes sense once a real user, over HTTP, is asking for
one: authentication, the same permission the report pages already use,
deduplication, a rate limit, an audit row, and a page shaped so nothing about
the provider, the database, or an unexpected crash ever reaches the client.

Two views, two HTTP methods, on purpose:

* ``AISummaryPageView`` (GET) only ever reads: it recomputes the trusted
  figures — a local, side-effect-free accounting calculation — and shows a
  previously generated explanation if the figures have not changed since. It
  never calls the AI provider and never writes a row, so visiting or
  refreshing this page can never itself spend an AI request.
* ``GenerateAISummaryView`` (POST) is the only place that may call the AI
  provider and the only place that writes an ``AISummaryRequest`` row.
"""

import csv
import hashlib
import json
import logging
from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.ai_summary import ai_gateway
from apps.ai_summary.models import AISummaryRequest, AISummaryStatus
from apps.ai_summary.services import build_summary_payload
from apps.core import audit
from apps.core.mixins import ActionPermissionMixin
from apps.core.permissions import EXPORT_DATA, VIEW_FINANCIAL_REPORTS
from apps.reports.forms import DateRangeForm

logger = logging.getLogger(__name__)

TEMPLATE_NAME = "ai_summary/summary.html"

RATE_LIMIT_WINDOW_SECONDS = 3600
RATE_LIMIT_CACHE_KEY = "ai_summary_rl:{user_id}"

# Shown to the user. What actually went wrong is logged, never returned.
RATE_LIMIT_MESSAGE = (
    "You've reached the limit for AI summary requests this hour. Please try again later."
)
GENERIC_ERROR_MESSAGE = "Something went wrong while generating the summary. Please try again."
#: Shown only on the HTML page — an internal/admin-facing message, since a
#: business-owner user cannot act on it. The JSON contract keeps the plain
#: ai_gateway.ERROR_NOT_CONFIGURED string instead.
NOT_CONFIGURED_ADMIN_MESSAGE = (
    "The AI financial summary is not configured on this server. An administrator "
    "needs to set GROQ_API_KEY before this feature is available."
)

#: (payload key, label, how to format it) for the trusted-figures table. Never
#: shown as anything the AI produced — every value here is read straight off
#: the backend payload.
FIGURE_ROWS = (
    ("revenue", "Revenue", "money"),
    ("cost_of_sales", "Cost of sales", "money"),
    ("gross_profit", "Gross profit", "money"),
    ("gross_margin_pct", "Gross margin", "percent"),
    ("operating_expenses", "Operating expenses", "money"),
    ("net_profit", "Net profit", "money"),
    ("total_assets", "Total assets", "money"),
    ("total_liabilities", "Total liabilities", "money"),
    ("total_equity", "Total equity", "money"),
    ("receivables_total", "Receivables", "money"),
    ("receivables_overdue", "Overdue receivables", "money"),
    ("payables_total", "Payables", "money"),
    ("payables_overdue", "Overdue payables", "money"),
)


def _wants_json(request) -> bool:
    """Whether this looks like an API/AJAX caller rather than a browser form.

    A plain ``<form method="post">`` submission sends no special header, so it
    falls through to the HTML page — the default for everyone using the
    feature the way it is actually built to be used.
    """
    accept = request.headers.get("Accept", "")
    return (
        "application/json" in accept
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _first_message(exc: ValidationError) -> str:
    """The human-authored text of a ValidationError, dict or plain."""
    messages = getattr(exc, "messages", None)
    return str(messages[0]) if messages else str(exc)


def _form_error_message(form) -> str:
    """One line summarising why a submitted date range was rejected."""
    for field in form:
        if field.errors:
            return field.errors[0]
    if form.non_field_errors():
        return form.non_field_errors()[0]
    return "Please check the selected date range."


def hash_payload(payload: dict) -> str:
    """A stable fingerprint of the payload, for deduplication only.

    Sorted, separator-tight JSON so the same figures always hash the same way
    regardless of dict ordering — the hash is compared byte-for-byte, not
    parsed back, so any instability here would silently defeat deduplication.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matching_success(user, date_from: date, date_to: date, payload_hash: str, *, since=None):
    queryset = AISummaryRequest.objects.filter(
        user=user,
        date_from=date_from,
        date_to=date_to,
        payload_hash=payload_hash,
        status=AISummaryStatus.SUCCESS,
    )
    if since is not None:
        queryset = queryset.filter(created_at__gte=since)
    return queryset.order_by("-created_at").first()


def find_reusable_result(
    user, date_from: date, date_to: date, payload_hash: str
) -> dict | None:
    """A recent, successful answer to this exact request, if one exists.

    Used only to decide whether *generating* a new one can be skipped — bounded
    by ``AI_SUMMARY_DEDUP_WINDOW_SECONDS`` so an old match does not suppress a
    generation forever, even though the hash guarantees it would still be
    accurate. Matched on the hash as well as the dates: the same period asked
    about again after the ledger changed is not "the same request" and must
    not reuse a now-stale answer.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.AI_SUMMARY_DEDUP_WINDOW_SECONDS)
    row = _matching_success(user, date_from, date_to, payload_hash, since=cutoff)
    return row.response_data if row else None


def find_latest_result(user, date_from: date, date_to: date, payload_hash: str) -> dict | None:
    """The most recent answer to this exact request, for *display* only.

    Unlike ``find_reusable_result`` this has no time window: the hash already
    proves the figures have not changed since, so a match from a year ago is
    exactly as trustworthy as one from a minute ago. Reading this never calls
    the AI provider and never writes a row — it is what a GET is allowed to do.
    """
    row = _matching_success(user, date_from, date_to, payload_hash)
    return row.response_data if row else None


def rate_limit_exceeded(user) -> bool:
    """Whether this user has already used up this hour's AI requests.

    Checked (and, if not exceeded, counted) before the AI provider is ever
    called — a blocked request costs nothing. A fixed window rather than a
    sliding one: the count is set once with a one-hour expiry and only
    incremented after that, so it resets on the hour rather than being
    endlessly extended by activity.
    """
    key = RATE_LIMIT_CACHE_KEY.format(user_id=user.pk)
    count = cache.get(key, 0)
    if count >= settings.AI_SUMMARY_RATE_LIMIT_PER_HOUR:
        return True
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=RATE_LIMIT_WINDOW_SECONDS)
    return False


def _payload_context(payload: dict, date_from: date, date_to: date) -> dict:
    """Everything the template needs to show the trusted figures and period.

    ``date_from``/``date_to`` are passed alongside ``payload["period"]``
    (the same dates as plain ISO strings) because the two are used for
    different things: real ``date`` objects are what Django's ``date``
    template filter needs to print "September 1, 2026", while the strings are
    what the hidden form fields need for the redirect back to this page.
    """
    return {
        "figures": build_figures(payload),
        "date_from": date_from,
        "date_to": date_to,
        "period": payload["period"],
        "currency": payload["currency"],
        "excluded_currency_notes": excluded_currency_notes(payload),
    }


def build_figures(payload: dict) -> list[dict]:
    """The trusted-figures table's rows, read straight off the payload.

    Never asked of the AI, never influenced by it — this is the same payload
    ``ai_gateway.request_ai_summary`` was given, rendered for a person instead
    of a model.
    """
    return [
        {"label": label, "value": payload.get(key), "kind": kind}
        for key, label, kind in FIGURE_ROWS
    ]


#: Tag recorded on the audit event and used to build the download's filename
#: (see write_summary_csv / AISummaryPageView._export_csv).
EXPORT_REPORT_NAME = "ai_financial_summary"


def _money_cell(value) -> str:
    return "" if value is None else f"{value:.2f}"


def _percent_cell(value) -> str:
    return "" if value is None else f"{value:.1f}"


def write_summary_csv(writer, payload: dict, result: dict | None) -> int:
    """Write the exact figures (and, if generated, the exact AI text) a
    person is looking at on the page — never a second calculation, and never
    a fresh call to the AI provider to produce something to export.

    Returns the number of data rows written (excluding the header), for the
    audit record — matching the row count FilteredListView's own CSV export
    records for every other export in the project.
    """
    writer.writerow(["Section", "Item", "Value"])
    rows = [
        ["Period", "Period From", payload["period"]["from"]],
        ["Period", "Period To", payload["period"]["to"]],
        ["Period", "Currency", payload["currency"]],
    ]

    for row in build_figures(payload):
        cell = (
            _percent_cell(row["value"])
            if row["kind"] == "percent"
            else _money_cell(row["value"])
        )
        rows.append(["Financial Data", row["label"], cell])

    for note in excluded_currency_notes(payload):
        rows.append(["Financial Data", "Excluded currency", note])

    if result:
        rows.append(["AI Summary", "Summary", result["summary"]])
        for n, point in enumerate(result["key_points"], start=1):
            rows.append(["AI Summary", f"Key Point {n}", point])
        for n, warning in enumerate(result["warnings"], start=1):
            rows.append(["AI Summary", f"Warning {n}", warning])
        for n, item in enumerate(result["recommendations"], start=1):
            rows.append(["AI Summary", f"Recommendation {n}", item])
    else:
        rows.append(["AI Summary", "Status", "No AI summary available for this period."])

    writer.writerows(rows)
    return len(rows)


def excluded_currency_notes(payload: dict) -> list[str]:
    """A plain-English note for every currency an ageing total left out.

    ``apps.reports.ageing`` is single-currency by design, so anything open in
    another currency is not in the totals above — this says so, rather than
    letting the page look like a smaller number is the whole picture.
    """
    notes = []
    for key, noun in (
        ("receivables_excluded_currencies", "receivable"),
        ("payables_excluded_currencies", "payable"),
    ):
        for entry in payload.get(key) or []:
            count = entry["open_documents"]
            plural = "" if count == 1 else "s"
            verb = "is" if count == 1 else "are"
            notes.append(
                f"{count} open {noun}{plural} in {entry['currency']} {verb} not "
                f"included in the totals above (different currency from "
                f"{payload['currency']})."
            )
    return notes


class AISummaryPageView(ActionPermissionMixin, View):
    """The AI Financial Summary report page. Read-only — see module docstring."""

    required_permission = VIEW_FINANCIAL_REPORTS

    def get(self, request, *args, **kwargs):
        form = DateRangeForm(request.GET or None)
        date_from, date_to = form.window()

        # DateRangeForm's own default is the *whole* current fiscal period,
        # including the days after today that have not happened yet — right
        # for a trial balance someone will keep re-reading all month, wrong
        # for a page that must never ask the AI about a period still in
        # progress. Only the unrequested default is adjusted; a date range
        # someone actually typed in is left alone so an invalid one still
        # shows the validation error below, not a silently different range.
        #
        # "Explicitly requested" means real date params, not merely a bound
        # form — `?export=csv` alone also binds the form (any non-empty
        # querystring does), and that request still wants today's default
        # window, not whatever `default_window()` returns unclamped.
        explicit_dates = bool(request.GET.get("date_from") or request.GET.get("date_to"))
        if not (explicit_dates and form.is_valid()) and date_to > timezone.localdate():
            date_to = timezone.localdate()
            form.initial["date_to"] = date_to

        if request.GET.get("export") == "csv":
            # The on-screen page tolerates an invalid explicit range by
            # falling back to the default window and showing the problem
            # inline (matching every other report page) — reasonable when the
            # substitution is visible right next to the explanation. A CSV has
            # nowhere to show that explanation, so an export of a range the
            # user did not ask for would just look like the right answer.
            # Rejecting outright here is what "invalid ranges are rejected"
            # means for a download specifically.
            if explicit_dates and not form.is_valid():
                return HttpResponseBadRequest(_form_error_message(form))
            return self._export_csv(request, date_from, date_to)

        context = {
            "page_title": "AI Financial Summary",
            "page_subtitle": (
                "A plain-language explanation of the figures below, written from "
                "them and only them."
            ),
            "form": form,
            "result": None,
            "error": None,
        }

        try:
            payload = build_summary_payload(date_from, date_to)
        except ValidationError as exc:
            context["error"] = _first_message(exc)
            return render(request, TEMPLATE_NAME, context)

        context.update(_payload_context(payload, date_from, date_to))

        payload_hash = hash_payload(payload)
        context["result"] = find_latest_result(request.user, date_from, date_to, payload_hash)

        return render(request, TEMPLATE_NAME, context)

    def _export_csv(self, request, date_from: date, date_to: date) -> HttpResponse:
        """The same figures, and the same stored AI text, as the page shows.

        Never recomputes anything differently from the normal page render and
        never calls the AI provider — this is a second *view* of the exact
        same read, not a second implementation of it.
        """
        if not request.user.has_perm(EXPORT_DATA):
            raise PermissionDenied("You do not have permission to export data.")

        try:
            payload = build_summary_payload(date_from, date_to)
        except ValidationError as exc:
            return HttpResponseBadRequest(_first_message(exc))

        payload_hash = hash_payload(payload)
        result = find_latest_result(request.user, date_from, date_to, payload_hash)

        response = HttpResponse(content_type="text/csv")
        filename = f"{EXPORT_REPORT_NAME}_{date_from.isoformat()}_to_{date_to.isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        row_count = write_summary_csv(csv.writer(response), payload, result)
        audit.record_export(
            request,
            f"{EXPORT_REPORT_NAME} ({date_from} to {date_to}, {row_count} rows)",
            row_count,
        )
        return response


class GenerateAISummaryView(ActionPermissionMixin, View):
    """POST date_from/date_to, get redirected to the page showing the result.

    Uses the same ``VIEW_FINANCIAL_REPORTS`` permission and
    ``ActionPermissionMixin`` as every statement page in ``apps.reports`` —
    this is not a second authorisation system, just another screen gated on
    the same right to see the figures in the first place.

    A plain browser form post gets the ordinary Django redirect/render flow
    (302 to the page on success, the page re-rendered with an error on
    failure). A caller that sends ``Accept: application/json`` — an internal
    tool, or a future script — gets the JSON contract this endpoint has always
    had instead; nothing here is a second API surface, just content
    negotiation on one.
    """

    required_permission = VIEW_FINANCIAL_REPORTS
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        try:
            return self._handle(request)
        except Exception:
            # Anything not already turned into a safe response above — a
            # programming error, a database hiccup — ends here rather than as
            # a stack trace on the wire.
            logger.exception("Unexpected error generating an AI financial summary.")
            return self._error(request, GENERIC_ERROR_MESSAGE, status=500)

    def _handle(self, request):
        form = DateRangeForm(request.POST)
        if not form.is_valid():
            return self._error(
                request,
                _form_error_message(form),
                status=400,
                form=form,
                show_form_errors=True,
            )

        date_from = form.cleaned_data["date_from"]
        date_to = form.cleaned_data["date_to"]

        try:
            payload = build_summary_payload(date_from, date_to)
        except ValidationError as exc:
            return self._error(request, _first_message(exc), status=400, form=form)

        payload_hash = hash_payload(payload)

        reused = find_reusable_result(request.user, date_from, date_to, payload_hash)
        if reused is not None:
            return self._success(request, date_from, date_to, reused, reused_flag=True)

        if rate_limit_exceeded(request.user):
            logger.info("AI summary rate limit reached for user id %s.", request.user.pk)
            return self._error(
                request,
                RATE_LIMIT_MESSAGE,
                status=429,
                form=form,
                payload=payload,
                date_from=date_from,
                date_to=date_to,
            )

        result = ai_gateway.request_ai_summary(payload)

        AISummaryRequest.objects.create(
            user=request.user,
            date_from=date_from,
            date_to=date_to,
            payload_hash=payload_hash,
            status=AISummaryStatus.SUCCESS if result.success else AISummaryStatus.FAILED,
            response_data=result.data if result.success else None,
            error=result.error or "",
        )

        if not result.success:
            return self._error(
                request,
                result.error,
                status=503,
                form=form,
                payload=payload,
                date_from=date_from,
                date_to=date_to,
            )

        return self._success(request, date_from, date_to, result.data, reused_flag=False)

    def _success(self, request, date_from, date_to, data, *, reused_flag):
        if _wants_json(request):
            return JsonResponse({"status": "success", "data": data, "reused": reused_flag})
        url = (
            f"{reverse('ai_summary:summary')}"
            f"?date_from={date_from.isoformat()}&date_to={date_to.isoformat()}"
        )
        return redirect(url)

    def _error(
        self,
        request,
        message,
        *,
        status,
        form=None,
        payload=None,
        date_from=None,
        date_to=None,
        show_form_errors=False,
    ):
        if _wants_json(request):
            return JsonResponse({"error": message}, status=status)

        display_message = (
            NOT_CONFIGURED_ADMIN_MESSAGE
            if message == ai_gateway.ERROR_NOT_CONFIGURED
            else message
        )
        context = {
            "page_title": "AI Financial Summary",
            "form": form or DateRangeForm(),
            "result": None,
            # A rejected date range shows through the form's own field errors
            # (matching every other report page); anything else — rate limit,
            # provider trouble, an internal failure — has no field to blame
            # and is shown as a banner instead.
            "error": None if show_form_errors else display_message,
        }
        if payload is not None:
            context.update(_payload_context(payload, date_from, date_to))
        return render(request, TEMPLATE_NAME, context, status=status)
