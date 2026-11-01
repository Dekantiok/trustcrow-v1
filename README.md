# Trustcrow

Passwordless escrow for buyers and sellers. Create a deal, verify both inboxes by email OTP, fund with Paystack, then track dispatch → delivery → inspection → payout.

Three interfaces, one backend: the manual web forms, the website AI assistant (text + voice), and the WhatsApp AI assistant (text + voice notes) all run on the same escrow engine, the same validation, and the same money rules.

Built with Django + SQLite (local) / Postgres (prod) + Paystack + Termii + Baseten (chat) + Meta Model API (voice).

## How it works

1. **Create** — creator sets title, amount, counterparty email, role (buyer/seller), inspection days, and who pays the service fee. The draft is assembled by hand, by the AI chat, or by voice.
2. **Verify creator** — a 6-digit email OTP proves the creator's inbox. The escrow becomes `awaiting_counterparty` and the counterparty is emailed a join link with the code prefilled.
3. **Counterparty joins** — they open the join link, prove their own inbox with an OTP, and accept the terms. The buyer is notified (WhatsApp push when linked, AI chat brief on next visit, live dashboard panel) and the escrow moves to `awaiting_funding`.
4. **Fund** — buyer pays via Paystack (card / bank / transfer) from the dashboard button or from a payment link the AI fetches in chat. Webhook + callback fallback confirm against expected amount, currency, and reference.
5. **Dispatch** — seller confirms dispatch in chat ("dispatched") or dashboard → `in_transit`. Buyer is notified.
6. **Receipt + inspection** — buyer confirms receipt → inspection window starts (default 3 days, auto-releases via the `auto_release_escrow` worker when eligible). Both sides are notified with the deadline.
7. **Release / dispute** — buyer says "release" in chat or clicks release → Paystack transfer to the seller's verified vault account. Either party can dispute instead; disputes freeze everything until ops resolves in admin.
8. **Done** — seller and buyer both get payout confirmation.

State machine (`escrow/models.py::EscrowContract.TRANSITIONS`):

```
pending_verification → awaiting_counterparty → awaiting_funding → awaiting_dispatch
  → in_transit → in_inspection → pending_payout → completed
  (disputed / cancelled reachable from funded states)
```

Money rules: amounts stored as decimals, charged in kobo; tiered service fee (1,500 / 2,000 / 2,500 / 5,000 / 10,000 by band); buyer pays checkout total, seller receives net terms; every funding confirmation and payout is idempotent (conditional updates, single provider reference, `uncertain` parking on timeouts — never a blind retry).

## Interfaces

- **Manual web** — `/create/`, `/join/`, `/escrow/<code>/` detail with HTMX live panel (15s poll), My Escrow lookup by email+OTP, seller bank vault (account numbers masked until funding).
- **Website AI assistant** — `/assistant/` chat (also embedded on `/`). Text, mic button (MediaRecorder → Muse transcription → normal agent turn), pending-action panel ("reply yes or discard"), New chat button, typed `discard` to drop a proposal. Bare 6-digit messages are treated as OTPs, never sent to the model.
- **WhatsApp AI assistant** — `/webhooks/whatsapp/` Cloud API webhook. Text and voice notes flow into the same bounded agent loop; email OTP verification binds the sender number to a verified identity (the number itself is never identity). Non-text messages get a gentle nudge back to text/voice.

The AI handles the whole arc conversationally: draft → confirm → code → join nudge → payment link → dispatch/receipt confirmations → release → payout confirmation, with status briefs ("since your last visit…") and WhatsApp pushes at every handoff.

## Tech stack

- Python 3.14, Django (templates + HTMX, no separate frontend)
- Gunicorn + WhiteNoise, `dj-database-url`
- Paystack (payments + transfers), Termii (email OTP), SMTP/email (counterparty invites)
- Baseten-hosted chat model (OpenAI-compatible), Meta Muse Voice Transcribe (STT)
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
# Fill in DJANGO_SECRET_KEY, PAYSTACK_SECRET_KEY, TERMII_*, MODEL_API_KEY (voice)

