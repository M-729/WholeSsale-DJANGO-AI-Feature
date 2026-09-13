"""Builds the minimal, already-calculated payload an AI model is allowed to see.

The AI's job is to explain figures, never to produce them. Every number in
``build_summary_payload`` comes straight from ``apps.reports`` — the same
``profit_and_loss``, ``balance_sheet`` and ``ageing`` functions that render the
statement pages — so the explanation can never disagree with what those pages
show, and there is no code path here that computes a financial figure from raw
records itself.

Nothing in this module calls an AI provider. That, and the request/response
handling around it, belongs to a later step.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import Company
from apps.reports import ageing
from apps.reports.services import INCEPTION, balance_sheet, profit_and_loss

#: About five years. Long enough to talk about a trend, short enough that the
#: AI is never asked to summarise a decade of trading in one paragraph — and
#: short enough that a single request stays cheap regardless of who asks.
MAX_PERIOD_DAYS = 5 * 365 + 1


def validate_period(date_from: date, date_to: date) -> None:
    """Reject a period that cannot produce a meaningful, affordable summary.

    The inverted-range check mirrors ``reports.forms.DateRangeForm.clean`` so
    this feature agrees with the statement pages on what counts as a valid
    window. The other two rules exist only here: a summary is generated on
    demand against a paid, rate-limited API, so a period with no ledger
    activity yet (the future) or with no upper bound on how much history it
    spans is refused before anything is calculated. Valid historical periods,
    however old, are not restricted beyond that span.
    """
    if date_from > date_to:
        raise ValidationError({"date_to": "The end of the period comes before its start."})
    if date_to > timezone.localdate():
        raise ValidationError({"date_to": "The period cannot extend into the future."})
    if (date_to - date_from).days > MAX_PERIOD_DAYS:
        raise ValidationError(
            {
                "date_to": (
                    f"The period cannot span more than {MAX_PERIOD_DAYS} days (about 5 years)."
                )
            }
        )


def _num(value: Decimal | None) -> float | None:
    """A ledger figure at display precision, as plain JSON — never a Decimal.

    The AI only explains numbers; it never needs the ledger's full 4-decimal
    precision. Rounding here, rather than leaving it to whatever eventually
    serialises this dict, is what keeps a `Decimal` from ever reaching an API
    call by accident.
    """
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def _excluded_currencies(other_currencies: tuple[tuple[str, int], ...]) -> list[dict]:
    """Currencies an ageing run left out, named rather than silently dropped.

    ``apps.reports.ageing`` is single-currency by design — adding balances in
    two currencies together would be meaningless — so choosing one currency
    always excludes any open document in another. Naming what was excluded
    here is what keeps this feature from quietly understating what a business
    is owed or owes.
    """
    return [{"currency": code, "open_documents": count} for code, count in other_currencies]


def _period_metrics(date_from: date, date_to: date, currency_code: str) -> dict:
    """One window's aggregated figures, reused for both the requested period
    and its prior-period comparison so the two are always shaped alike."""
    pl = profit_and_loss(date_from, date_to)
    bs = balance_sheet(date_to)
    receivables = ageing.ageing(ageing.AR, date_to, currency_code)
    payables = ageing.ageing(ageing.AP, date_to, currency_code)

    return {
        "revenue": _num(pl.revenue.total),
        "cost_of_sales": _num(pl.cost_of_sales.total),
        "gross_profit": _num(pl.gross_profit),
        "gross_margin_pct": _num(pl.gross_margin_percent),
        "operating_expenses": _num(pl.operating_expenses.total),
        "net_profit": _num(pl.net_profit),
        "total_assets": _num(bs.total_assets),
        "total_liabilities": _num(bs.total_liabilities),
        "total_equity": _num(bs.total_equity),
        "receivables_total": _num(receivables.total),
        "receivables_overdue": _num(receivables.overdue),
        "receivables_overdue_pct": _num(receivables.overdue_percent),
        "receivables_excluded_currencies": _excluded_currencies(receivables.other_currencies),
        "payables_total": _num(payables.total),
        "payables_overdue": _num(payables.overdue),
        "payables_overdue_pct": _num(payables.overdue_percent),
        "payables_excluded_currencies": _excluded_currencies(payables.other_currencies),
    }


def _prior_window(date_from: date, date_to: date) -> tuple[date, date] | None:
    """The immediately preceding period of equal length, for a same-shape
    comparison — without handing the AI a full time series to page through.

    Returns ``None`` when there is no such period to compare against: either
    the arithmetic would go back further than any plausible ledger entry (see
    ``reports.services.INCEPTION``), or it would underflow the earliest date
    Python itself can represent.
    """
    span = date_to - date_from
    prior_to = date_from - timedelta(days=1)
    try:
        prior_from = prior_to - span
    except OverflowError:
        return None
    if prior_from < INCEPTION:
        return None
    return prior_from, prior_to


def build_summary_payload(date_from: date, date_to: date) -> dict:
    """The minimal aggregated figures an AI summary of this period may use.

    Every value is a plain ``float``, ``str`` or ``None`` — safe to pass
    straight to ``json.dumps`` — and traceable to a single call into
    ``apps.reports``. No invoice, payment, customer or vendor record is read
    directly, and nothing here writes to the ledger or any other accounting
    table.
    """
    validate_period(date_from, date_to)

    company = Company.objects.select_related("base_currency").first()
    if company is None:
        raise ValidationError(
            "The company has not been set up yet — configure it in Settings."
        )
    currency_code = company.base_currency.code

    payload = {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "currency": currency_code,
    }
    payload.update(_period_metrics(date_from, date_to, currency_code))

    prior_window = _prior_window(date_from, date_to)
    payload["prior_period"] = (
        _period_metrics(*prior_window, currency_code) if prior_window else None
    )
    return payload
