"""
Core views.

Role-aware dashboard and audited configuration screens backed by Django ORM.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from apps.ai_summary.models import AISummaryRequest, AISummaryStatus
from apps.core import audit, period_close
from apps.core.forms import (
    AccountForm,
    AccountMappingForm,
    CompanyForm,
    CurrencyForm,
    DocumentSequenceForm,
    PaymentTermForm,
    PeriodCloseForm,
    PeriodReopenForm,
    TaxCodeForm,
)
from apps.core.list_views import (
    BooleanFilter,
    ChoiceFilter,
    Column,
    DateRangeFilter,
    FilteredListView,
)
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin, BackLinkMixin
from apps.core.models import (
    AuditAction,
    Company,
    Currency,
    DocumentSequence,
    DocumentType,
    FiscalPeriod,
    PaymentTerm,
    PeriodStatus,
    SequenceReset,
    TaxApplicability,
    TaxCode,
    TaxTreatment,
)
from apps.core.permissions import (
    CLOSE_PERIOD,
    EXPORT_DATA,
    MANAGE_CHART_OF_ACCOUNTS,
    MANAGE_CONFIGURATION,
    REOPEN_PERIOD,
)
from apps.ledger.models import (
    Account,
    AccountMapping,
    AccountSubtype,
    AccountType,
    MappingKey,
)
from apps.parties.models import Customer, Vendor
from apps.payments.models import Payment, PaymentDirection
from apps.sales.models import SalesOrder


def home(request):
    """
    The public home page at "/".

    Signed-out visitors get the marketing landing page. Signed-in users are
    handed straight to the dashboard, where the existing login and permission
    gates apply: a user with no role still receives the 403 they get today.
    """
    if request.user.is_authenticated:
        return dashboard(request)
    return render(request, "core/landing.html")


@login_required
@permission_required("core.view_company", raise_exception=True)
def dashboard(request):
    """
    Landing page after sign-in.

    The aggregate snapshot and recent activity share one short cache lifetime,
    which removes repeated round trips to a remote PostgreSQL region while
    keeping the operational overview acceptably fresh.
    """
    overview = cache.get("dashboard_overview_v1")
    if overview is None:
        company = Company.objects.select_related("base_currency").first()
        open_periods = list(FiscalPeriod.objects.filter(status="OPEN").order_by("start_date"))
        account_totals = Account.objects.filter(is_active=True).aggregate(
            total=Count("id"),
            postable=Count("id", filter=Q(is_postable=True)),
        )
        overview = {
            "company": company,
            "base_currency": company.base_currency_id if company else None,
            "current_period": open_periods[0] if open_periods else None,
            "open_period_count": len(open_periods),
            "account_count": account_totals["total"],
            "postable_account_count": account_totals["postable"],
            "missing_mappings": sorted(
                set(dict(MappingKey.choices))
                - set(AccountMapping.objects.values_list("key", flat=True))
            ),
            "customer_count": Customer.objects.filter(is_active=True).count(),
            "vendor_count": Vendor.objects.filter(is_active=True).count(),
            "sales_order_count": SalesOrder.objects.count(),
            "payment_totals": Payment.objects.aggregate(
                receipts=Sum("amount_base", filter=Q(direction=PaymentDirection.RECEIPT)),
                payments=Sum("amount_base", filter=Q(direction=PaymentDirection.PAYMENT)),
                unallocated=Sum("unallocated_txn"),
                drafts=Count("id", filter=Q(status="DRAFT")),
            ),
            "recent_payments": list(
                Payment.objects.select_related("customer", "vendor", "currency", "method")[:5]
            ),
        }
        cache.set("dashboard_overview_v1", overview, settings.DASHBOARD_CACHE_SECONDS)

    context = {
        "page_title": f"Good day, {request.user.full_name or request.user.username}.",
        "page_subtitle": "Configuration is in place. Operational modules arrive with the other slices.",
        **overview,
        # Per-user and cheap (one indexed query), so it is read fresh on every
        # request rather than folded into the shared cache above — unlike the
        # rest of this dashboard, whose figures are the same for anyone
        # looking at them. Never triggers an AI call: this only ever reads
        # rows a previous, explicit "Generate" click already created.
        "latest_ai_summary": AISummaryRequest.objects.filter(
            user=request.user, status=AISummaryStatus.SUCCESS
        )
        .order_by("-created_at")
        .first(),
    }
    return render(request, "core/dashboard.html", context)


class CurrencyListView(FilteredListView):
    """CFG-003: the currencies the business trades in."""

    model = Currency
    required_permission = "core.view_currency"
    page_title = "Currencies"
    page_subtitle = "Currencies available on documents. One is the base currency (BR-002)."
    default_ordering = "code"
    create_url_name = "core:currency_create"
    create_label = "New currency"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("symbol", "Symbol"),
        Column("decimal_places", "Decimals", align="right"),
        Column("is_base", "Base", badge=True, align="center"),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["code", "name"]

    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]

    export_permission = EXPORT_DATA
    export_filename = "currencies"

    def get_summary(self):
        queryset = self.get_queryset()
        totals = queryset.aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
        )
        return [
            ("Currencies", totals["total"]),
            ("Active", totals["active"]),
            ("Base", queryset.filter(is_base=True).first() or "—"),
        ]


class CurrencyCreateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView):
    back_url_name = "core:currency_list"
    back_label = "Back to currencies"
    model = Currency
    form_class = CurrencyForm
    template_name = "core/currency_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:currency_list")
    extra_context = {"page_title": "New currency"}


class CurrencyUpdateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView):
    back_url_name = "core:currency_list"
    back_label = "Back to currencies"
    model = Currency
    form_class = CurrencyForm
    template_name = "core/currency_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:currency_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


class TaxCodeListView(FilteredListView):
    """CFG-004: the tax codes available on documents."""

    model = TaxCode
    required_permission = "core.view_taxcode"
    page_title = "Tax codes"
    page_subtitle = "Rates and treatments applied to sales and purchase lines."
    default_ordering = "code"
    create_url_name = "core:taxcode_create"
    create_label = "New tax code"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("rate_percent", "Rate %", align="right", sortable=True),
        Column("get_treatment_display", "Treatment"),
        Column("get_applies_to_display", "Applies to"),
        Column("is_inclusive", "Inclusive", align="center"),
        Column("is_recoverable", "Recoverable", align="center"),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["code", "name"]

    filters = [
        ChoiceFilter("treatment", "Treatment", TaxTreatment.choices),
        ChoiceFilter("applies_to", "Applies to", TaxApplicability.choices),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]

    export_permission = EXPORT_DATA
    export_filename = "tax-codes"

    def get_summary(self):
        totals = self.get_queryset().aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
            standard=Count("pk", filter=Q(treatment=TaxTreatment.STANDARD)),
        )
        return [
            ("Tax codes", totals["total"]),
            ("Active", totals["active"]),
            ("Standard rated", totals["standard"]),
        ]


class TaxCodeCreateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView):
    back_url_name = "core:taxcode_list"
    back_label = "Back to tax codes"
    model = TaxCode
    form_class = TaxCodeForm
    template_name = "core/taxcode_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:taxcode_list")
    extra_context = {"page_title": "New tax code"}


class TaxCodeUpdateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView):
    back_url_name = "core:taxcode_list"
    back_label = "Back to tax codes"
    model = TaxCode
    form_class = TaxCodeForm
    template_name = "core/taxcode_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:taxcode_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


# ---------------------------------------------------------------------------
# Payment terms (CFG-005)
# ---------------------------------------------------------------------------
class PaymentTermListView(FilteredListView):
    model = PaymentTerm
    required_permission = "core.view_paymentterm"
    page_title = "Payment terms"
    page_subtitle = "When an invoice falls due, and any early-settlement discount."
    default_ordering = "code"
    create_url_name = "core:paymentterm_create"
    create_label = "New payment term"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("net_days", "Net days", align="right", sortable=True),
        Column("end_of_month", "End of month", align="center"),
        Column("discount_percent", "Discount %", align="right"),
        Column("discount_days", "Discount days", align="right"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name"]
    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive")
    ]
    export_permission = EXPORT_DATA
    export_filename = "payment-terms"

    def get_summary(self):
        totals = self.get_queryset().aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
        )
        return [("Payment terms", totals["total"]), ("Active", totals["active"])]


class PaymentTermCreateView(
    BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView
):
    back_url_name = "core:paymentterm_list"
    back_label = "Back to payment terms"
    model = PaymentTerm
    form_class = PaymentTermForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:paymentterm_list")
    extra_context = {
        "page_title": "New payment term",
    }


class PaymentTermUpdateView(
    BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView
):
    back_url_name = "core:paymentterm_list"
    back_label = "Back to payment terms"
    model = PaymentTerm
    form_class = PaymentTermForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:paymentterm_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


# ---------------------------------------------------------------------------
# Company settings (CFG-001, CFG-010)
# ---------------------------------------------------------------------------
class CompanySettingsView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView):
    """
    A singleton screen: one row, edited in place. There is no list and no
    create — the company is seeded, and BRD 3.1 allows exactly one.
    """

    back_url_name = "dashboard"
    back_label = "Back to dashboard"

    model = Company
    form_class = CompanyForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:company_settings")
    extra_context = {
        "page_title": "Company settings",
        "form_hint": "These values appear on every document and drive system-wide policy.",
    }

    def get_object(self, queryset=None):
        return Company.objects.select_related("base_currency").first()


# ---------------------------------------------------------------------------
# Number series (CFG-008)
# ---------------------------------------------------------------------------
class DocumentSequenceListView(FilteredListView):
    model = DocumentSequence
    required_permission = "core.view_documentsequence"
    page_title = "Number series"
    page_subtitle = "How each document type is numbered. Used by numbering.next_number()."
    default_ordering = "document_type"
    create_url_name = "core:sequence_create"
    create_label = "New series"

    columns = [
        Column(
            "get_document_type_display",
            "Document",
            sortable=True,
            link=True,
            order_by="document_type",
        ),
        Column("series", "Series", css="font-mono text-xs"),
        Column("prefix", "Prefix", css="font-mono text-xs"),
        Column("padding", "Padding", align="right"),
        Column("next_number", "Next number", align="right", sortable=True),
        Column("get_reset_policy_display", "Resets"),
        Column("period_key", "Period", css="font-mono text-xs"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["series", "prefix"]
    filters = [
        ChoiceFilter("document_type", "Document type", DocumentType.choices),
        ChoiceFilter("reset_policy", "Resets", SequenceReset.choices),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "number-series"

    def get_summary(self):
        totals = self.get_queryset().aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
        )
        return [("Series", totals["total"]), ("Active", totals["active"])]


class DocumentSequenceCreateView(
    BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView
):
    back_url_name = "core:sequence_list"
    back_label = "Back to number series"
    model = DocumentSequence
    form_class = DocumentSequenceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:sequence_list")
    extra_context = {
        "page_title": "New number series",
    }


class DocumentSequenceUpdateView(
    BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView
):
    back_url_name = "core:sequence_list"
    back_label = "Back to number series"
    model = DocumentSequence
    form_class = DocumentSequenceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:sequence_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object}"
        return ctx


# ---------------------------------------------------------------------------
# Fiscal calendar (CFG-009) — read-only here. Member 4 owns close/reopen.
# ---------------------------------------------------------------------------
class FiscalPeriodListView(FilteredListView):
    model = FiscalPeriod
    required_permission = "core.view_fiscalperiod"
    page_title = "Fiscal periods"
    page_subtitle = "Posting windows. Open one to run its close checklist (CFG-009, ACC-008)."
    default_ordering = "start_date"
    paginate_by = 50

    columns = [
        Column("name", "Period", sortable=True, link=True),
        Column("fiscal_year", "Year", sortable=True),
        Column("period_no", "No.", align="right"),
        Column("start_date", "Starts", sortable=True),
        Column("end_date", "Ends", sortable=True),
        Column("status", "Status", badge=True, align="center"),
        Column("closed_by", "Closed by"),
    ]
    search_fields = ["name", "fiscal_year__code"]
    filters = [
        ChoiceFilter("status", "Status", PeriodStatus.choices),
        DateRangeFilter("start_date", "Starting between"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "fiscal-periods"

    def get_queryset(self):
        return super().get_queryset().select_related("fiscal_year", "closed_by")

    def get_summary(self):
        totals = self.get_queryset().aggregate(
            total=Count("pk"),
            open=Count("pk", filter=Q(status=PeriodStatus.OPEN)),
            closed=Count("pk", filter=~Q(status=PeriodStatus.OPEN)),
        )
        return [
            ("Periods", totals["total"]),
            ("Open", totals["open"]),
            ("Closed", totals["closed"]),
        ]


# ---------------------------------------------------------------------------
# Chart of accounts (CFG-006, GL-010)
# ---------------------------------------------------------------------------
class AccountListView(FilteredListView):
    model = Account
    page_title = "Chart of accounts"
    page_subtitle = "Every account in the general ledger. Postings go to leaf accounts only."
    default_ordering = "code"
    paginate_by = 50
    create_url_name = "core:account_create"
    create_label = "New account"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("get_account_type_display", "Type", sortable=True, order_by="account_type"),
        Column("get_subtype_display", "Subtype"),
        Column("get_normal_balance_display", "Normal"),
        Column("parent", "Parent", css="font-mono text-xs"),
        Column("is_postable", "Postable", align="center"),
        Column("is_control", "Control", align="center"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name", "description"]
    filters = [
        ChoiceFilter("account_type", "Type", AccountType.choices),
        ChoiceFilter("subtype", "Subtype", AccountSubtype.choices),
        BooleanFilter("is_postable", "Postable", true_label="Postable", false_label="Heading"),
        BooleanFilter("is_control", "Control", true_label="Control", false_label="Ordinary"),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "chart-of-accounts"

    def get_queryset(self):
        return super().get_queryset().select_related("parent")

    def get_summary(self):
        totals = Account.objects.aggregate(
            total=Count("id"),
            postable=Count("id", filter=Q(is_postable=True, is_active=True)),
            control=Count("id", filter=Q(is_control=True)),
        )
        return [
            ("Accounts", totals["total"]),
            ("Postable", totals["postable"]),
            ("Control accounts", totals["control"]),
        ]


class AccountCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url = reverse_lazy("core:account_list")
    extra_context = {
        "page_title": "New account",
        "cancel_url": "/settings/chart-of-accounts/",
    }


class AccountUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url = reverse_lazy("core:account_list")
    extra_context = {"cancel_url": "/settings/chart-of-accounts/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


# ---------------------------------------------------------------------------
# Account mappings (CFG-007)
# ---------------------------------------------------------------------------
class AccountMappingListView(FilteredListView):
    model = AccountMapping
    page_title = "Account mappings"
    page_subtitle = (
        "Where each automatic posting sends its debits and credits. Posting stops "
        "with a clear message when a key is missing (CFG-007)."
    )
    default_ordering = "key"
    paginate_by = 50
    create_url_name = "core:mapping_create"
    create_label = "New mapping"

    columns = [
        Column("get_key_display", "Key", sortable=True, link=True, order_by="key"),
        Column("account", "Account"),
        Column("notes", "Notes"),
    ]
    search_fields = ["key", "account__code", "account__name", "notes"]
    export_permission = EXPORT_DATA
    export_filename = "account-mappings"

    def get_queryset(self):
        return super().get_queryset().select_related("account")

    def get_summary(self):
        mapped = set(AccountMapping.objects.values_list("key", flat=True))
        return [
            ("Keys", len(MappingKey.choices)),
            ("Mapped", len(mapped)),
            ("Missing", len(MappingKey.choices) - len(mapped)),
        ]


class AccountMappingCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = AccountMapping
    form_class = AccountMappingForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url = reverse_lazy("core:mapping_list")
    extra_context = {
        "page_title": "New account mapping",
        "cancel_url": "/settings/account-mappings/",
    }


class AccountMappingUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = AccountMapping
    form_class = AccountMappingForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url = reverse_lazy("core:mapping_list")
    extra_context = {"cancel_url": "/settings/account-mappings/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.get_key_display()}"
        return ctx


# ---------------------------------------------------------------------------
# Period close (CFG-009, ACC-008, BR-020)
# ---------------------------------------------------------------------------
class PeriodCloseView(BackLinkMixin, ActionPermissionMixin, DetailView):
    """The checklist for one period, and the button that signs it off.

    Read access is the view permission; closing needs CLOSE_PERIOD and
    reopening needs REOPEN_PERIOD, both checked on the actions rather than
    here, so somebody without either can still see why a period is not ready.
    """

    model = FiscalPeriod
    template_name = "core/period_close.html"
    context_object_name = "period"
    required_permission = "core.view_fiscalperiod"
    back_url_name = "core:fiscalperiod_list"
    back_label = "Back to periods"

    def get_queryset(self):
        return super().get_queryset().select_related("fiscal_year", "closed_by", "reopened_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        report = period_close.checklist(self.object)
        ctx.update(
            {
                "page_title": f"Close {self.object.name}",
                "page_subtitle": (
                    f"{self.object.start_date} to {self.object.end_date} · "
                    f"{self.object.get_status_display()}"
                ),
                "checklist": report,
                "close_form": PeriodCloseForm(warnings=report.warnings),
                "reopen_form": PeriodReopenForm(),
                "can_close": self.request.user.has_perm(CLOSE_PERIOD),
                "can_reopen": self.request.user.has_perm(REOPEN_PERIOD),
            }
        )
        return ctx


class PeriodCloseActionView(ActionPermissionMixin, View):
    required_permission = CLOSE_PERIOD
    http_method_names = ["post"]

    def post(self, request, pk):
        period = get_object_or_404(FiscalPeriod, pk=pk)
        # Recomputed here so the acknowledgement matches what is true now, not
        # what was true when the page was drawn.
        report = period_close.checklist(period)
        form = PeriodCloseForm(request.POST, warnings=report.warnings)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, "; ".join(error))
            return redirect("core:period_close", pk=pk)
        try:
            closed = period_close.close_period(
                period, user=request.user, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            audit.record_action(
                request,
                AuditAction.CLOSE_PERIOD,
                closed,
                reason=closed.close_reason,
                warnings_acknowledged=len(report.warnings),
            )
            messages.success(request, f"{closed.name} is closed.")
        return redirect("core:period_close", pk=pk)


class PeriodReopenActionView(ActionPermissionMixin, View):
    required_permission = REOPEN_PERIOD
    http_method_names = ["post"]

    def post(self, request, pk):
        period = get_object_or_404(FiscalPeriod, pk=pk)
        form = PeriodReopenForm(request.POST)
        if not form.is_valid():
            messages.error(request, "A reason is required to reopen a period.")
            return redirect("core:period_close", pk=pk)
        try:
            reopened = period_close.reopen_period(
                period, user=request.user, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            audit.record_action(
                request,
                AuditAction.REOPEN_PERIOD,
                reopened,
                reason=reopened.reopen_reason,
            )
            messages.success(
                request, f"{reopened.name} is open again. Close it when the change is made."
            )
        return redirect("core:period_close", pk=pk)