# 3. Migrate + run
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/

Useful commands:

```bash
python manage.py test                # full suite incl. UI SmokeTest guardrails
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
| `TERMII_SEND_IN_DEBUG` | — | `False` | Set `True` for live local OTP delivery (costs credit) |
| `BASETEN_API_KEY` | for AI chat | — | Chat model inference key |
| `BASETEN_BASE_URL` | — | `https://inference.baseten.co/v1` | OpenAI-compatible endpoint |
| `BASETEN_MODEL` | — | `meta-models/muse-spark-open-1026` | Verified against project account |
| `AGENT_MAX_ITERATIONS` | — | `8` | Tool-call cap per chat turn |
| `AGENT_REQUEST_TIMEOUT` | — | `60` | Seconds per inference call |
| `MODEL_API_KEY` | for voice | — | Meta Model API key (Muse transcribe; ~$0.18/hr audio) |
| `ASR_BASE_URL` | — | `https://api.meta.ai` | Transcription endpoint |
| `ASR_MODEL` | — | `muse-voice-transcribe-1.0` | STT model |
| `VOICE_MAX_BYTES` | — | `8388608` | Upload cap per voice note |
| `VOICE_TIMEOUT` | — | `60` | Transcription request timeout |
| `WHATSAPP_ACCESS_TOKEN` | for WhatsApp | — | Cloud API token |
| `WHATSAPP_PHONE_NUMBER_ID` | for WhatsApp | — | Sending number ID |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | — | — | Meta business account |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | for WhatsApp | — | Must match Meta dashboard |
| `WHATSAPP_APP_SECRET` | prod | — | Enforces `X-Hub-Signature-256` validation |
| `WHATSAPP_API_VERSION` | — | `v21.0` | Graph API version |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `EMAIL_USE_TLS` | prod | console backend in DEBUG | SMTP for invitation mail |
| `DEFAULT_FROM_EMAIL` | — | `Trustcrow <no-reply@…>` | Invitation sender |
| `GATEWAY_HTTP_TIMEOUT` | — | `15` | Seconds before Paystack/Termii/WhatsApp calls abort |
| `OTP_ISSUE_COOLDOWN_SECONDS` | — | `60` | Min gap between OTPs to same address/purpose |
| `LOG_OTP_TO_CONSOLE` | — | `False` | Set `True` locally to print codes to logs |
| `LOG_LEVEL` | — | `INFO` | Root + `escrow` logger level |
| `CSRF_TRUSTED_ORIGINS` | prod only | — | e.g. `https://escrow.example.com` |
| `SECURE_SSL_REDIRECT` | — | `True` (prod) | Set `False` only behind a TLS-terminating proxy that needs it |

## Routes

| Path | Name | Purpose |
| --- | --- | --- |
| `/` | `home` | Landing + embedded AI chat |
| `/create/` | `create_contract` | New escrow |
| `/join/` | `join_escrow_page` | Join via code (`?code=` prefills) |
| `/verify/<code>/` | `verify_otp` | Creator OTP → invites counterparty |
| `/escrow/<code>/` | `contract_detail` | Detail + actions (HTMX panel) |
| `/escrow/<code>/verify/` | `counterparty_verify_otp` | Counterparty OTP → opens funding |
| `/escrow/<code>/pay/` | `initiate_payment` | Start Paystack checkout |
| `/escrow/<code>/callback/` | `payment_callback` | Paystack return (verification fallback) |
| `/webhooks/paystack/` | `paystack_webhook` | Payment confirmation (signature-checked, deduped) |
| `/webhooks/whatsapp/` | `whatsapp:webhook` | WhatsApp verify + inbound + statuses |
| `/assistant/` | `agent:chat` | Website AI assistant |
| `/assistant/send/` | `agent:chat_send` | Chat turn |
| `/assistant/voice/` | `agent:chat_voice` | Voice note → transcript → turn |
| `/assistant/transcribe/` | `agent:transcribe_field` | Field-level transcription (JSON) |
| `/assistant/new/` | `agent:chat_new` | Fresh thread |
| `/escrow/<code>/dispatch/` `/receipt/` `/release/` `/dispute/` `/seller-dispute/` `/cancel/` `/vault/` | — | Lifecycle actions |
| `/my-escrow/` `/my-escrow/verify/` `/my-escrow/list/` | `my_escrow*` | Email+OTP deal lookup |

