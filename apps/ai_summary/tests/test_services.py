"""Period validation and payload assembly for the AI Financial Summary.

Note: the payload tests need a real PostgreSQL test database (NFR-002), same
as apps.reports and apps.inventory — `python manage.py test apps.ai_summary`.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_summary import services
from apps.core.models import (
    Company,
    Currency,
    DocumentStatus,
    FiscalPeriod,
    FiscalYear,
)
from apps.ledger.models import (
    Account,
    AccountSubtype,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalType,
    NormalBalance,
)
from apps.parties.models import Customer, Vendor
from apps.purchases.models import PurchaseBill
from apps.reports import ageing
from apps.reports import services as reports_services
from apps.sales.models import SalesInvoice

ZERO = Decimal("0")


class ValidatePeriodTests(SimpleTestCase):
    """No database needed: `validate_period` is a pure function of two dates."""

    def test_a_valid_historical_range_is_accepted(self):
        services.validate_period(date(2020, 1, 1), date(2020, 3, 31))  # does not raise

    def test_today_as_the_end_of_the_range_is_accepted(self):
        today = timezone.localdate()
        services.validate_period(today - timedelta(days=10), today)

    def test_a_single_day_range_is_accepted(self):
        today = timezone.localdate()
        services.validate_period(today, today)

    def test_an_inverted_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            services.validate_period(date(2020, 3, 31), date(2020, 1, 1))

    def test_a_date_to_in_the_future_is_rejected(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(ValidationError):
            services.validate_period(tomorrow - timedelta(days=30), tomorrow)

    def test_a_period_at_the_maximum_length_is_accepted(self):
        start = date(2015, 1, 1)
        end = start + timedelta(days=services.MAX_PERIOD_DAYS)
        # Keep the fixture itself valid regardless of when the suite runs.
        if end > timezone.localdate():
            end = timezone.localdate()
            start = end - timedelta(days=services.MAX_PERIOD_DAYS)
        services.validate_period(start, end)

    def test_a_period_longer_than_the_maximum_is_rejected(self):
        start = date(2000, 1, 1)
        end = start + timedelta(days=services.MAX_PERIOD_DAYS + 1)
        with self.assertRaises(ValidationError):
            services.validate_period(start, end)

    def test_an_old_but_short_historical_period_is_not_restricted(self):
        """No lower bound: a valid accounting period from decades ago is fine."""
        services.validate_period(date(1950, 1, 1), date(1950, 1, 31))


class PayloadFixture:
    """A tiny ledger with a credit sale, an unpaid bill, and one foreign-
    currency invoice, built the same way apps.reports' own tests build one.
    """

    DATE_FROM = date(2020, 1, 1)
    DATE_TO = date(2020, 4, 15)

    @classmethod
    def setUpTestData(cls):
        # Seeded by apps.core's reference-data migration — not created here,
        # so this matches what build_summary_payload actually reads.
        cls.currency = Currency.objects.get(code="USD")
        cls.company = Company.objects.select_related("base_currency").first()
        cls.foreign_currency = Currency.objects.create(
            code="AIX", name="AI Summary Foreign Currency", symbol="X", is_active=True
        )

        year = FiscalYear.objects.create(
            code="AIS-FY20", start_date=date(2020, 1, 1), end_date=date(2020, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=1,
            name="AIS 2020",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
        )
        # BR-020 requires an entry date to fall inside the period it points
        # at, so the prior-period posting below needs a period of its own.
        prior_year = FiscalYear.objects.create(
            code="AIS-FY19", start_date=date(2019, 1, 1), end_date=date(2019, 12, 31)
        )
        cls.prior_period = FiscalPeriod.objects.create(
            fiscal_year=prior_year,
            period_no=1,
            name="AIS 2019",
            start_date=date(2019, 1, 1),
            end_date=date(2019, 12, 31),
        )

        cls.cash = cls._account(
            "AIS-1100", "AI Summary Cash", AccountType.ASSET, AccountSubtype.CURRENT_ASSET
        )
        cls.receivable = cls._account(
            "AIS-1210",
            "AI Summary Receivables",
            AccountType.ASSET,
            AccountSubtype.CURRENT_ASSET,
        )
        cls.payable = cls._account(
            "AIS-2100",
            "AI Summary Payables",
            AccountType.LIABILITY,
            AccountSubtype.CURRENT_LIABILITY,
        )
        cls.capital = cls._account(
            "AIS-3100", "AI Summary Capital", AccountType.EQUITY, AccountSubtype.EQUITY
        )
        cls.revenue = cls._account(
            "AIS-4100", "AI Summary Sales", AccountType.INCOME, AccountSubtype.REVENUE
        )
        cls.rent = cls._account(
            "AIS-6200",
            "AI Summary Rent",
            AccountType.EXPENSE,
            AccountSubtype.OPERATING_EXPENSE,
        )

        cls.customer = Customer.objects.create(
            code="AIS-CUST", name="Confidential Customer Ltd", currency=cls.currency
        )
        cls.foreign_customer = Customer.objects.create(
            code="AIS-CUSTX", name="Foreign Customer Ltd", currency=cls.foreign_currency
        )
        cls.vendor = Vendor.objects.create(
            code="AIS-VEND", name="Confidential Vendor Ltd", currency=cls.currency
        )

        cls._entry_no = 0

        # Funding — equity and cash only, no P&L effect.
        cls.post(
            date(2020, 1, 2),
            cls.currency,
            (cls.cash, Decimal("5000")),
            (cls.capital, Decimal("-5000")),
        )

        # A credit sale, still unpaid and overdue at DATE_TO: drives revenue
        # and the open receivable ageing sees.
        invoice_entry = cls.post(
            date(2020, 3, 1),
            cls.currency,
            (cls.receivable, Decimal("1200")),
            (cls.revenue, Decimal("-1200")),
        )
        cls.invoice = SalesInvoice.objects.create(
            journal_entry=invoice_entry,
            number="AIS-INV-0001",
            document_date=date(2020, 3, 1),
            due_date=date(2020, 3, 31),
            posting_date=date(2020, 3, 1),
            fiscal_period=cls.period,
            customer=cls.customer,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_txn=Decimal("1200"),
            total_base=Decimal("1200"),
            open_txn=Decimal("1200"),
            status=DocumentStatus.POSTED,
        )

        # An unpaid bill: drives operating expense and the open payable.
        bill_entry = cls.post(
            date(2020, 3, 1),
            cls.currency,
            (cls.rent, Decimal("300")),
            (cls.payable, Decimal("-300")),
        )
        cls.bill = PurchaseBill.objects.create(
            journal_entry=bill_entry,
            number="AIS-BILL-0001",
            vendor=cls.vendor,
            vendor_invoice_number="VEND-INV-0001",
            document_date=date(2020, 3, 1),
            due_date=date(2020, 3, 31),
            posting_date=date(2020, 3, 1),
            fiscal_period=cls.period,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_txn=Decimal("300"),
            total_base=Decimal("300"),
            open_txn=Decimal("300"),
            status=DocumentStatus.POSTED,
        )

        # Same shape, in a currency the payload's base-currency ageing must
        # exclude (and report as excluded) rather than fold in or drop.
        foreign_entry = cls.post(
            date(2020, 3, 1),
            cls.foreign_currency,
            (cls.receivable, Decimal("999")),
            (cls.revenue, Decimal("-999")),
        )
        cls.foreign_invoice = SalesInvoice.objects.create(
            journal_entry=foreign_entry,
            number="AIS-INV-FX",
            document_date=date(2020, 3, 1),
            due_date=date(2020, 3, 31),
            posting_date=date(2020, 3, 1),
            fiscal_period=cls.period,
            customer=cls.foreign_customer,
            currency=cls.foreign_currency,
            exchange_rate=Decimal("1"),
            total_txn=Decimal("999"),
            total_base=Decimal("999"),
            open_txn=Decimal("999"),
            status=DocumentStatus.POSTED,
        )

        # A separate posting in the prior comparison window, so that window
        # is not simply empty.
        cls.post(
            date(2019, 10, 1),
            cls.currency,
            (cls.cash, Decimal("1000")),
            (cls.revenue, Decimal("-1000")),
            period=cls.prior_period,
        )

    @classmethod
    def _account(cls, code, name, account_type, subtype):
        debit_natured = account_type in (AccountType.ASSET, AccountType.EXPENSE)
        return Account.objects.create(
            code=code,
            name=name,
            account_type=account_type,
            subtype=subtype,
            normal_balance=(NormalBalance.DEBIT if debit_natured else NormalBalance.CREDIT),
        )

    @classmethod
    def post(cls, entry_date, currency, *pairs, period=None):
        cls._entry_no += 1
        total = sum(amount for _, amount in pairs)
        if total != ZERO:
            raise ValueError(f"test journal does not balance, off by {total}")
        entry = JournalEntry.objects.create(
            number=f"AIS-JE-{cls._entry_no:04d}",
            entry_date=entry_date,
            fiscal_period=period or cls.period,
            journal_type=JournalType.GENERAL,
            narration="AI summary test entry",
            currency=currency,
            exchange_rate=Decimal("1"),
            total_debit_base=sum(a for _, a in pairs if a > 0),
            total_credit_base=-sum(a for _, a in pairs if a < 0),
        )
        for line_no, (account, amount) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry,
                line_no=line_no,
                account=account,
                currency=currency,
                exchange_rate=Decimal("1"),
                debit_base=amount if amount > 0 else ZERO,
                debit_txn=amount if amount > 0 else ZERO,
                credit_base=-amount if amount < 0 else ZERO,
                credit_txn=-amount if amount < 0 else ZERO,
            )
        return entry


EXPECTED_METRIC_KEYS = {
    "revenue",
    "cost_of_sales",
    "gross_profit",
    "gross_margin_pct",
    "operating_expenses",
    "net_profit",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "receivables_total",
    "receivables_overdue",
    "receivables_overdue_pct",
    "receivables_excluded_currencies",
    "payables_total",
    "payables_overdue",
    "payables_overdue_pct",
    "payables_excluded_currencies",
}


class BuildSummaryPayloadTests(PayloadFixture, TestCase):
    def payload(self):
        return services.build_summary_payload(self.DATE_FROM, self.DATE_TO)

    def test_the_payload_contains_only_the_documented_aggregated_fields(self):
        """No per-invoice, per-customer or per-vendor data — totals only."""
        payload = self.payload()
        top_level_keys = set(payload) - {"period", "currency", "prior_period"}
        self.assertEqual(top_level_keys, EXPECTED_METRIC_KEYS)
        self.assertEqual(set(payload["prior_period"]), EXPECTED_METRIC_KEYS)
        self.assertEqual(payload["period"], {"from": "2020-01-01", "to": "2020-04-15"})
        self.assertEqual(payload["currency"], "USD")

    def test_no_raw_record_identifiers_leak_into_the_payload(self):
        """The names on the fixture's invoice/customer/vendor never appear."""
        blob = json.dumps(self.payload())
        for leaked in (
            "Confidential Customer",
            "Confidential Vendor",
            "AIS-INV-0001",
            "AIS-BILL-0001",
            "VEND-INV-0001",
        ):
            self.assertNotIn(leaked, blob)

    def test_every_value_is_json_serialisable_with_no_decimals(self):
        payload = self.payload()
        blob = json.dumps(payload)  # raises TypeError if a Decimal slipped through
        reloaded = json.loads(blob)
        self.assertEqual(reloaded, payload)

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            else:
                self.assertNotIsInstance(node, Decimal)

        walk(payload)

    def test_figures_match_the_underlying_report_services_directly(self):
        """The payload is a thin wrapper, never a second calculation."""
        payload = self.payload()
        pl = reports_services.profit_and_loss(self.DATE_FROM, self.DATE_TO)
        bs = reports_services.balance_sheet(self.DATE_TO)
        receivables = ageing.ageing(ageing.AR, self.DATE_TO, "USD")
        payables = ageing.ageing(ageing.AP, self.DATE_TO, "USD")

        def rounded(value):
            return float(value.quantize(Decimal("0.01"))) if value is not None else None

        self.assertEqual(payload["revenue"], rounded(pl.revenue.total))
        self.assertEqual(payload["cost_of_sales"], rounded(pl.cost_of_sales.total))
        self.assertEqual(payload["gross_profit"], rounded(pl.gross_profit))
        self.assertEqual(payload["gross_margin_pct"], rounded(pl.gross_margin_percent))
        self.assertEqual(payload["operating_expenses"], rounded(pl.operating_expenses.total))
        self.assertEqual(payload["net_profit"], rounded(pl.net_profit))
        self.assertEqual(payload["total_assets"], rounded(bs.total_assets))
        self.assertEqual(payload["total_liabilities"], rounded(bs.total_liabilities))
        self.assertEqual(payload["total_equity"], rounded(bs.total_equity))
        self.assertEqual(payload["receivables_total"], rounded(receivables.total))
        self.assertEqual(payload["receivables_overdue"], rounded(receivables.overdue))
        self.assertEqual(payload["payables_total"], rounded(payables.total))
        self.assertEqual(payload["payables_overdue"], rounded(payables.overdue))

    def test_the_open_receivable_and_payable_are_reflected(self):
        payload = self.payload()
        self.assertEqual(payload["receivables_total"], 1200.0)
        self.assertEqual(payload["receivables_overdue"], 1200.0)
        self.assertEqual(payload["receivables_overdue_pct"], 100.0)
        self.assertEqual(payload["payables_total"], 300.0)
        self.assertEqual(payload["payables_overdue"], 300.0)

    def test_a_foreign_currency_document_is_excluded_and_named_not_dropped(self):
        payload = self.payload()
        # Not folded into the base-currency total...
        self.assertEqual(payload["receivables_total"], 1200.0)
        # ...but not silently missing either.
        self.assertIn(
            {"currency": "AIX", "open_documents": 1},
            payload["receivables_excluded_currencies"],
        )

    def test_the_prior_period_is_the_immediately_preceding_window_of_equal_length(self):
        payload = self.payload()
        span = self.DATE_TO - self.DATE_FROM
        expected_to = self.DATE_FROM - timedelta(days=1)
        expected_from = expected_to - span
        expected = services._period_metrics(expected_from, expected_to, "USD")
        self.assertEqual(payload["prior_period"], expected)
        # The prior window's own posting shows up, the current period's does not.
        self.assertEqual(payload["prior_period"]["revenue"], 1000.0)

    def test_prior_period_is_none_when_it_would_precede_inception(self):
        payload = services.build_summary_payload(date(1900, 1, 1), date(1900, 1, 31))
        self.assertIsNone(payload["prior_period"])

    def test_an_invalid_period_is_rejected_before_any_report_is_built(self):
        with self.assertRaises(ValidationError):
            services.build_summary_payload(date(2020, 4, 15), date(2020, 1, 1))
