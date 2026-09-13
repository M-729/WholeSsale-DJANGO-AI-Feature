"""The sidebar, as data (UX-002).

The navigation used to live as ~250 lines of markup in base.html, with each
row repeating its own permission guard and its own active-state expression.
Thirty-seven rows of that is hard to read and easy to get subtly wrong - and it
was: several rows had no permission guard at all while their neighbours did.

Declaring it here instead means the shape of the menu can be seen at a glance,
the active rule for a row sits beside the row it belongs to, and the whole
thing can be tested. `_nav.html` renders it and holds no knowledge of any
particular module.

Nothing here decides *whether* a user may reach a page. Every view enforces its
own permission; `permission` on an item only decides whether to draw a door
somebody cannot open.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Item:
    """One navigation row.

    The match fields encode when the row is the current page. They read as
    "this row is active when...", and each mirrors exactly what the template
    used to test inline:

        exact   the resolved url_name is one of these
        prefix  the url_name starts with this
        app     ...and belongs to this app namespace
        path    the request path contains this
    """

    label: str
    url_name: str
    icon: str
    permission: str = ""
    exact: tuple[str, ...] = ()
    prefix: str = ""
    app: str = ""
    path: str = ""
    #: A row that is deliberately never highlighted. The Django admin opens a
    #: different application, so there is no "you are here" to claim.
    never_active: bool = False

    def visible_to(self, perms, user) -> bool:
        if not self.permission:
            return True
        if self.permission == "is_staff":
            return bool(user.is_staff)
        return self.permission in perms

    def is_active(self, url_name: str, app_name: str, request_path: str) -> bool:
        if self.never_active:
            return False
        if self.path:
            return self.path in request_path
        if self.app and app_name != self.app:
            return False
        if self.prefix and url_name.startswith(self.prefix):
            return True
        if url_name in self.exact:
            return True
        # An app with no narrower rule claims every page in it, which is how
        # Payments behaved before: one row, one app, any screen within it.
        return bool(self.app) and not self.prefix and not self.exact


@dataclass(frozen=True, slots=True)
class Subgroup:
    """A labelled run of rows inside a section - the Finance section needs it."""

    label: str
    items: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class Section:
    """One collapsible block of the sidebar."""

    key: str
    label: str
    items: tuple = ()
    subgroups: tuple[Subgroup, ...] = field(default=())
    #: Sections a reader is always oriented by stay open and lose the control.
    collapsible: bool = True
    #: The module rail's glyph. One per section, so the rail is a list of
    #: modules rather than a second copy of the row icons.
    icon: str = "nav-dashboard"


# ---------------------------------------------------------------------------
# The menu
#
# Permissions and active rules are copied from what base.html tested inline,
# unchanged. Where a row had no guard before it has none now: tightening them
# would change what each role can see, which is a decision about access rather
# than about layout, and is listed in the review notes instead.
# ---------------------------------------------------------------------------
SECTIONS: tuple[Section, ...] = (
    Section(
        key="overview",
        icon="nav-dashboard",
        label="Overview",
        collapsible=False,
        items=(Item("Dashboard", "dashboard", "nav-dashboard", exact=("dashboard",)),),
    ),
    Section(
        key="sales",
        icon="sec-sales",
        label="Sales",
        items=(
            Item(
                "Sales orders",
                "sales:so_list",
                "nav-sales-order",
                permission="sales.view_salesorder",
                prefix="so_",
            ),
            Item(
                "Deliveries",
                "sales:delivery_list",
                "nav-delivery",
                permission="inventory.view_deliverynote",
                prefix="delivery_",
            ),
            Item(
                "Invoices",
                "sales:invoice_list",
                "nav-invoice",
                permission="sales.view_salesinvoice",
                prefix="invoice_",
            ),
            Item(
                "Returns",
                "sales:return_list",
                "nav-return",
                permission="sales.view_salesreturn",
                prefix="return_",
            ),
            Item(
                "Credit notes",
                "sales:credit_note_list",
                "nav-credit-note",
                permission="sales.view_salescreditnote",
                prefix="credit_note_",
            ),
        ),
    ),
    Section(
        key="purchasing",
        icon="sec-purchasing",
        label="Purchasing",
        items=(
            Item(
                "Purchase orders",
                "purchases:po_list",
                "nav-purchase-order",
                app="purchases",
                prefix="po_",
            ),
            Item(
                "Purchase bills",
                "purchases:bill_list",
                "nav-bill",
                app="purchases",
                prefix="bill_",
            ),
            Item(
                "Purchase returns",
                "purchases:pr_list",
                "nav-return",
                app="purchases",
                prefix="pr_",
            ),
            Item(
                "Vendor debit notes",
                "purchases:dbn_list",
                "nav-debit-note",
                app="purchases",
                prefix="dbn_",
            ),
        ),
    ),
    Section(
        key="finance",
        icon="sec-finance",
        label="Finance",
        items=(
            Item(
                "Payments",
                "payments:payment_list",
                "nav-payment",
                permission="payments.view_payment",
                app="payments",
            ),
        ),
        subgroups=(
            Subgroup(
                "Accounting",
                (
                    Item(
                        "General ledger",
                        "reports:general_ledger",
                        "nav-ledger",
                        permission="core.view_financial_reports",
                        exact=("general_ledger",),
                    ),
                    Item(
                        "Trial balance",
                        "reports:trial_balance",
                        "nav-trial-balance",
                        permission="core.view_financial_reports",
                        exact=("trial_balance",),
                    ),
                    Item(
                        "Profit and loss",
                        "reports:profit_and_loss",
                        "nav-pnl",
                        permission="core.view_financial_reports",
                        exact=("profit_and_loss",),
                    ),
                    Item(
                        "Balance sheet",
                        "reports:balance_sheet",
                        "nav-balance-sheet",
                        permission="core.view_financial_reports",
                        exact=("balance_sheet",),
                    ),
                    Item(
                        "AI Financial Summary",
                        "ai_summary:summary",
                        "nav-reports",
                        permission="core.view_financial_reports",
                        app="ai_summary",
                    ),
                ),
            ),
            Subgroup(
                "Financial reports",
                (
                    Item(
                        "Receivables ageing",
                        "reports:ar_ageing",
                        "nav-ar-ageing",
                        permission="core.view_financial_reports",
                        exact=("ar_ageing",),
                    ),
                    Item(
                        "Payables ageing",
                        "reports:ap_ageing",
                        "nav-ap-ageing",
                        permission="core.view_financial_reports",
                        exact=("ap_ageing",),
                    ),
                    Item(
                        "Tax report",
                        "reports:tax",
                        "nav-tax",
                        permission="core.view_financial_reports",
                        exact=("tax",),
                    ),
                    Item(
                        "Money register",
                        "reports:money_register",
                        "nav-register",
                        permission="core.view_financial_reports",
                        exact=("money_register",),
                    ),
                ),
            ),
            Subgroup(
                "Controls",
                (
                    Item(
                        "Reconciliation",
                        "reports:reconciliation",
                        "nav-reconcile",
                        permission="core.view_financial_reports",
                        exact=("reconciliation",),
                    ),
                    Item(
                        "Period close",
                        "core:fiscalperiod_list",
                        "lock",
                        permission="core.view_fiscalperiod",
                        prefix="period",
                        exact=("fiscalperiod_list",),
                    ),
                ),
            ),
        ),
    ),
    Section(
        key="relationships",
        icon="nav-customers",
        label="Relationships",
        items=(
            Item(
                "Customers",
                "parties:customer_list",
                "nav-customers",
                permission="parties.view_customer",
                prefix="customer",
            ),
            Item(
                "Vendors",
                "parties:vendor_list",
                "nav-vendors",
                permission="parties.view_vendor",
                prefix="vendor",
            ),
        ),
    ),
    # Ported from PR #39 (Product Catalog). The rows are that branch's, name
    # for name: the same labels, url names, permissions and active rules it
    # declared inline against the old markup. Only the shape changed, because
    # the markup it was written against no longer exists.
    Section(
        key="catalog",
        icon="sec-catalog",
        label="Catalog",
        items=(
            Item(
                "Products",
                "catalog:product_list",
                "nav-product",
                permission="catalog.view_product",
                path="/catalog/products/",
            ),
            Item(
                "Categories",
                "catalog:category_list",
                "nav-category",
                permission="catalog.view_productcategory",
                path="/catalog/categories/",
            ),
            Item(
                "Units",
                "catalog:unit_list",
                "nav-unit",
                permission="catalog.view_unitofmeasure",
                path="/catalog/units/",
            ),
        ),
    ),
    Section(
        key="inventory",
        icon="sec-inventory",
        label="Inventory",
        items=(
            Item(
                "Goods receipts",
                "inventory:gr_list",
                "nav-receipt",
                app="inventory",
                prefix="gr_",
            ),
            Item(
                "Stock transfers",
                "inventory:st_list",
                "nav-transfer",
                app="inventory",
                prefix="st_",
            ),
            Item(
                "Stock adjustments",
                "inventory:sa_list",
                "nav-adjust",
                app="inventory",
                prefix="sa_",
            ),
            Item(
                "Stock ledger",
                "inventory:stock_ledger",
                "nav-stock-ledger",
                exact=("stock_ledger",),
            ),
            Item(
                "Inventory valuation",
                "inventory:stock_valuation",
                "nav-valuation",
                exact=("stock_valuation",),
            ),
            Item(
                "Low stock",
                "inventory:low_stock",
                "alert",
                exact=("low_stock",),
            ),
        ),
    ),
    Section(
        key="settings",
        icon="sec-settings",
        label="Settings",
        items=(
            Item("Company", "core:company_settings", "nav-company", path="/settings/company/"),
            Item(
                "Currencies",
                "core:currency_list",
                "nav-currency",
                path="/settings/currencies/",
            ),
            Item("Tax codes", "core:taxcode_list", "nav-tax", path="/settings/tax-codes/"),
            Item(
                "Payment terms",
                "core:paymentterm_list",
                "nav-terms",
                path="/settings/payment-terms/",
            ),
            Item(
                "Number series",
                "core:sequence_list",
                "nav-series",
                path="/settings/number-series/",
            ),
            Item(
                "Fiscal periods",
                "core:fiscalperiod_list",
                "nav-calendar",
                path="/settings/fiscal-periods/",
            ),
            Item(
                "Chart of accounts",
                "core:account_list",
                "nav-coa",
                path="/settings/chart-of-accounts/",
            ),
            Item(
                "Account mappings",
                "core:mapping_list",
                "nav-mapping",
                path="/settings/account-mappings/",
            ),
        ),
    ),
    Section(
        key="system",
        icon="shield",
        label="System",
        items=(
            Item(
                "Django admin",
                "admin:index",
                "shield",
                permission="is_staff",
                never_active=True,
            ),
        ),
    ),
)
