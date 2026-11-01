"""Bounded agent loop: the model proposes, the backend disposes.

One turn: persist the user message, ask the model, execute any approved
tool calls against the registry (never trusting model arguments), feed
results back, and return the final reply. Consequential tools run only
when the user has confirmed the exact pending proposal; everything else
degrades to a safe message when the model is unreachable.
"""
import json
import logging
import re

from django.conf import settings
from django.utils import timezone

from agent.client import BasetenError, chat
from agent.models import AIConversation, ConversationMessage
from agent.registry import (
    CONFIRMATION_TTL_SECONDS,
    CONSEQUENTIAL_TOOLS,
    TOOLS,
    execute,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Trustcrow's escrow assistant. You help users create and \
track escrows safely.

Style: reply in 1-2 short sentences, at most one question per message. \
Sound like chat, not a form. Never send a long message.

Drafting rules — extract, don't interrogate:
- Pull every detail you can from what the user already wrote (e.g. \
"sell my laptop for 250000 to ada@example.com" already gives title, \
amount, counterparty, and creator_role=seller). Never ask for anything \
the user already told you.
- Save what you have immediately with create_escrow_draft, even partial, \
and top it up with update_escrow_draft as more arrives. Missing \
description? Reuse the title. Missing fee_payer? Assume buyer pays. \
Missing inspection window? Assume 3 days. Fold assumptions into the \
final summary instead of asking about them.
- Only ask for what cannot be guessed: usually just their own email \
address, plus the amount or the other party's email if truly absent. One \
missing thing per message, most important first.
- Guide the whole deal end to end: after creation say what happens \
next (counterparty verifies, then the buyer funds); when the \
counterparty has joined, offer the buyer the payment link without \
waiting to be asked; after funding nudge the seller to dispatch; \
after dispatch nudge the buyer to confirm receipt; in inspection \
offer release or dispute; after release confirm the payout.
- When asked for status or "what's next", check \
get_authorized_escrow_status first — never answer from memory.
- When the draft is complete, FIRST call request_escrow_creation right \
away — the backend holds it and tells you to present the exact terms. \
THEN show the terms briefly and ask for confirmation. Never show terms \
and ask for confirmation WITHOUT calling the tool in the same turn — \
otherwise the user is asked to confirm twice.
- After the user confirms, call the SAME tool again with IDENTICAL \
arguments (copy them exactly, e.g. draft_id stays a number). Do not \
re-check, re-read, or re-validate first — just make the identical call.

Rules you must follow:
- Explain what you are about to do BEFORE doing anything consequential, \
and only proceed after the user explicitly confirms the exact terms.
- To create, fund, confirm, or dispute anything, use your tools — never \
claim success from conversation alone.
- Start helping immediately; no account or verification is needed to talk, \
quote fees, or assemble a draft.
- Creating an escrow needs two user steps: first they confirm the exact \
terms, then they paste the 6-digit code sent to the creator email. That \
code arrives as their next chat message — just continue the conversation; \
the app verifies it with the backend and tells you the result through \
your tools. Never ask the user to read a code aloud or type it anywhere \
outside this chat; never repeat a code back.
- You can only see escrows the verified user participates in.
- Never ask for an escrow code, email verification, or any identifying \
detail unless the user wants to join, track, or act on a specific \
escrow. No unprompted lookups, no fishing for details.
- Never mention tool names, proposals, or raw results in chat. Speak \
plainly: "send the code", "confirm the terms", never the machinery \
behind them.
- If you do not know something, say so."""

# A message that is only a 6-digit code is an OTP being pasted, not chat.
OTP_MESSAGE_RE = re.compile(r'^\s*\d{6}\s*$')

DEGRADED_REPLY = (
    "The AI assistant is temporarily unavailable, but nothing is lost — "
    "you can continue with the manual escrow forms on this site."
)


def redact(text):
    """Strip OTP-like content before it reaches model context or logs."""
    if OTP_MESSAGE_RE.match(text or ''):
        return '[one-time code withheld]'
    return text


def get_or_create_conversation(channel, channel_identity):
    conversation, _ = AIConversation.objects.get_or_create(
        channel=channel,
        channel_identity=channel_identity,
    )
    return conversation


def _normalize_arguments(arguments):
    """Coerce model-generated args to canonical form before comparing.

    The model sometimes re-emits an id as a string ("2" vs 2) on its
    confirmation retry. Without this, the retry never matches the
    proposal and the user is asked to confirm forever.
    """
    normalized = {}
    for key, value in (arguments or {}).items():
        if key == 'draft_id':
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        normalized[key] = value
    return normalized


def _proposal_matches(pending, name, arguments):
    if not pending or pending.get('tool') != name:
        return False
    try:
        return json.dumps(_normalize_arguments(pending.get('arguments')),
                          sort_keys=True) == json.dumps(
                              _normalize_arguments(arguments), sort_keys=True)
    except (TypeError, ValueError):
        return False


def _proposal_fresh(pending):
    proposed_at = (pending or {}).get('proposed_at')
    if not proposed_at:
        return False
    try:
        from datetime import datetime
        moment = datetime.fromisoformat(proposed_at)
    except (ValueError, TypeError):
        return False
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    age = (timezone.now() - moment).total_seconds()
    return 0 <= age < CONFIRMATION_TTL_SECONDS


UPDATE_ACTIONS = {
    'escrow.created': 'created',
    'escrow.created_via_agent': 'created',
    'escrow.created_via_whatsapp': 'created',
    'funding.confirmed': 'funded',
    'delivery.dispatch_confirmed': 'dispatched',
    'delivery.receipt_confirmed': 'receipt confirmed, inspection running',
    'payout.completed': 'paid out',
    'dispute.submitted': 'disputed — frozen',
}


def _recent_updates_note(actor_email):
    """One-line brief of lifecycle moves since the user's last visit.

    Lets the agent open with what changed ("funded while you were away")
    instead of waiting to be asked. Read-only over the audit trail.
    """
    if not actor_email:
        return ''
    from datetime import timedelta
    from django.db.models import Q
    from escrow.models import AuditEvent
    cutoff = timezone.now() - timedelta(hours=48)
    rows = (AuditEvent.objects
            .filter(action__in=list(UPDATE_ACTIONS), created_at__gt=cutoff,
                    escrow__isnull=False)
            .filter(Q(escrow__creator_email=actor_email)
                    | Q(escrow__counterparty_email=actor_email))
            .select_related('escrow').order_by('-id')[:3])
    bits = [f"{row.escrow.code} {UPDATE_ACTIONS.get(row.action, row.action)}"
            for row in rows if row.escrow is not None]
    if not bits:
        return ''
    return ("Since your last visit: " + "; ".join(bits) + ". "
            "Mention what changed and offer the next step.")


def _confirmation_reminder(pending):
    """Tell the model the exact awaiting proposal so it can echo it.

    Tool calls are hidden from history (never replayed), so without this
    the model cannot know which arguments it proposed — it rebuilds the
    draft and the confirmation never matches. The reminder carries only
    backend-stored proposal data (tool + args, no OTPs); execution still
    requires the user's confirmation and full backend validation.
    """
    return (
        "Reminder: you already proposed "
        f"{pending.get('tool')} with arguments "
        f"{json.dumps(pending.get('arguments') or {}, sort_keys=True)} "
        "and the user was asked to confirm. If their latest message "
        "confirms it, call that tool NOW with IDENTICAL arguments — do "
        "not create another draft, re-read, or re-validate first. If "
        "they changed the terms or declined, handle the change instead "
        "(a new proposal replaces this one)."
    )


def propose_action(conversation, name, arguments):
    """Record the exact proposal the user is being asked to confirm."""
    conversation.pending_action = {
        'tool': name,
        'arguments': arguments or {},
        'proposed_at': timezone.now().isoformat(),
    }
    conversation.save(update_fields=['pending_action'])


def clear_proposal(conversation):
    conversation.pending_action = {}
    conversation.save(update_fields=['pending_action'])


def reset_thread_unless_live(conversation):
    """Clean thread for a page load unless mid-flow.

    Refreshing shows an empty chat instead of stale history. A fresh
    (unexpired) proposal — awaiting confirmation or an emailed code — is
    left intact with its messages so an in-flight flow survives a reload.
    Returns the live pending dict, or {}.
    """
    pending = conversation.pending_action or {}
    if pending.get('tool') and not _proposal_fresh(pending):
        clear_proposal(conversation)
        conversation.refresh_from_db()
        pending = {}
    if not pending.get('tool'):
        conversation.messages.all().delete()
        return {}
    return pending


def confirmed(conversation, name, arguments):
    pending = conversation.pending_action or {}
    return _proposal_matches(pending, name, arguments) and _proposal_fresh(pending)


def _history_messages(conversation, limit=20):
    """Replayable history: plain user/assistant turns only.

    Tool calls and results are internal reasoning, not conversation — they
    are re-driven by the model each turn, never replayed, so nothing can
    be re-executed from history and no call ids need persisting.
    """
    rows = conversation.messages.order_by('-id')[:limit]
    history = []
    for row in reversed(list(rows)):
        if row.role == 'user':
            history.append({'role': 'user', 'content': redact(row.content)})
        elif row.role == 'assistant' and not row.tool_name and row.content:
            history.append({'role': 'assistant', 'content': row.content})
    return history


def run_turn(conversation, user_text, base_url='', session_key=''):
    """Run one bounded turn. Always returns a reply string, never raises."""
    actor = (conversation.owner_email or '').strip() or None
    ConversationMessage.objects.create(
        conversation=conversation, role='user', content=user_text,
    )
    model_messages = (
        [{'role': 'system', 'content': SYSTEM_PROMPT}]
        + _history_messages(conversation)
    )
    pending = conversation.pending_action or {}
    if pending.get('tool'):
        if _proposal_fresh(pending):
            model_messages.append(
                {'role': 'system',
                 'content': _confirmation_reminder(pending)})
        else:
            # Stale proposal: drop it so the model starts clean instead
            # of echoing dead arguments into another gated round-trip.
            clear_proposal(conversation)
    updates_note = _recent_updates_note(actor)
    if updates_note:
        model_messages.append({'role': 'system', 'content': updates_note})
    max_iterations = getattr(settings, 'AGENT_MAX_ITERATIONS', 8)

    try:
        for _ in range(max_iterations):
            try:
                result = chat(model_messages, tools=TOOLS)
            except BasetenError:
                logger.exception("Agent inference failed; degrading gracefully")
                return DEGRADED_REPLY

            assistant_text = result.get('text') or ''
            calls = result.get('tool_calls') or []
            ConversationMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=assistant_text,
            )
            if not calls:
                return assistant_text or DEGRADED_REPLY

            model_messages.append({
                'role': 'assistant',
                'content': assistant_text,
                'tool_calls': [
                    {'id': c['id'], 'type': 'function',
                     'function': {'name': c['name'],
                                 'arguments': json.dumps(c['arguments']) }}
                    for c in calls
                ],
            })
            for call in calls:
                name, arguments = call['name'], call['arguments']
                ConversationMessage.objects.create(
                    conversation=conversation,
                    role='assistant',
                    content='',
                    tool_name=name,
                    tool_arguments=arguments,
                )
                if name in CONSEQUENTIAL_TOOLS and not confirmed(
                    conversation, name, arguments
                ):
                    propose_action(conversation, name, arguments)
                    outcome = {
                        'ok': False,
                        'error': (
                            'CONFIRMATION_REQUIRED: present the exact terms to '
                            'the user and ask for explicit confirmation first. '
                            'Do not call this tool again until they confirm.'
                        ),
                    }
                else:
                    if name in CONSEQUENTIAL_TOOLS:
                        clear_proposal(conversation)
                    outcome = execute(
                        name, arguments,
                        actor_email=actor, base_url=base_url,
                        session_key=session_key,
                    )
                    data = outcome.get('data') or {}
                    verification_email = data.get(
                        'verification_email') or (
                        data.get('email') if data.get('verification_required') else None
                    )
                    if outcome.get('ok') and verification_email:
                        # Creation is parked on email proof: remember what the
                        # user is verifying for so the chat view can resume it
                        # when the code arrives as the next chat message.
                        conversation.pending_action = {
                            'tool': name,
                            'arguments': arguments,
                            'proposed_at': timezone.now().isoformat(),
                            'verification_email': verification_email,
                            'draft_id': data.get('draft_id'),
                        }
                        conversation.save(update_fields=['pending_action'])
                ConversationMessage.objects.create(
                    conversation=conversation,
                    role='tool',
                    content=json.dumps(outcome),
                    tool_name=name,
                )
                model_messages.append({
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'content': json.dumps(outcome),
                })
        logger.warning("Agent hit iteration cap; degrading gracefully")
        return DEGRADED_REPLY
    except Exception:
        logger.exception("Agent loop failed; degrading gracefully")
        return DEGRADED_REPLY
