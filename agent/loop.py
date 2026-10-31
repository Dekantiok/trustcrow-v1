"""The bounded agent loop: model proposes, backend disposes.

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

Rules you must follow:
- Explain what you are about to do BEFORE doing anything consequential, \
and only proceed after the user explicitly confirms the exact terms.
- To create, fund, confirm, or dispute anything, use your tools — never \
claim success from conversation alone.
- You can only see escrows the verified user participates in. Never ask \
for or repeat one-time codes; verification happens in the app, not in chat.
- If you do not know something, say so. Keep replies short and concrete."""

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


def _proposal_matches(pending, name, arguments):
    if not pending or pending.get('tool') != name:
        return False
    try:
        return json.dumps(pending.get('arguments') or {}, sort_keys=True) == (
            json.dumps(arguments or {}, sort_keys=True)
        )
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


def run_turn(conversation, user_text, base_url=''):
    """Run one bounded turn. Always returns a reply string, never raises."""
    actor = (conversation.owner_email or '').strip() or None
    ConversationMessage.objects.create(
        conversation=conversation, role='user', content=user_text,
    )
    model_messages = (
        [{'role': 'system', 'content': SYSTEM_PROMPT}]
        + _history_messages(conversation)
    )
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
                    )
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
