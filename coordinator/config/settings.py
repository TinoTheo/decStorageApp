"""
Coordinator settings.

Everything that changes between environments comes from environment variables,
so the same image runs locally, on staging and in production. See
`.env.example` at the repo root for the full list.
"""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    return int(os.environ.get(name, default))


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "accounts",
    "files",
    "storage",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite when DATABASE_URL is unset keeps tests and quick local runs dependency-free.
# Staging and production set DATABASE_URL to Postgres.
DATABASES = {
    "default": dj_database_url.config(
        # as_posix() keeps the URL valid on Windows (C:/... instead of C:\...).
        default=f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}",
        conn_max_age=env_int("DATABASE_CONN_MAX_AGE", 60),
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = []  # The server only ever sees a derived auth key, never the passphrase.

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# The browser crypto library lives in web/src and is served as-is, so the demo page
# and any future frontend use the exact same module the tests exercise.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [("dstore", REPO_ROOT / "web" / "src"), BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedStaticFilesStorage"
        )
    },
}

WHITENOISE_USE_FINDERS = DEBUG

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["accounts.authentication.HashedTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Behind Caddy every request arrives from the proxy's address. NUM_PROXIES=1
    # makes rate limits key on the client IP from X-Forwarded-For instead.
    "NUM_PROXIES": env_int("NUM_PROXIES", 0) or None,
    "DEFAULT_THROTTLE_RATES": {
        "auth": os.environ.get("AUTH_THROTTLE_RATE", "120/minute"),
        "auth_user": os.environ.get("AUTH_USER_THROTTLE_RATE", "10/minute"),
    },
}

# --- Storage ---------------------------------------------------------------

# Fixed segment size. The browser splits files into segments of exactly this many
# plaintext bytes (the last one may be shorter); each encrypted segment is 16 bytes
# larger because of the AES-GCM tag.
SEGMENT_SIZE = env_int("SEGMENT_SIZE", 4 * 1024 * 1024)
GCM_TAG_BYTES = 16
MAX_FILE_SIZE = env_int("MAX_FILE_SIZE", 1024 * 1024 * 1024)  # 1 GB for the MVP

# Django refuses to read request bodies above this size; one encrypted segment
# plus headroom must fit.
DATA_UPLOAD_MAX_MEMORY_SIZE = SEGMENT_SIZE + 64 * 1024

# Where encrypted segments live. M1 stages them on the coordinator's disk;
# M2 swaps in a backend that pins them on storage nodes through Kubo.
SEGMENT_STORE = {
    "BACKEND": os.environ.get("SEGMENT_STORE_BACKEND", "storage.backends.LocalSegmentStore"),
    "OPTIONS": {"root": os.environ.get("STAGING_DIR", str(BASE_DIR / "staging"))},
}

# --- Key derivation ----------------------------------------------------------

# PBKDF2-SHA256 iterations the browser must use. The server rejects sign-ups below
# the minimum. 600k matches current OWASP guidance for PBKDF2-SHA256.
KDF_ITERATIONS = env_int("KDF_ITERATIONS", 600_000)
KDF_MIN_ITERATIONS = env_int("KDF_MIN_ITERATIONS", 600_000)

# --- Production hardening ----------------------------------------------------

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0)

SECURE_REFERRER_POLICY = "no-referrer"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
