"""Website AI assistant: chat UI over the shared agent loop.

Verification binds the conversation to an email via the same OTP path as
the manual flows. A bare 6-digit message while a verification is pending
is routed to OTP consumption — never to the model.
"""
import logging

from django.shortcuts import render
from django.views.decorators.http import require_POST

from agent.loop import (
    OTP_MESSAGE_RE,
    clear_proposal,
    get_or_create_conversation,
    run_turn,
)
from escrow.services import audit, consume_otp, issue_otp
from escrow.utils import normalize_email

logger = logging.getLogger(__name__)


def _conversation(request):
    if not request.session.session_key:
        request.session.save()
    conversation = get_or_create_conversation(
        'web', f"session:{request.session.session_key}"
    )
    verified = normalize_email(request.session.get('chat_verified_email', ''))
    if verified and not conversation.owner_email:
        conversation.owner_email = verified
        conversation.save(update_fields=['owner_email'])
    elif conversation.owner_email and conversation.owner_email != verified:
        # Verification lapsed or switched: follow the session, the
        # server-side proof of who sits at this browser.
        conversation.owner_email = verified or None
        conversation.save(update_fields=['owner_email'])
    return conversation


def _thread_context(conversation, request):
    pending = conversation.pending_action or {}
    return {
        'messages': list(conversation.messages.order_by('id')),
        'pending': pending if pending.get('tool') else None,
        'owner_email': conversation.owner_email,
        'pending_email': request.session.get('chat_pending_email', ''),
    }


def chat_page(request):
    conversation = _conversation(request)
    context = _thread_context(conversation, request)
    return render(request, 'agent/chat.html', context)


@require_POST
def chat_verify(request):
    """Start or complete email verification for this chat session."""
    conversation = _conversation(request)
    email = normalize_email(request.POST.get('email', ''))
    code = (request.POST.get('code') or '').strip()

    if code:
        pending_email = request.session.get('chat_pending_email', '')
        if not pending_email:
            return render(
                request, 'agent/_thread.html',
                _thread_context(conversation, request),
            )
        result = consume_otp(pending_email, code, 'reauth')
        if result.ok:
            request.session['chat_verified_email'] = pending_email
            del request.session['chat_pending_email']
            conversation = _conversation(request)
            audit('chat.verified', actor=pending_email)
        else:
            conversation.messages.create(
                role='assistant',
                content=f"That code did not work: {result.message}",
            )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if not email:
        conversation.messages.create(
            role='assistant',
            content="Please share the email address you want to verify.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if issue_otp(email, 'reauth') is None:
        conversation.messages.create(
            role='assistant',
            content=("I could not send a code to that address right now. "
                     "Please try again in a minute."),
        )
    else:
        request.session['chat_pending_email'] = email
        conversation.messages.create(
            role='assistant',
            content=(f"I sent a 6-digit code to {email}. "
                     "Type it here or enter it below."),
        )
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_POST
def chat_send(request):
    conversation = _conversation(request)
    text = (request.POST.get('message') or '').strip()
    if not text:
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    pending_email = request.session.get('chat_pending_email', '')
    if pending_email and OTP_MESSAGE_RE.match(text):
        # OTP interception: verify in the backend, never show the model.
        result = consume_otp(pending_email, text, 'reauth')
        conversation.messages.create(role='user', content='[one-time code withheld]')
        if result.ok:
            request.session['chat_verified_email'] = pending_email
            del request.session['chat_pending_email']
            conversation = _conversation(request)
            conversation.messages.create(
                role='assistant',
                content=(f"Verified — I can now help with escrows for "
                         f"{pending_email}. What would you like to do?"),
            )
            audit('chat.verified', actor=pending_email)
        else:
            conversation.messages.create(
                role='assistant', content=result.message,
            )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    base_url = request.build_absolute_uri('/')[:-1]
    run_turn(conversation, text, base_url=base_url)
    conversation.refresh_from_db()
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_POST
def chat_dismiss(request):
    conversation = _conversation(request)
    clear_proposal(conversation)
    conversation.messages.create(
        role='assistant',
        content="Discarded — nothing was done. Tell me what you would like instead.",
    )
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )
