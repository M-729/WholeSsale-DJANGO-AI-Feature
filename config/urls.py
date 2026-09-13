"""
Root URL configuration.

Member 1 owns this file. Other members add one `include()` line for their app
and keep their own routes in their app's urls.py — see CONTRIBUTING.md.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy

from apps.accounts import views as account_views
from apps.core import health as core_health
from apps.core import views as core_views

urlpatterns = [
    path("", core_views.home, name="dashboard"),
    # Probes for the load balancer. Unauthenticated by necessity - see the
    # module docstring for what they are careful not to say.
    path("healthz/", core_health.healthz, name="healthz"),
    path("readyz/", core_health.readyz, name="readyz"),
    # ACC-001: Django's session authentication, with our own templates.
    path("login/", account_views.SignInView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password-change/",
        account_views.PasswordChangeView.as_view(),
        name="password_change",
    ),
    path(
        "password-change/done/",
        account_views.PasswordChangeDoneView.as_view(),
        name="password_change_done",
    ),
    # Password reset, in four steps: ask, sent, choose, done. Django owns the
    # token and the single-use guarantee; the project supplies the templates and
    # the message. Without an email backend the message is printed to the
    # console — see EMAIL_BACKEND in settings.
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            success_url=reverse_lazy("password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/sent/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    # ACC-003: this creates an account that cannot sign in until an
    # administrator activates it, which is what makes it safe to expose.
    path("request-account/", account_views.AccountRequestView.as_view(), name="signup"),
    path(
        "request-account/sent/",
        account_views.AccountRequestDoneView.as_view(),
        name="signup_done",
    ),
    path("parties/", include("apps.parties.urls")),
    path("purchases/", include("apps.purchases.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("sales/", include("apps.sales.urls")),
    path("payments/", include("apps.payments.urls")),
    path("reports/", include("apps.reports.urls")),
    path("ai-summary/", include("apps.ai_summary.urls")),
    path("settings/", include("apps.core.urls")),
    path("admin/", admin.site.urls),
    path("catalog/", include("apps.catalog.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