Admin: `/admin/` (escrow ops, OTPs, vaults, payments, payouts, refunds, webhooks, audit trail)

## Project structure

```
config/            # settings, urls, wsgi/asgi
escrow/            # models, views, forms, services, lifecycle.py, invitations.py,
                   # gateways/, notifications.py, bank_verification.py, tests
agent/             # chat loop, Baseten client, tool registry, views
whatsapp/          # Cloud API adapter, webhook, conversation flow
voice/             # Muse transcription (ffmpeg convert + API client)
templates/         # base.html (design system), home.html, agent/, escrow/
static/js/         # voice.js (mic recorder shared by chat + forms)
manage.py
requirements.txt
Procfile           # web: gunicorn / worker: auto_release_escrow
runtime.txt
```

Design tokens, components, and responsive rules live in `templates/base.html`; `design.md` is the local UI source of truth (gitignored) and `escrow/tests.py::SmokeTest` enforces its guardrails — run `python manage.py test` before pushing UI changes.

## The AI agent

Bounded tool-using loop (`agent/loop.py`): user message → model → approved tool call → backend result → reply. 13 tools (`agent/registry.py`), all authenticated against the session-verified email and the escrow state machine — model arguments are untrusted input.

- Consequential tools (create, invite, pay, dispatch/receipt, release, dispute) need exact fresh confirmation; the loop replays the identical call after "yes".
- OTPs are consumed by the backend, never the model; codes are redacted from model context.
- Model outages degrade to the manual forms; nothing is ever claimed from conversation alone.

## Voice notes

Browser mic / WhatsApp audio → `voice/transcriber.py`: ffmpeg (pip-bundled binary, no apt) converts to 16-bit mono WAV → Muse Voice Transcribe (`PUSH_TO_TALK`, English bias, escrow vocabulary). Results feed the normal agent turn (chat) or fill form fields; failures degrade to "type instead". Throttled (20/hr per session) and size-capped since transcription is metered.

## Deployment (Render + Neon style)

1. Set `DJANGO_DEBUG=False`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CSRF_TRUSTED_ORIGINS`, Paystack/Termii/Model/WhatsApp keys, SMTP.
2. Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
3. Start: `gunicorn config.wsgi:application`
4. Worker: `python manage.py auto_release_escrow` (cron or Render worker)
5. Register webhooks: Paystack dashboard → `/webhooks/paystack/`; Meta dashboard → `/webhooks/whatsapp/` (+ verify token).
6. Run the test-mode end-to-end (create → join → test card → dispatch → receipt → release) before going live.
7. Prod enforces `Secure` cookies, HSTS (1yr), `SECURE_PROXY_SSL_HEADER`, `X-Frame-Options: DENY`.

## Security notes

- No passwords — OTP-gated sessions expire after `SESSION_COOKIE_AGE` (default 24h); OTPs hashed, short-lived, attempt-limited, cooldown-guarded, single-use.
- WhatsApp numbers are endpoints, not identity; verified email is the actor everywhere.
- Webhook signatures validated (Paystack, WhatsApp); duplicate events ignored via unique constraints.
- Amount + currency verified on every funding confirmation; payouts idempotent with `uncertain` parking (never blind-retry a timeout).
- Post-funding cancel requires ops refund check (see admin warning in `escrow/models.py`).
- Agent cannot release funds, resolve disputes, or bypass auth; prompt-injection and tool-spoofing covered by tests.
- Keep `LOG_OTP_TO_CONSOLE=False` outside local dev.
