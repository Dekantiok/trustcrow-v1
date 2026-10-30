import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils.timezone import now

from .gateways import get_payment_gateway
from .models import AuthOTP, EscrowContract, SettlementVault, TransitionError
from .notifications import send_otp_via_termii
from .utils import generate_otp, generate_secure_code, normalize_email

logger = logging.getLogger(__name__)

OTP_TTL = datetime.timedelta(minutes=15)


def calculate_service_fee(amount):
    """
    Calculates the flat-tier service fee based on the transaction amount.
    """
    from decimal import Decimal

    amount = Decimal(amount)

    if amount <= Decimal('50000'):
        return Decimal('1500')
    elif amount <= Decimal('100000'):
        return Decimal('2000')
    elif amount <= Decimal('500000'):
        return Decimal('2500')
    elif amount <= Decimal('1000000'):
        return Decimal('5000')
    else:
        return Decimal('10000')


def chargeable_total(contract):
    """Single source of truth for the checkout total; delegates to the model."""
    return contract.total_charge


def chargeable_kobo(contract):
    return contract.total_charge_kobo


def get_unique_contract_code(length=9, attempts=5):
    """Reserve a code that is actually free rather than trusting 62^9 entropy."""
    for _ in range(attempts):
        code = generate_secure_code(length)
        if not EscrowContract.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Unable to allocate a unique escrow code.")


def issue_otp(email, intent):
    """
    Create and deliver a one-time code.

    Any previously issued code for the same (email, intent) is burned first, so
    exactly one code is ever live and .get() can never match multiple rows. A
    cooldown also stops a contract code being used to spam an inbox.
    """
    email = normalize_email(email)
    cooldown = getattr(settings, 'OTP_ISSUE_COOLDOWN_SECONDS', 60)
    recent = AuthOTP.objects.filter(
        email=email, intent=intent, created_at__gt=now() - datetime.timedelta(seconds=cooldown)
    ).exists()
    if recent:
        logger.info("OTP issuance cooldown active for %s (%s)", email, intent)
        return None

    AuthOTP.objects.filter(email=email, intent=intent, is_used=False).update(is_used=True)

    otp_code = generate_otp()
    record = AuthOTP.objects.create(
        email=email,
        otp_code=otp_code,
        intent=intent,
        expires_at=now() + OTP_TTL,
    )
    delivered = send_otp_via_termii(email, otp_code)
    if not delivered:
        logger.warning("OTP delivery failed for %s (%s)", email, intent)
        record.is_used = True
        record.save(update_fields=['is_used'])
        return None
    return record


class OtpResult:
    SUCCESS = 'success'
    INVALID = 'invalid'
    EXPIRED = 'expired'
    EXHAUSTED = 'exhausted'

    MESSAGES = {
        INVALID: "That code is not correct. Please check and try again.",
        EXPIRED: "That code has expired. Please request a new one.",
        EXHAUSTED: "Too many incorrect attempts. Please request a new code.",
    }

    def __init__(self, outcome, record=None):
        self.outcome = outcome
        self.record = record

    @property
    def ok(self):
        return self.outcome == self.SUCCESS

    @property
    def message(self):
        return self.MESSAGES.get(self.outcome, "Invalid or expired code.")


def consume_otp(email, submitted_code, intent):
    """
    Validate a submitted one-time code.

    Attempts are decremented against the *live* code even when the submitted
    value is wrong, which closes the brute-force hole where a mismatched code
    skipped the attempt counter entirely.
    """
    email = normalize_email(email)
    submitted_code = (submitted_code or '').strip()
    if not submitted_code:
        return OtpResult(OtpResult.INVALID)

    record = (
        AuthOTP.objects.filter(email=email, intent=intent, is_used=False)
        .order_by('-id')
        .first()
    )
    if record is None:
        return OtpResult(OtpResult.INVALID)

    if record.otp_code != submitted_code:
        record.attempts_left = max(0, record.attempts_left - 1)
        record.save(update_fields=['attempts_left'])
        outcome = OtpResult.EXHAUSTED if record.attempts_left == 0 else OtpResult.INVALID
        return OtpResult(outcome, record)

    if record.is_expired:
        return OtpResult(OtpResult.EXPIRED, record)

    if record.attempts_left <= 0:
        return OtpResult(OtpResult.EXHAUSTED, record)

    record.is_used = True
    record.save(update_fields=['is_used'])
    return OtpResult(OtpResult.SUCCESS, record)


@dataclass
class PayoutResult:
    success: bool
    reason: str = ''
    detail: dict = None

    @property
    def message(self):
        return {
            'missing_vault': "The seller has not added payout bank details yet.",
            'not_in_inspection': "This escrow is not awaiting a payout.",
            'not_awaiting_payout': "This escrow is not awaiting a payout.",
            'transfer_failed': "The bank transfer did not go through. Our team has been notified.",
        }.get(self.reason, "The payout could not be completed.")


