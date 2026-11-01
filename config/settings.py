import os
from pathlib import Path
from dotenv import load_dotenv
import dj_database_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-fallback-key-change-in-production')

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

if not DEBUG and SECRET_KEY == 'django-insecure-dev-fallback-key-change-in-production':
    raise RuntimeError(
        "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off."
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if host.strip()
]

if DEBUG:
    # Local tunnel providers (cloudflared quick tunnels, ngrok, localtunnel)
    # mint a random public host on every run. Allow their suffixes in dev
    # only, so Paystack webhooks and callbacks can reach this machine without
    # editing env and restarting on each tunnel. Never enable in production:
    # the DEBUG-off guard above already refuses the default host list.
    ALLOWED_HOSTS += [
        suffix for suffix in (
            '.trycloudflare.com', '.ngrok.io', '.ngrok-free.app', '.loca.lt',
        )
        if suffix not in ALLOWED_HOSTS
    ]

if not DEBUG and ALLOWED_HOSTS == ['127.0.0.1', 'localhost']:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set when DJANGO_DEBUG is off.")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'escrow',
    'agent',
    'voice',
    'whatsapp',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# SQLite locally; Neon Postgres (via DATABASE_URL) when deployed with
# DEBUG off — e.g. Render. Keeps the single-node override for good measure.
if DEBUG:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': Path(os.environ.get('DJANGO_DB_PATH', BASE_DIR / 'db.sqlite3')),
        }
    }
else:
    DATABASES = {
        'default': dj_database_url.config(
            default=os.environ.get('DATABASE_URL'),
            conn_max_age=600,
            ssl_require=True,
        )
    }

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'trustcrow',
    }
}

# Passwordless OTP sessions: bound the window a stolen session stays useful.
SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE', '86400'))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Lagos'

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Not the manifest variant: the admin 500s on a missing manifest entry
        # until collectstatic has been run, which is an easy trap on a fresh
        # checkout. Fingerprinted assets buy little here since WhiteNoise serves
        # a single collected directory.
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Trustcrow integration settings -----------------------------------------

# Outgoing email (counterparty invitations). Console backend in DEBUG so
# links print to the log; configure SMTP for production delivery.
if DEBUG:
    EMAIL_BACKEND = os.environ.get(
        'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
else:
    EMAIL_BACKEND = os.environ.get(
        'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'Trustcrow <no-reply@trustcrow.example>')

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_dummy')
TERMII_API_KEY = os.environ.get('TERMII_API_KEY', 'dummy_key')
TERMII_EMAIL_CONFIG_ID = os.environ.get('TERMII_EMAIL_CONFIG_ID', 'default_config')

# WhatsApp Business Platform Cloud API.
WHATSAPP_ACCESS_TOKEN = os.environ.get('WHATSAPP_ACCESS_TOKEN', '')
WHATSAPP_PHONE_NUMBER_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '')
WHATSAPP_BUSINESS_ACCOUNT_ID = os.environ.get('WHATSAPP_BUSINESS_ACCOUNT_ID', '')
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.environ.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN', '')
WHATSAPP_APP_SECRET = os.environ.get('WHATSAPP_APP_SECRET', '')
WHATSAPP_API_VERSION = os.environ.get('WHATSAPP_API_VERSION', 'v21.0')

# Muse Voice Transcribe (Meta Model API). Separate key from Baseten;
# voice features degrade to typed input when unset.
MODEL_API_KEY = os.environ.get('MODEL_API_KEY', '')
ASR_BASE_URL = os.environ.get('ASR_BASE_URL', 'https://api.meta.ai')
ASR_MODEL = os.environ.get('ASR_MODEL', 'muse-voice-transcribe-1.0')
VOICE_MAX_BYTES = int(os.environ.get('VOICE_MAX_BYTES', str(8 * 1024 * 1024)))
VOICE_TIMEOUT = int(os.environ.get('VOICE_TIMEOUT', '60'))

# Baseten model inference (OpenAI-compatible endpoint). The model id is
# verified against the project account; change it via the environment.
BASETEN_API_KEY = os.environ.get('BASETEN_API_KEY', '')
BASETEN_BASE_URL = os.environ.get(
    'BASETEN_BASE_URL', 'https://inference.baseten.co/v1'
)
BASETEN_MODEL = os.environ.get(
    'BASETEN_MODEL', 'meta-models/muse-spark-open-1026'
)

# Bounded agent execution: max tool iterations per user message and the
# per-request timeout for a single inference call.
AGENT_MAX_ITERATIONS = int(os.environ.get('AGENT_MAX_ITERATIONS', '8'))
AGENT_REQUEST_TIMEOUT = int(os.environ.get('AGENT_REQUEST_TIMEOUT', '60'))

# Seconds before an outbound Paystack/Termii call is abandoned. Without this a
# slow gateway holds the user's request open indefinitely, which matters most on
# the payout path.
GATEWAY_HTTP_TIMEOUT = int(os.environ.get('GATEWAY_HTTP_TIMEOUT', '15'))

# Minimum gap between one-time codes sent to the same address for the same
# purpose, which bounds inbox spam driven from a known escrow code.
OTP_ISSUE_COOLDOWN_SECONDS = int(os.environ.get('OTP_ISSUE_COOLDOWN_SECONDS', '60'))

# Print one-time codes to the log while developing. Off by default so a
# credential can never leak to a shared console.
LOG_OTP_TO_CONSOLE = os.environ.get('LOG_OTP_TO_CONSOLE', 'False') == 'True'

# Deliver real Termii emails while DEBUG is on. The notifications module
# otherwise short-circuits to a mock in DEBUG (returns True without sending),
# which is why a correct TERMII_API_KEY still sends nothing locally. Turn this
# on only for live local testing — every OTP then costs a real Termii credit.
TERMII_SEND_IN_DEBUG = os.environ.get('TERMII_SEND_IN_DEBUG', 'False') == 'True'

# --- Security / deployment ---------------------------------------------------

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # Preloading is an opt-in commitment: once a browser sees it, the domain is
    # forced to HTTPS for months and the decision is hard to walk back.
    SECURE_HSTS_PRELOAD = os.environ.get('SECURE_HSTS_PRELOAD', 'False') == 'True'
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    X_FRAME_OPTIONS = 'DENY'
    CSRF_TRUSTED_ORIGINS = [
        origin.strip()
        for origin in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
        if origin.strip()
    ]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
    },
    'root': {'handlers': ['console'], 'level': os.environ.get('LOG_LEVEL', 'INFO')},
    'loggers': {
        'escrow': {
            'handlers': ['console'],
            'level': os.environ.get('LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}