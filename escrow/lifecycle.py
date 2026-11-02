"""Lifecycle notifications: tell each party what just happened.

Hooks sit beside the state transitions in views, services, and agent
tools. Best-effort WhatsApp push to the verified sender; anything
unconfigured or failing degrades silently and never breaks the money move.
"""
import logging

logger = logging.getLogger(__name__)


def _recipients(event, contract):
    buyer, seller = contract.buyer_email, contract.seller_email
    return {
        'counterparty_joined': [contract.creator_email],
        'funding_confirmed': [seller],
        'dispatch_confirmed': [buyer],
        'receipt_confirmed': [buyer, seller],
        'bank_details_needed': [seller],
        'payout_completed': [seller, buyer],
        'disputed': [buyer, seller],
    }.get(event, [])


def _copy(event, contract):
    code = contract.code
    if event == 'counterparty_joined':
        return (f"Update on escrow {code}: the counterparty has joined "
                "and verified. It is now awaiting funding — the buyer "
                "can pay next.")
    if event == 'bank_details_needed':
        return (f"Action needed on escrow {code}: the buyer is ready to "
                "release funds, but your payout bank details are missing. "
                "Tell me your bank name and 10-digit account number here "
                "and I'll verify and save them.")
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
