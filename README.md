# Trustcrow

Passwordless escrow for buyers and sellers. Create a deal, verify both inboxes by email OTP, fund with Paystack, then track dispatch → delivery → inspection → payout.

Three interfaces, one backend: the manual web forms, the website AI assistant (text + voice), and the WhatsApp AI assistant (text + voice notes) all run on the same escrow engine, the same validation, and the same money rules.

Built with Django + SQLite (local) / Postgres (prod) + Paystack + Termii + Baseten (chat) + Meta Model API (voice).

## How it works

1. **Create**, creator sets title, amount, counterparty email, role (buyer/seller), inspection days, and who pays the service fee. The draft is assembled by hand, by the AI chat, or by voice.
2. **Verify creator**, a 6-digit email OTP proves the creator's inbox. The escrow becomes `awaiting_counterparty` and the counterparty is emailed a join link with the code prefilled.
3. **Counterparty joins**, they open the join link, prove their own inbox with an OTP, and accept the terms. The buyer is notified (WhatsApp push when linked, AI chat brief on next visit, live dashboard panel) and the escrow moves to `awaiting_funding`.
4. **Fund**, buyer pays via Paystack (card / bank / transfer) from the dashboard button or from a payment link the AI fetches in chat. Webhook + callback fallback confirm against expected amount, currency, and reference.
5. **Dispatch**, seller confirms dispatch in chat ("dispatched") or dashboard → `in_transit`. Buyer is notified.
6. **Receipt + inspection**, buyer confirms receipt → inspection window starts (default 3 days, auto-releases via the `auto_release_escrow` worker when eligible). Both sides are notified with the deadline.
7. **Release / dispute**, buyer says "release" in chat or clicks release → Paystack transfer to the seller's verified vault account. Either party can dispute instead; disputes freeze everything until ops resolves in admin.
8. **Done**, seller and buyer both get payout confirmation.

State machine (`escrow/models.py::EscrowContract.TRANSITIONS`):

```
pending_verification → awaiting_counterparty → awaiting_funding → awaiting_dispatch
  → in_transit → in_inspection → pending_payout → completed
  (disputed / cancelled reachable from funded states)
```

Money rules: amounts stored as decimals, charged in kobo; tiered service fee (1,500 / 2,000 / 2,500 / 5,000 / 10,000 by band); buyer pays checkout total, seller receives net terms; every funding confirmation and payout is idempotent (conditional updates, single provider reference, `uncertain` parking on timeouts, never a blind retry).

## Interfaces

- **Manual web**, `/create/`, `/join/`, `/escrow/<code>/` detail with HTMX live panel (15s poll), My Escrow lookup by email+OTP, seller bank vault (account numbers masked until funding).
- **Website AI assistant**, `/assistant/` chat (also embedded on `/`). Text, mic button (MediaRecorder → Muse transcription → normal agent turn), pending-action panel ("reply yes or discard"), New chat button, typed `discard` to drop a proposal. Bare 6-digit messages are treated as OTPs, never sent to the model.
- **WhatsApp AI assistant**, `/webhooks/whatsapp/` Cloud API webhook. Text and voice notes flow into the same bounded agent loop; email OTP verification binds the sender number to a verified identity (the number itself is never identity). Non-text messages get a gentle nudge back to text/voice.

The AI handles the whole arc conversationally: draft → confirm → code → join nudge → payment link → dispatch/receipt confirmations → release → payout confirmation. Lifecycle updates are pushed to WhatsApp and into the other party's open webchat (about 10 second poll) at every handoff.

## Tech stack

- Python 3.14, Django (templates + HTMX, no separate frontend)
- Gunicorn + WhiteNoise, `dj-database-url`
- Paystack (payments + transfers), Termii (email OTP), SMTP/email (counterparty invites)
- Baseten-hosted chat model (OpenAI-compatible), Meta Muse Voice Transcribe (STT)
- SQLite locally, Postgres (e.g. Neon) when `DJANGO_DEBUG=False`
- Timezone: `Africa/Lagos`

## Quickstart

You need Python 3.12 or newer (the project runtime is Python 3.14), Git, and a Baseten API key if you want to test the AI assistant. For the quickest evaluation, use Demo mode below. It needs no Termii or Paystack keys.

