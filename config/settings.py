import os
import sys
from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

SECRET_KEY = env("DJANGO_SECRET_KEY", default="change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "blog",
    "orders",
    "drivers.apps.DriversConfig",
    "dispatch",
    "pricing",
    "tracking",
    "bot",
    "analytics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'davomat'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', '41552145'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', 5432),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "uz"
TIME_ZONE = "Asia/Tashkent"

USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_GROUP_ID = env("TELEGRAM_GROUP_ID", default="")
_tg_topic_raw = (env("TELEGRAM_GROUP_MESSAGE_THREAD_ID", default="") or "").strip()
try:
    TELEGRAM_GROUP_MESSAGE_THREAD_ID: int | None = int(_tg_topic_raw) if _tg_topic_raw else None
except ValueError:
    TELEGRAM_GROUP_MESSAGE_THREAD_ID = None
TELEGRAM_OPS_GROUP_ID = env("TELEGRAM_OPS_GROUP_ID", default="")
_tg_ops_topic_raw = (env("TELEGRAM_OPS_GROUP_MESSAGE_THREAD_ID", default="") or "").strip()
try:
    TELEGRAM_OPS_GROUP_MESSAGE_THREAD_ID: int | None = int(_tg_ops_topic_raw) if _tg_ops_topic_raw else None
except ValueError:
    TELEGRAM_OPS_GROUP_MESSAGE_THREAD_ID = None
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
# True bo‘lsa, reys xabariga Yandex marshrut havolalari qo‘shiladi; aks holda faqat Telegram pinlari
TRIP_MAP_SHOW_YANDEX_LINKS = env.bool("TRIP_MAP_SHOW_YANDEX_LINKS", default=False)
# HTTPS asosiy domen (masalan https://bot.sizningdomen.uz) — Telegram Web App marshrut xaritasi uchun majburiy
TELEGRAM_WEBAPP_BASE_URL = env("TELEGRAM_WEBAPP_BASE_URL", default="").strip().rstrip("/")

SLA_ESCALATION_THRESHOLDS_MINUTES = [
    int(value) for value in env.list("SLA_ESCALATION_THRESHOLDS_MINUTES", default=["15", "30", "60"])
]
IMPOSSIBLE_SPEED_KMH = env.int("IMPOSSIBLE_SPEED_KMH", default=130)
ROUTE_DEVIATION_DEFAULT_THRESHOLD_KM = env.float("ROUTE_DEVIATION_DEFAULT_THRESHOLD_KM", default=3.0)
GPS_MAX_DISTANCE_FROM_ORDER_KM = env.int("GPS_MAX_DISTANCE_FROM_ORDER_KM", default=1500)
# Telegram Live Location: har bir yangilanish DB ga yoziladi; oralig‘ (soniya). 0 = har bir tahrirni saqlash.
TELEGRAM_LIVE_LOCATION_SAVE_INTERVAL_SEC = env.int("TELEGRAM_LIVE_LOCATION_SAVE_INTERVAL_SEC", default=5)
# Web xaritada marshrut nuqtalari (oxirgi N ta)
ORDER_LIVE_TRAIL_MAX_POINTS = env.int("ORDER_LIVE_TRAIL_MAX_POINTS", default=400)
# Live flot xaritasi: har bir reys uchun iz (har buyurtmaga alohida, kamroq nuqta)
FLEET_LIVE_TRAIL_MAX_POINTS = env.int("FLEET_LIVE_TRAIL_MAX_POINTS", default=100)

# Lokatsiya fraud (detect_location_fraud_task)
LOCATION_FRAUD_IDLE_DISTANCE_KM = env.float("LOCATION_FRAUD_IDLE_DISTANCE_KM", default=0.03)
LOCATION_FRAUD_IDLE_SAME_POINT_COUNT = env.int("LOCATION_FRAUD_IDLE_SAME_POINT_COUNT", default=5)
LOCATION_FRAUD_IDLE_ALERT_THRESHOLD_MINUTES = env.int("LOCATION_FRAUD_IDLE_ALERT_THRESHOLD_MINUTES", default=60)
LIVE_TRACK_REQUIRED_AFTER_KETDIK_SEC = env.int("LIVE_TRACK_REQUIRED_AFTER_KETDIK_SEC", default=120)
LIVE_TRACK_REMINDER_COOLDOWN_SEC = env.int("LIVE_TRACK_REMINDER_COOLDOWN_SEC", default=600)

# Reys hajm kamomadi (kg): operator webda loaded/delivered kiritadi.
SHORTAGE_WARNING_KG = env.float("SHORTAGE_WARNING_KG", default=70)
SHORTAGE_PENALTY_KG = env.float("SHORTAGE_PENALTY_KG", default=100)
SHORTAGE_PENALTY_POINTS_70_99 = env.int("SHORTAGE_PENALTY_POINTS_70_99", default=2)
SHORTAGE_PENALTY_POINTS_100_199 = env.int("SHORTAGE_PENALTY_POINTS_100_199", default=5)
SHORTAGE_PENALTY_POINTS_200_PLUS = env.int("SHORTAGE_PENALTY_POINTS_200_PLUS", default=10)
SHORTAGE_RATING_MIN = env.float("SHORTAGE_RATING_MIN", default=0)

# Hujjat muddati yaqinlashgan (kunlar)
DRIVER_DOC_EXPIRY_NEAR_DAYS = env.int("DRIVER_DOC_EXPIRY_NEAR_DAYS", default=30)

# Tender davomiyligi (daqiqa)
TENDER_DURATION_MIN_MINUTES = env.int("TENDER_DURATION_MIN_MINUTES", default=3)
TENDER_DURATION_MAX_MINUTES = env.int("TENDER_DURATION_MAX_MINUTES", default=10)

# Oylik hisobot: gross_revenue (= client_price yig‘indisi) / driver_cost; faqat COMPLETED. Shofir: klientdan tushum yo‘q bo‘lsa gross_revenue ~0.
ANALYTICS_REVENUE_SUM_COMPLETED_ONLY = env.bool("ANALYTICS_REVENUE_SUM_COMPLETED_ONLY", default=True)

# ISSUE/CANCELED: narx/fee va ledgerlarni nolga qaytarish (False = quote/moliya saqlanadi)
ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED = env.bool("ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED", default=True)

# Web UI: ro'yxatlar sahifasi
ORDERS_LIST_PER_PAGE = env.int("ORDERS_LIST_PER_PAGE", default=25)
ANALYTICS_CLIENTS_RATING_PAGE_SIZE = env.int("ANALYTICS_CLIENTS_RATING_PAGE_SIZE", default=20)
SPLIT_SHIPMENT_MAX_PARTS = env.int("SPLIT_SHIPMENT_MAX_PARTS", default=10)

# REST API pagination
API_PAGE_SIZE = env.int("API_PAGE_SIZE", default=25)
API_MAX_PAGE_SIZE = env.int("API_MAX_PAGE_SIZE", default=100)

# GET /metrics/ (Prometheus scrape); o'chiq bo'lsa 404
PROMETHEUS_METRICS_ENABLED = env.bool("PROMETHEUS_METRICS_ENABLED", default=False)

# Logging (LOG_JSON=True bo'lsa JSON qatorlari)
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=False)

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_BEAT_SCHEDULE = {
    "sla-escalation-every-5-min": {
        "task": "analytics.tasks.check_sla_escalations_task",
        "schedule": 300.0,
    },
    "nightly-reconcile": {
        "task": "analytics.tasks.nightly_reconcile_task",
        "schedule": 86400.0,
    },
    "monthly-report-scheduler": {
        "task": "analytics.tasks.monthly_report_scheduler_task",
        "schedule": 86400.0,
    },
    "driver-doc-expiry-daily": {
        "task": "analytics.tasks.notify_driver_document_expiry_task",
        "schedule": 86400.0,
    },
    "check-live-track-every-minute": {
        "task": "analytics.tasks.check_live_track_required_task",
        "schedule": 60.0,
    },
}

_RUNNING_TESTS = any(arg in {"test", "tests"} for arg in sys.argv)

CACHE_REDIS_URL = env("REDIS_CACHE_URL", default="redis://localhost:6379/2")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache" if _RUNNING_TESTS else "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "shofir-cache" if _RUNNING_TESTS else CACHE_REDIS_URL,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "api.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": API_PAGE_SIZE,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/minute",
        "user": "300/minute",
    },
}

_CONSOLE_LOG_FORMATTER = "json" if LOG_JSON else "verbose"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(name)s %(message)s",
        },
        "json": {
            "()": "common.json_logging.JsonLogFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": _CONSOLE_LOG_FORMATTER,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
}

try:
    from .settings_dev import *
except ImportError:
    pass