def _transfer_succeeded(result):
    if not isinstance(result, dict):
        return False
    if 'status' in result:
        return bool(result['status'])
    return result.get('message') == 'Success'


def confirm_funding_payment(contract, amount_kobo):
    """Mark a funded contract as awaiting_dispatch.

    Shared by the webhook and the callback fallback. The move is a single
    conditional UPDATE, so concurrent confirmations can only advance the
    contract once. Returns True when this call moved it.
    """
    if (
        EscrowContract.objects.filter(pk=contract.pk, status='awaiting_funding').count()
        == 0
    ):
        return False
    try:
        paid_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        logger.error(
            "Unusable paid amount %r for contract %s", amount_kobo, contract.code
        )
        return False
    if paid_kobo != contract.total_charge_kobo:
        logger.error(
            "Amount mismatch on %s: got %s expected %s",
            contract.code, paid_kobo, contract.total_charge_kobo,
        )
        return False
    moved = EscrowContract.objects.filter(
        pk=contract.pk, status='awaiting_funding'
    ).update(status='awaiting_dispatch')
    if moved:
        logger.info("Payment confirmed for contract %s", contract.code)
        contract.refresh_from_db()
    return bool(moved)


def settle_pending_payout(contract):
    """
    Transfer funds for a contract that is already in pending_payout.

    Used by staff resolving a dispute. The pending_payout -> completed move is
    conditional, so calling this twice can never issue a second transfer.
    """
    vault = SettlementVault.objects.filter(contract=contract).first()
    if not vault:
        return PayoutResult(success=False, reason='missing_vault')

    if EscrowContract.objects.filter(pk=contract.pk, status='pending_payout').count() == 0:
        return PayoutResult(success=False, reason='not_awaiting_payout')

    amount = contract.seller_receives
    try:
        result = get_payment_gateway().execute_seller_payout(vault, amount)
    except Exception:
        logger.exception("Payout raised for contract %s", contract.code)
        result = None

    if not _transfer_succeeded(result):
        logger.error("Payout failed for contract %s: %r", contract.code, result)
        contract.refresh_from_db()
        return PayoutResult(success=False, reason='transfer_failed', detail=result)

    EscrowContract.objects.filter(pk=contract.pk, status='pending_payout').update(status='completed')
    contract.refresh_from_db()
    logger.info("Payout complete for contract %s", contract.code)
    return PayoutResult(success=True)


def release_escrow_funds(contract):
    """
    Move an inspected escrow to payout and transfer the seller's balance.

    The in_inspection -> pending_payout move is a single conditional UPDATE, so
    two concurrent requests cannot both claim the contract and issue two
    transfers. The follow-on pending_payout -> completed move is guarded the
    same way.
    """
    # Read the vault through the manager rather than the cached related object,
    # so a stale in-memory instance can never claim a payout for a vault that
    # no longer exists.
    if not SettlementVault.objects.filter(contract=contract).exists():
        return PayoutResult(success=False, reason='missing_vault')

    claimed = EscrowContract.objects.filter(pk=contract.pk, status='in_inspection').update(
        status='pending_payout', buyer_confirmed=True
    )
    if not claimed:
        logger.info("Payout already claimed for contract %s", contract.code)
        return PayoutResult(success=False, reason='not_in_inspection')

    return settle_pending_payout(contract)


def auto_release_expired_inspections(dry_run=False):
    """
    Release escrows whose inspection window has elapsed without buyer action.

    Matches on in_inspection, which is the status that actually carries an
    inspection_ends deadline.
    """
    expired = EscrowContract.objects.filter(
        status='in_inspection', inspection_ends__lt=now()
    )
    if dry_run:
        return list(expired)

    released, skipped = 0, 0
    for contract in expired:
        result = release_escrow_funds(contract)
        if result.success:
            released += 1
        else:
            skipped += 1
            logger.warning(
                "Auto-release skipped contract %s: %s", contract.code, result.reason
            )
    return released, skipped


def transition_or_log(contract, target, **fields):
    """Convenience wrapper used by views that already guard on role."""
    try:
        return contract.transition_to(target, **fields)
    except TransitionError:
        logger.warning(
            "Rejected illegal transition %s -> %s on contract %s",
            contract.status, target, contract.code,
        )
        return None


def contracts_for_email(email, include_closed=False):
    email = normalize_email(email)
    queryset = EscrowContract.objects.filter(
        Q(creator_email=email) | Q(counterparty_email=email)
    )
    if not include_closed:
        queryset = queryset.exclude(status__in=EscrowContract.TERMINAL_STATUSES)
    return queryset
