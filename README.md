# Trustcrow

Passwordless escrow for buyers and sellers. Create a deal, verify both inboxes by email OTP, fund with Paystack, then track dispatch → delivery → inspection → payout.

Built with Django 6 + SQLite (local) / Postgres (prod) + Paystack + Termii email OTP.

## How it works

1. **Create** — creator sets title, amount, counterparty email, role (buyer/seller), inspection days, and who pays the service fee. Gets a shareable escrow code.
2. **Verify** — creator verifies via 6-digit email OTP, then counterparty verifies the same way.
3. **Fund** — buyer pays via Paystack (card / bank / transfer). Webhook confirms payment.
4. **Dispatch** — seller confirms dispatch → `in_transit`.
5. **Receipt + inspection** — buyer confirms receipt → inspection window starts (default 3 days, auto-release via `auto_release_escrow`).
6. **Release / dispute** — buyer releases funds → Paystack transfer to seller, or either party raises a dispute.

State machine (`escrow/models.py::EscrowContract.TRANSITIONS`):

```
pending_verification → awaiting_counterparty → awaiting_funding → awaiting_dispatch
  → in_transit → in_inspection → pending_payout → completed
  (disputed / cancelled reachable from funded states)
```

Other key features:

- Passwordless auth via email OTP sessions (no accounts/passwords)
- `My Escrow` lookup: email + OTP to list your deals
- Seller vault (bank account details masked from buyer until funding)
- Live contract panel polling via HTMX (15s)
- Tiered service-fee preview on the create form
- Red + black design system (see `design.md`, gitignored working file)

## Tech stack

- Python 3.14, Django 6.1
- Gunicorn + WhiteNoise, `dj-database-url`
- Paystack (payments + transfers), Termii (email OTP)
- SQLite locally, Postgres (e.g. Neon) when `DJANGO_DEBUG=False`
- Timezone: `Africa/Lagos`

## Quickstart

```bash
# 1. Clone + venv
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in DJANGO_SECRET_KEY, PAYSTACK_SECRET_KEY, TERMII_* (see below)

# 3. Migrate + run
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/

Useful commands:

```bash
python manage.py test                # run suite incl. UI SmokeTest guardrails
python manage.py auto_release_escrow  # release expired inspections (Procfile worker)
python manage.py collectstatic       # prod static build
```

## Environment

Copy `.env.example` → `.env`. Never commit `.env`.

| Var | Required | Default | Notes |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | prod only | dev fallback | Must be set when `DJANGO_DEBUG=False` |
| `DJANGO_DEBUG` | — | `False` | Set `True` locally. SQLite is used when `True` |
| `DJANGO_ALLOWED_HOSTS` | prod only | `127.0.0.1,localhost` | Comma-separated |
| `DATABASE_URL` | prod only | — | Postgres URL (Neon/Render). `ssl_require=True` |
| `PAYSTACK_SECRET_KEY` | yes | `sk_test_dummy` | `sk_test_…` for testing, `sk_live_…` for live |
| `TERMII_API_KEY` | yes | `dummy_key` | Termii API key for email OTP |
| `TERMII_EMAIL_CONFIG_ID` | yes | `default_config` | Termii email config/channel ID |
| `GATEWAY_HTTP_TIMEOUT` | — | `15` | Seconds before Paystack/Termii calls abort |
| `OTP_ISSUE_COOLDOWN_SECONDS` | — | `60` | Min gap between OTPs to same address/purpose |
| `LOG_OTP_TO_CONSOLE` | — | `False` | Set `True` locally to print codes to logs |
| `LOG_LEVEL` | — | `INFO` | Root + `escrow` logger level |
| `CSRF_TRUSTED_ORIGINS` | prod only | — | e.g. `https://escrow.example.com` |
| `SECURE_SSL_REDIRECT` | — | `True` (prod) | Set `False` only behind a TLS-terminating proxy that needs it |

## Routes

| Path | Name | Purpose |
| --- | --- | --- |
| `/` | `home` | Landing |
| `/create/` | `create_contract` | New escrow |
| `/join/` | `join_escrow_page` | Join via code |
| `/verify/<code>/` | `verify_otp` | Creator OTP |
| `/escrow/<code>/` | `contract_detail` | Detail + actions (HTMX panel) |
| `/escrow/<code>/verify/` | `counterparty_verify_otp` | Counterparty OTP |
| `/escrow/<code>/pay/` | `initiate_payment` | Start Paystack checkout |
| `/escrow/<code>/callback/` | `payment_callback` | Paystack return |
| `/webhooks/paystack/` | `paystack_webhook` | Payment confirmation |
| `/escrow/<code>/dispatch/` `/receipt/` `/release/` `/dispute/` `/seller-dispute/` `/cancel/` `/vault/` | — | Lifecycle actions |
| `/my-escrow/` `/my-escrow/verify/` `/my-escrow/list/` | `my_escrow*` | Email+OTP deal lookup |

Admin: `/admin/`

## Project structure

```
config/            # settings, urls, wsgi/asgi
escrow/            # models, views, forms, services, gateways/, notifications.py, bank_verification.py
templates/         # base.html (design tokens), home.html, escrow/
manage.py
requirements.txt
Procfile           # web: gunicorn / worker: auto_release_escrow
runtime.txt        # python-3.14.5
```

Design tokens, components, and responsive rules live in `templates/base.html`; `design.md` is the local UI source of truth (gitignored) and `escrow/tests.py::SmokeTest` enforces its guardrails — run `python manage.py test` before pushing UI changes.

## Deployment (Render + Neon style)

1. Set `DJANGO_DEBUG=False`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CSRF_TRUSTED_ORIGINS`, Paystack/Termii keys.
2. Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
3. Start: `gunicorn config.wsgi:application`
4. Worker: `python manage.py auto_release_escrow` (cron or Render worker)
5. Prod enforces `Secure` cookies, HSTS (1yr), `SECURE_PROXY_SSL_HEADER`, `X-Frame-Options: DENY`.

## Security notes

- No passwords — OTP-gated sessions expire after `SESSION_COOKIE_AGE` (default 24h).
- Emails normalized; action buttons gate on session-verified emails.
- Post-funding cancel requires ops refund check (see admin warning in `escrow/models.py`).
- Keep `LOG_OTP_TO_CONSOLE=False` outside local dev.