```bash
# 1. Clone + venv
git clone https://github.com/Dekantiok/trustcrow-v1.git
cd trustcrow-v1
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# For judging, paste the Demo mode .env shown in the next section.
# For real integrations, fill in PAYSTACK_SECRET_KEY, TERMII_*, and the other keys.

# 3. Migrate + run (migrate is mandatory on every fresh checkout)
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ and check:

1. The home page loads with a blue Demo mode banner when `DEMO_MODE=True`.
2. The chat answers. If it says the AI is unavailable, `BASETEN_API_KEY` is missing or wrong in `.env`. Restart the server after changing `.env`.
3. The full judge script in Demo mode below completes and the escrow ends as `completed`.

Useful commands:

```bash
python manage.py test                # full suite incl. UI SmokeTest guardrails
python manage.py auto_release_escrow  # release expired inspections (Procfile worker)
python manage.py collectstatic       # prod static build
python manage.py flush               # wipe local test data and start again (asks first)
```

## Demo mode (for judges)

Demo mode lets you run the complete escrow circle locally with **no Termii account and no Paystack account**. Only those two integrations are simulated. The AI, voice transcription, escrow rules, confirmations, bank-name checks, notifications, and audit trail all run for real.

### Turn it on

Put this in `.env`, then restart `runserver`:

```bash
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=local-dev-secret-change-me
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost

# The only keys you must supply yourself for AI and voice:
BASETEN_API_KEY=your-baseten-key
BASETEN_BASE_URL=https://inference.baseten.co/v1
BASETEN_MODEL=meta-models/muse-spark-open-1026
MODEL_API_KEY=your-meta-model-api-key

