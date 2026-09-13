"""
Django settings for the Wholesale Accounting & Business Management System.

Every environment-specific value comes from the environment, loaded from a .env
file in development. Nothing secret is committed: `.env` is gitignored and
`.env.example` documents the keys. See CONTRIBUTING.md.
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# .env loading
#
# A deliberately tiny reader rather than a dependency: it keeps `pip install`
# to two packages, and it cannot surprise anyone with precedence rules.
# Real environment variables always win over the file, so production just sets
# them and never ships a .env at all.
# ---------------------------------------------------------------------------
def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")


def env(key, default=None, required=False):
    """
    Read a setting from the environment.

    An empty value counts as unset, so a blank line in .env (`DJANGO_SECRET_KEY=`)
    falls back to the default rather than silently setting it to "". Getting this
    wrong produces "The SECRET_KEY setting must not be empty" on the first page
    load, which is a confusing way to start a project.
    """
    value = os.environ.get(key)
    if value is None or value == "":
        value = default
    if required and not value:
        raise RuntimeError(
            f"{key} is not set. Copy .env.example to .env and fill it in "
            f"(see the Setup section of README.md)."
        )
    return value


def env_bool(key, default=False):
    raw = env(key, str(default))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_list(key, default=""):
    return [item.strip() for item in env(key, default).split(",") if item.strip()]


def env_int(key, default):
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer.") from exc


DEBUG = env_bool("DJANGO_DEBUG", False)

# In development a throwaway key is fine; in production the app refuses to start
# without a real one rather than running on a key that is public on GitHub.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="dev-only-insecure-key-do-not-use-in-production" if DEBUG else None,
    required=not DEBUG,
)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Domain apps (BRD 11.2). Order matters only for readability.
    "apps.core",
    "apps.accounts",
    "apps.ledger",
    "apps.parties",
    "apps.catalog",
    "apps.inventory",
    "apps.sales",
    "apps.purchases",
    "apps.payments",
    "apps.reports",
    "apps.ai_summary",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Directly after SecurityMiddleware and before everything else, which is
    # what WhiteNoise documents: a static file should not be paying for session
    # loading, authentication and message middleware on its way out.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",  # ACC-007
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # The company name and base currency appear in the shell of
                # every page, so they cannot be one view's responsibility.
                "apps.core.context.company",
                "apps.core.context.navigation",
            ]
        },
    }
]

# NFR-002: PostgreSQL in every deployed environment. SQLite is not an option —
# the schema uses exclusion constraints, partial indexes, trigram indexes and
# plpgsql triggers, none of which SQLite has.

# ---------------------------------------------------------------------------
# TCP keepalives.
#
# connect_timeout only bounds *establishing* a connection. Once one is open,
# libpq blocks on the socket indefinitely, so a peer that goes away without
# sending a FIN - a pooler recycling, a NAT table forgetting the flow, a
# network partition - leaves the client waiting forever rather than failing.
# Observed in practice: a management command sat on a dead socket for hours.
#
# These make the kernel probe an idle connection and give up on it, so a dead
# connection surfaces as an error in about a minute and the caller can react.
# Gunicorn's own request timeout covers a web worker; nothing covers a
# management command except this.
# ---------------------------------------------------------------------------
KEEPALIVE_OPTIONS = {
    "keepalives": 1,
    "keepalives_idle": env_int("DB_KEEPALIVES_IDLE", 30),
    "keepalives_interval": env_int("DB_KEEPALIVES_INTERVAL", 10),
    "keepalives_count": env_int("DB_KEEPALIVES_COUNT", 3),
}

database_url = env("DATABASE_URL")
if database_url:
    parsed_database_url = urlsplit(database_url)
    if parsed_database_url.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("DATABASE_URL must use the postgres:// or postgresql:// scheme.")

    query_options = parse_qs(parsed_database_url.query)
    database_config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed_database_url.path.lstrip("/")) or "postgres",
        "USER": unquote(parsed_database_url.username or ""),
        "PASSWORD": unquote(parsed_database_url.password or ""),
        "HOST": parsed_database_url.hostname or "",
        "PORT": str(parsed_database_url.port or 5432),
        "OPTIONS": {
            "sslmode": query_options.get("sslmode", ["require"])[-1],
            "connect_timeout": env_int("DB_CONNECT_TIMEOUT", 10),
            "application_name": env("DB_APPLICATION_NAME", "ledgerwise-django"),
            **KEEPALIVE_OPTIONS,
        },
    }
else:
    database_config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("PGDATABASE", "wams"),
        "USER": env("PGUSER", "postgres"),
        "PASSWORD": env("PGPASSWORD", ""),
        "HOST": env("PGHOST", "127.0.0.1"),
        "PORT": env("PGPORT", "5432"),
        "OPTIONS": {
            "sslmode": env("PGSSLMODE", "prefer"),
            "connect_timeout": env_int("DB_CONNECT_TIMEOUT", 10),
            "application_name": env("DB_APPLICATION_NAME", "ledgerwise-django"),
            **KEEPALIVE_OPTIONS,
        },
    }

# Posting services open their own transaction.atomic() blocks (BR-005), so
# wrapping every request in a transaction would nest pointlessly.
database_config.update(
    {
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
        # A health check costs one extra round trip on the first query of each
        # request. That is real money against a remote database, but the pooler
        # does drop connections, and reusing a dead one is not a slow request -
        # it is a 500 for whoever made it. Off in development, where the churn
        # does not happen and the round trip is the whole cost; on by default
        # once DEBUG is off, and still overridable per environment.
        "CONN_HEALTH_CHECKS": env_bool("DB_CONN_HEALTH_CHECKS", not DEBUG),
    }
)
test_database_name = env("DJANGO_TEST_DATABASE_NAME")
if test_database_name:
    database_config["TEST"] = {"NAME": test_database_name}
DATABASES = {
    "default": database_config,
}

# The dashboard is intentionally short-lived: it avoids repeated remote
# aggregates during navigation without presenting operational data as live.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ledgerwise-process-cache",
        "TIMEOUT": env_int("DJANGO_CACHE_DEFAULT_TIMEOUT", 300),
        "OPTIONS": {"MAX_ENTRIES": env_int("DJANGO_CACHE_MAX_ENTRIES", 1000)},
    }
}
DASHBOARD_CACHE_SECONDS = env_int("DASHBOARD_CACHE_SECONDS", 30)

AUTH_USER_MODEL = "accounts.User"

# Usernames are what administrators issue, but the email address is what people
# remember about a work account — and it is already unique on the model. The
# backend subclasses ModelBackend, so permissions and the is_active rule are
# Django's own; only the identifier lookup is widened.
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailOrUsernameModelBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Authentication (ACC-001, ACC-002)
# ---------------------------------------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_AGE", 43200)  # 12 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Preserve database-backed session durability while caching repeat navigation
# in the serving process. A cache miss always falls back to django_session.
# ---------------------------------------------------------------------------
# Email (ACC-001 password reset).
#
# Reset works by sending a signed, single-use link, so without a backend the
# feature silently does nothing. Development prints the message to the console,
# which is enough to click the link; a deployment sets DJANGO_EMAIL_HOST and the
# SMTP backend takes over. Nothing else in the product sends mail.
# ---------------------------------------------------------------------------
EMAIL_HOST = env("DJANGO_EMAIL_HOST")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(env("DJANGO_EMAIL_PORT") or 587)
    EMAIL_HOST_USER = env("DJANGO_EMAIL_USER")
    EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_PASSWORD")
    EMAIL_USE_TLS = env("DJANGO_EMAIL_USE_TLS") != "0"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = env("DJANGO_FROM_EMAIL") or "no-reply@ledgerwise.local"

#: How long a reset link stays valid. Three hours is long enough to find the
#: mail and short enough that a forwarded one has usually expired.
PASSWORD_RESET_TIMEOUT = int(env("DJANGO_PASSWORD_RESET_TIMEOUT") or 60 * 60 * 3)

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# ---------------------------------------------------------------------------
# Stripe (PAY-013).
#
# Off unless a secret key is present, which is what makes it safe to ship to
# deployments that will never use it. Nothing here has a default: a card
# integration running against somebody else's guessed-at key is worse than one
# that is plainly switched off.
#
# STRIPE_WEBHOOK_SECRET is what separates a real Stripe callback from anyone on
# the internet who found the URL, so the webhook refuses every request when it
# is unset rather than trusting the body.
# ---------------------------------------------------------------------------
# Whether Stripe is on is derived from this key wherever it is asked, by
# stripe_gateway.is_enabled() - not cached into a second setting that could then
# disagree with it.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
# Stripe has to be told where to send the customer back to, and it needs an
# absolute URL. In development that is the runserver; a deployment sets it to
# the public origin. No trailing slash.
STRIPE_RETURN_ORIGIN = env("STRIPE_RETURN_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
# A checkout link should not stay payable forever. Stripe's own limit is 24
# hours and its minimum is 30 minutes.
STRIPE_SESSION_MINUTES = env_int("STRIPE_SESSION_MINUTES", 720)

# ---------------------------------------------------------------------------
# AI Financial Summary (apps.ai_summary).
#
# Off unless a key is present, same as Stripe above: an installation that never
# sets GROQ_API_KEY still shows every report exactly as it does today, just
# without an AI-written explanation on top of the figures.
#
# The provider is Groq (an OpenAI-compatible Chat Completions API) rather than
# OpenAI itself — see apps/ai_summary/ai_gateway.py for the one place that
# distinction actually matters.
# ---------------------------------------------------------------------------
GROQ_API_KEY = env("GROQ_API_KEY")
# A short, already-aggregated summary does not need a large model. Kept
# configurable so a cheaper or newer model can be swapped in without a
# code change.
AI_SUMMARY_MODEL = env("AI_SUMMARY_MODEL", "openai/gpt-oss-20b")
# A fixed one-hour window per user (apps/ai_summary/views.py), enforced with
# the same cache used for the dashboard above rather than a new dependency.
AI_SUMMARY_RATE_LIMIT_PER_HOUR = env_int("AI_SUMMARY_RATE_LIMIT_PER_HOUR", 5)
# How long an identical request (same user, dates and payload) reuses the
# stored result instead of spending a second AI call on unchanged figures.
AI_SUMMARY_DEDUP_WINDOW_SECONDS = env_int("AI_SUMMARY_DEDUP_WINDOW_SECONDS", 600)

# ---------------------------------------------------------------------------
# Locale (CFG-001, NFR-018). The company row carries the business timezone and
# base currency; these are the framework defaults behind it.
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Logging (NFR-016): application errors and posting failures are logged with
# their correlation id; passwords and secrets never are.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": env("APP_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        # Permission and CSRF denial tests intentionally exercise 403 paths.
        # Keep their production warnings, but allow CI to suppress expected
        # tracebacks without muting genuine application failures.
        "django.request": {
            "handlers": ["console"],
            "level": env("DJANGO_REQUEST_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
        "django.security.csrf": {
            "handlers": ["console"],
            "level": env("DJANGO_REQUEST_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# NFR-005 / ACC-007 production hardening. Automatic when DEBUG is off, so
# nobody has to remember to switch it on at deployment time.
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_HTTPONLY = True
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            # WhiteNoise's manifest storage, which is Django's own plus gzip
            # and brotli copies made once at collectstatic rather than per
            # request. The manifest half is the important half: it fingerprints
            # every file so it can be cached forever and still change instantly.
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    if env_bool("DJANGO_BEHIND_HTTPS_PROXY", False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
