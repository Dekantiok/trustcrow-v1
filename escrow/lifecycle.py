"""Lifecycle notifications: tell each party what just happened.

Hooks sit beside the state transitions (join, funding, dispatch, receipt,
payout, dispute) in views, services, and agent tools. Delivery is
best-effort WhatsApp push to the verified sender behind an email address;
anything unconfigured or failing degrades silently and never breaks the
underlying money move. The agent also picks up the same events from the
audit trail on the user's next turn, so webchat users hear about updates
conversationally and dashboard users see them live.
"""
import logging

logger = logging.getLogger(__name__)


def _recipients(event, contract):
    buyer, seller = contract.buyer_email, contract.seller_email
    return {
        'counterparty_joined': [buyer],
        'funding_confirmed': [seller],
        'dispatch_confirmed': [buyer],
        'receipt_confirmed': [buyer, seller],
        'payout_completed': [seller, buyer],
        'disputed': [buyer, seller],
    }.get(event, [])


def _copy(event, contract):
    code = contract.code
    if event == 'counterparty_joined':
        return ("Good news — the other party verified escrow "
                f"{code}. Say 'fund' and I'll fetch your payment link.")
    if event == 'funding_confirmed':
        return (f"Escrow {code} is funded ({contract.total_charge} "
                f"{contract.currency}). Please dispatch, then tell me "
                "'dispatched' here.")
    if event == 'dispatch_confirmed':
        return (f"Your order in escrow {code} is on its way. When it "
                "arrives, tell me 'received' to start inspection.")
    if event == 'receipt_confirmed':
        ends = (contract.inspection_ends.strftime('%d %b, %H:%M')
                if contract.inspection_ends else 'the deadline')
        return (f"Receipt confirmed for {code} — inspection ends {ends}. "
                "Say 'release' to pay the seller or 'dispute' if "
                "something is wrong.")
    if event == 'payout_completed':
        return f"Escrow {code} is complete — the seller has been paid."
    if event == 'disputed':
        return (f"A dispute was raised on escrow {code} — funds are "
                "frozen until it is resolved.")
    return ''


def whatsapp_numbers_for(email):
    """Sender ids verified to this email. Empty when none or unconfigured."""
    from django.conf import settings
    if not getattr(settings, 'WHATSAPP_ACCESS_TOKEN', ''):
        return []
    from whatsapp.models import WhatsAppLink
    return list(WhatsAppLink.objects.filter(
        verified_email=email).values_list('wa_id', flat=True))


def notify_event(event, contract):
    """Push one lifecycle update per recipient. Never raises."""
    text = _copy(event, contract)
    if not text:
        return 0
    from whatsapp.adapter import send_text_message
    sent = 0
    for email in _recipients(event, contract):
        try:
            for wa_id in whatsapp_numbers_for(email):
                if send_text_message(wa_id, text):
                    sent += 1
        except Exception:
            logger.exception("Lifecycle notify failed for %s", email)
    if sent:
        logger.info("Lifecycle %s for %s pushed to %d chat(s)",
                    event, contract.code, sent)
    return sent