# Demo switches. Leave every TERMII_* and PAYSTACK_* line blank or dummy.
DEMO_MODE=True
DEMO_OTP_CODE=123456
```

Notes:

- `BASETEN_API_KEY` powers the AI chat. Without it the site still loads and the manual forms still work, but the assistant replies that it is temporarily unavailable.
- `MODEL_API_KEY` powers voice notes only. Without it, typing works everywhere and voice asks you to type instead.
- WhatsApp is optional for local judging. It needs a Meta WhatsApp app plus a public HTTPS tunnel (see Deployment). Skip it locally unless you are specifically scoring WhatsApp.
- Bank account resolution still calls Paystack's public resolve endpoint when `PAYSTACK_SECRET_KEY` is present. With a dummy key in Demo mode it returns a demo account name instead, so the flow never blocks.

### What is simulated, and what is not

| Simulated in Demo mode | Still fully real |
| --- | --- |
| OTP email delivery. Use code `123456` anywhere a verification code is asked. Wrong codes still fail. | OTP verification logic, attempt limits, expiry, and identity binding |
| Funding. The payment link opens an internal simulated checkout page; one click runs the same backend confirmation a verified webhook would. | Amount and currency checks, funding state change, seller notification |
| Seller payout. Release returns a fake successful transfer such as `DEMO-TRF-...` and writes a real payout record. | Vault requirement, inspection rules, dispute freeze, release confirmation |
| Nothing else | AI tools and confirmations, join and resume, dispatch and receipt, bank detail saving, disputes, dashboard, admin |

### Judge script (about 5 minutes, two people or two browsers)

Use two separate browser sessions, because each party needs its own identity. Easiest: Browser A normal window for Person 1, Browser B private/incognito window for Person 2. Refreshing a chat starts it fresh on purpose. To resume, say "continue" plus the escrow code and verify again.

**Person 1 (buyer), Browser A, home page chat:**

1. Type or hold the mic and say: "I want to buy a laptop for 250000 from seller@example.com. My email is buyer@example.com, I am the buyer."
2. When the assistant shows the terms, reply: "yes".
3. When it says a code was sent, type `123456`. The escrow is created. Note the code, it looks like `TRSTaB3xK`. Share it with Person 2.

**Person 2 (seller), Browser B, home page chat:**

4. Say: "I want to join escrow TRSTaB3xK" (use the real code).
5. Give your email: "my email is seller@example.com".
6. When the code arrives in the story, type `123456`. You are joined, and Person 1 is notified in their open chat within about 10 seconds.

**Person 1 (buyer):**

7. Say: "fund TRSTaB3xK", then reply "yes" when asked. Open the payment link and click **Confirm simulated payment**. Do not expect a real Paystack page in Demo mode.

**Person 2 (seller):**

8. Your chat is told the escrow is funded. Say: "I have shipped it", then confirm with "yes".

**Person 1 (buyer):**

9. Say: "I received it", then confirm with "yes". Inspection starts. The seller is now asked for payout bank details.

**Person 2 (seller):**

10. Say: "My bank is Guaranty Trust Bank, account number 0123456789", then confirm with "yes". The assistant states the verified account name returned by the backend. Read it back and check it is the name you expect.

**Person 1 (buyer):**

11. Say: "release TRSTaB3xK", then confirm with "yes". Both sides are told the seller was paid and the escrow is complete.

Optional checks while you are there:

- Swap roles and fee payer on a second run ("I am the seller", "the seller pays the fee") and repeat.
- Raise a dispute instead of releasing at step 11 and confirm the funds stay frozen.
- Try a wrong OTP such as `000000` once and confirm it is rejected, then use `123456`.
- Open `/escrow/<code>/` or `/admin/` (create an admin user with `python manage.py createsuperuser`) and confirm the dashboard and audit trail match what the chats said.

### Start again from zero

```bash
python manage.py flush   # wipes escrows, chats, OTPs, and vaults in the local DB
```

Then reload both browsers. There is no seed data to restore; the judge script above creates everything it needs.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no such column: agent_aiconversation.pending_email` or any `no such column` error | Run `python manage.py migrate`, then restart the server. This is the most common fresh-checkout hiccup. |
| Chat says the AI assistant is temporarily unavailable | `BASETEN_API_KEY` is missing or wrong. Set it in `.env` and restart `runserver`. Manual forms still work without it. |
| Voice says transcription is not configured, or asks you to type | `MODEL_API_KEY` is missing. Voice also needs Chrome or Edge on `http://127.0.0.1:8000` (mic access is blocked on some plain-HTTP origins) and microphone permission allowed. |
| No email ever arrives with a code | Expected in Demo mode. Nothing is emailed; use `123456`. Outside Demo mode, check `TERMII_API_KEY`, `TERMII_EMAIL_CONFIG_ID`, and `TERMII_SEND_IN_DEBUG=True` for local delivery. |
| Payment link opens a Trustcrow page instead of Paystack | Expected in Demo mode. Click **Confirm simulated payment**. With `DEMO_MODE=False` the same link goes to real Paystack checkout. |
| Seller bank name shows a demo or test name | You have a dummy `PAYSTACK_SECRET_KEY`. A real test or live key makes the resolver return the actual account name. |
| The other person's chat does not update | Keep the tab open and visible for about 10 seconds; webchat polls for updates. A full refresh restarts the chat by design: resume with "continue TRST..." plus `123456`. |
| WhatsApp bot does not reply locally | WhatsApp needs a public HTTPS webhook. Run a tunnel such as `cloudflared tunnel --url http://127.0.0.1:8000`, set the callback to `https://<tunnel-host>/webhooks/whatsapp/`, and fill in all `WHATSAPP_*` values. Not needed for the web demo. |
| `Address already in use` on port 8000 | Stop the old server (Ctrl+C in its terminal) or run `python manage.py runserver 8001` and open that port instead. |
| Page loads but buttons or mic do nothing | Hard refresh (Ctrl+Shift+R). After pulling new code, also re-run `pip install -r requirements.txt` and `python manage.py migrate`. |

## Environment

Copy `.env.example` → `.env`. Never commit `.env`.

| Var | Required | Default | Notes |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | prod only | dev fallback | Must be set when `DJANGO_DEBUG=False` |
| `DJANGO_DEBUG` |, | `False` | Set `True` locally. SQLite is used when `True` |
| `DJANGO_ALLOWED_HOSTS` | prod only | `127.0.0.1,localhost` | Comma-separated |
| `DATABASE_URL` | prod only |, | Postgres URL (Neon/Render). `ssl_require=True` |
| `DEMO_MODE` | judges | `False` | `True` stubs Termii OTP delivery and Paystack payments. AI and escrow logic stay real |
| `DEMO_OTP_CODE` | judges | `123456` | Universal verification code accepted only while `DEMO_MODE=True` |
| `PAYSTACK_SECRET_KEY` | yes | `sk_test_dummy` | `sk_test_…` for testing, `sk_live_…` for live |
| `TERMII_API_KEY` | yes | `dummy_key` | Termii API key for email OTP |
| `TERMII_EMAIL_CONFIG_ID` | yes | `default_config` | Termii email config/channel ID |
| `TERMII_SEND_IN_DEBUG` |, | `False` | Set `True` for live local OTP delivery (costs credit) |
| `BASETEN_API_KEY` | for AI chat |, | Chat model inference key |
| `BASETEN_BASE_URL` |, | `https://inference.baseten.co/v1` | OpenAI-compatible endpoint |
| `BASETEN_MODEL` |, | `meta-models/muse-spark-open-1026` | Verified against project account |
| `AGENT_MAX_ITERATIONS` |, | `8` | Tool-call cap per chat turn |
| `AGENT_REQUEST_TIMEOUT` |, | `60` | Seconds per inference call |
| `MODEL_API_KEY` | for voice |, | Meta Model API key (Muse transcribe; ~$0.18/hr audio) |
| `ASR_BASE_URL` |, | `https://api.meta.ai` | Transcription endpoint |
| `ASR_MODEL` |, | `muse-voice-transcribe-1.0` | STT model |
| `VOICE_MAX_BYTES` |, | `8388608` | Upload cap per voice note |
| `VOICE_TIMEOUT` |, | `60` | Transcription request timeout |
| `WHATSAPP_ACCESS_TOKEN` | for WhatsApp |, | Cloud API token |
| `WHATSAPP_PHONE_NUMBER_ID` | for WhatsApp |, | Sending number ID |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` |, |, | Meta business account |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | for WhatsApp |, | Must match Meta dashboard |
| `WHATSAPP_APP_SECRET` | prod |, | Enforces `X-Hub-Signature-256` validation |
| `WHATSAPP_API_VERSION` |, | `v21.0` | Graph API version |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `EMAIL_USE_TLS` | prod | console backend in DEBUG | SMTP for invitation mail |
| `DEFAULT_FROM_EMAIL` |, | `Trustcrow <no-reply@…>` | Invitation sender |
| `GATEWAY_HTTP_TIMEOUT` |, | `15` | Seconds before Paystack/Termii/WhatsApp calls abort |
| `OTP_ISSUE_COOLDOWN_SECONDS` |, | `60` | Min gap between OTPs to same address/purpose |
| `LOG_OTP_TO_CONSOLE` |, | `False` | Set `True` locally to print codes to logs |
| `LOG_LEVEL` |, | `INFO` | Root + `escrow` logger level |
| `CSRF_TRUSTED_ORIGINS` | prod only |, | e.g. `https://escrow.example.com` |
| `SECURE_SSL_REDIRECT` |, | `True` (prod) | Set `False` only behind a TLS-terminating proxy that needs it |

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
| `/assistant/poll/` | `agent:chat_poll` | Live chat updates for open pages (`204` when nothing new) |
| `/demo/pay/<code>/` | `demo_pay` | Simulated checkout, Demo mode only (`404` when off) |
| `/escrow/<code>/dispatch/` `/receipt/` `/release/` `/dispute/` `/seller-dispute/` `/cancel/` `/vault/` |, | Lifecycle actions |
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

Design tokens, components, and responsive rules live in `templates/base.html`; `design.md` is the local UI source of truth (gitignored) and `escrow/tests.py::SmokeTest` enforces its guardrails, run `python manage.py test` before pushing UI changes.

## The AI agent

Bounded tool-using loop (`agent/loop.py`): user message → model → approved tool call → backend result → reply. 18 tools (`agent/registry.py`), all authenticated against the session-verified email and the escrow state machine, model arguments are untrusted input.

- Consequential tools (create, invite, join, pay, dispatch/receipt, bank details, release, dispute) need exact fresh confirmation. A clear "yes" executes the backend-stored proposal directly, so users are never asked to confirm twice.
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

- No passwords, OTP-gated sessions expire after `SESSION_COOKIE_AGE` (default 24h); OTPs hashed, short-lived, attempt-limited, cooldown-guarded, single-use.
- WhatsApp numbers are endpoints, not identity; verified email is the actor everywhere.
- Webhook signatures validated (Paystack, WhatsApp); duplicate events ignored via unique constraints.
- Amount + currency verified on every funding confirmation; payouts idempotent with `uncertain` parking (never blind-retry a timeout).
- Post-funding cancel requires ops refund check (see admin warning in `escrow/models.py`).
- Agent cannot release funds, resolve disputes, or bypass auth; prompt-injection and tool-spoofing covered by tests.
- Keep `LOG_OTP_TO_CONSOLE=False` outside local dev.
