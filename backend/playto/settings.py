"""Django settings for the Playto payout engine."""
from pathlib import Path
from urllib.parse import urlparse

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "django_q",
    "ledger",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "playto.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "playto.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB", default="playto"),
        "USER": config("POSTGRES_USER", default="playto"),
        "PASSWORD": config("POSTGRES_PASSWORD", default="playto"),
        "HOST": config("POSTGRES_HOST", default="127.0.0.1"),
        "PORT": config("POSTGRES_PORT", default="5432"),
        "CONN_MAX_AGE": 60,
    }
}

DATABASE_URL = config("DATABASE_URL", default="")
if DATABASE_URL:
    u = urlparse(DATABASE_URL)
    DATABASES["default"].update({
        "NAME": u.path.lstrip("/"),
        "USER": u.username or "",
        "PASSWORD": u.password or "",
        "HOST": u.hostname or "",
        "PORT": str(u.port or 5432),
    })
    if "sslmode" not in DATABASES["default"].get("OPTIONS", {}):
        DATABASES["default"].setdefault("OPTIONS", {})["sslmode"] = config(
            "POSTGRES_SSLMODE", default="require"
        )

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
}

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "idempotency-key",
    "x-merchant-id",
]

# Django-Q2 uses Postgres as the broker — no Redis required.
Q_CLUSTER = {
    "name": "playto",
    "workers": config("Q_WORKERS", default=2, cast=int),
    "recycle": 500,
    "timeout": 60,
    "retry": 120,
    "compress": True,
    "save_limit": 250,
    "queue_limit": 500,
    "cpu_affinity": 1,
    "label": "Playto Queue",
    "orm": "default",
    "poll": 1,
    "catch_up": False,
}

PAYOUT_SUCCESS_RATE = config("PAYOUT_SUCCESS_RATE", default=0.70, cast=float)
PAYOUT_FAIL_RATE = config("PAYOUT_FAIL_RATE", default=0.20, cast=float)
PAYOUT_HANG_RATE = config("PAYOUT_HANG_RATE", default=0.10, cast=float)
PAYOUT_STUCK_AFTER_SECONDS = config("PAYOUT_STUCK_AFTER_SECONDS", default=30, cast=int)
PAYOUT_MAX_ATTEMPTS = config("PAYOUT_MAX_ATTEMPTS", default=3, cast=int)
IDEMPOTENCY_TTL_HOURS = config("IDEMPOTENCY_TTL_HOURS", default=24, cast=int)
