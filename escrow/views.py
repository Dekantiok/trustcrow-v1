import datetime
import logging

from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .bank_verification import get_bank_name, list_banks, verify_bank_account
from .forms import ContractCreationForm, SettlementVaultForm
from .gateways import get_payment_gateway
from .models import EscrowContract, TransitionError
from .services import (
    calculate_service_fee,
    confirm_funding_payment,
    consume_otp,
    contracts_for_email,
    get_unique_contract_code,
    issue_otp,
    release_escrow_funds,
)
from .utils import normalize_email

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    'pending_verification': 'Pending Verification',
    'awaiting_counterparty': 'Awaiting Counterparty',
    'awaiting_funding': 'Awaiting Funding',
    'awaiting_dispatch': 'Awaiting Dispatch',
    'in_transit': 'In Transit',
    'in_inspection': 'In Inspection',
    'pending_payout': 'Funds Released',
    'completed': 'Completed',
    'disputed': 'Disputed',
    'cancelled': 'Cancelled',
}

STATUS_BADGES = {
    'pending_verification': 'bg-warning text-dark',
    'awaiting_counterparty': 'bg-dark text-white',
    'awaiting_funding': 'bg-primary-custom',
    'awaiting_dispatch': 'bg-warning text-dark',
    'in_transit': 'bg-secondary',
    'in_inspection': 'bg-primary-custom',
    'pending_payout': 'bg-success',
    'completed': 'bg-success',
    'disputed': 'bg-danger',
    'cancelled': 'bg-secondary',
}

# Human-readable summary of where an escrow sits in the lifecycle.
STATUS_STAGE = {
    'pending_verification': 1,
    'awaiting_counterparty': 2,
    'awaiting_funding': 3,
    'awaiting_dispatch': 4,
    'in_transit': 5,
    'in_inspection': 6,
    'pending_payout': 7,
    'completed': 8,
    'disputed': 8,
    'cancelled': 8,
}

MONEY_STAGES = 8


def naira(value):
    return f"\u20a6{value:,.2f}"


def get_contract_verified_email(request, contract):
    """Email this device verified for THIS contract, or ''.

    Buyer and seller are different people on different devices, so
    verification is scoped per escrow code: this browser holds at most
    one role per contract. The old global ``verified_emails`` list is
    only consulted as a one-party legacy fallback; a legacy session
    holding both parties is treated as unverified to force a fresh,
    single-role verify.
    """
    code = contract.code
    scoped = request.session.get('verified_contracts', {})
    email = normalize_email(scoped.get(code, ''))
    if email and email in contract.parties:
        return email
    my_email = normalize_email(request.session.get('my_escrow_verified', ''))
    if my_email and my_email in contract.parties:
        return my_email
    legacy = [normalize_email(e) for e in request.session.get('verified_emails', [])]
    matched = [e for e in legacy if e in contract.parties]
    if len(matched) == 1:
        return matched[0]
    return ''


def get_verified_emails(request):
    # Legacy accessor kept for backwards compatibility; new code should
    # use get_contract_verified_email() which is scoped per contract.
    return [normalize_email(e) for e in request.session.get('verified_emails', [])]


def set_contract_verified_email(request, code, email):
    email = normalize_email(email)
    scoped = dict(request.session.get('verified_contracts', {}))
    scoped[code] = email
    request.session['verified_contracts'] = scoped
    # Rotate the session key on privilege change to prevent fixation:
    # a pre-login session id must never become a verified-party session.
    try:
        request.session.cycle_key()
    except Exception:
        pass


def add_verified_email(request, email):
    # Legacy helper: bind the email to every contract it belongs to is
    # unsafe (one device becomes both parties), so this is a no-op kept
    # only so old imports keep working. Use set_contract_verified_email.
    return None


def is_party(contract, request):
    return bool(get_contract_verified_email(request, contract))


def home_view(request):
    return render(request, 'home.html')


def join_escrow_page(request):
    error_message = None
    if request.method == 'POST':
        code = request.POST.get('code', '').strip()
        email = normalize_email(request.POST.get('email', ''))
        contract = EscrowContract.objects.filter(code=code).first()
        if contract is None:
            error_message = "No escrow found with that code."
        elif contract.status != 'awaiting_counterparty':
            error_message = "This escrow is not currently accepting counterparty verification."
        elif email != contract.counterparty_email:
            error_message = "The email does not match the counterparty linked to this escrow."
        elif issue_otp(email, 'join') is None:
            error_message = (
                "We could not send a verification code right now. "
                "Please wait a moment and try again."
            )
        else:
            return redirect('counterparty_verify_otp', code=contract.code)
    return render(request, 'escrow/join_escrow.html', {'error_message': error_message})


def create_contract(request):
    if request.method == 'POST':
        form = ContractCreationForm(request.POST)
        if form.is_valid():
            contract = form.save(commit=False)
            contract.code = get_unique_contract_code(9)
            contract.service_fee = calculate_service_fee(contract.amount)
            contract.status = 'pending_verification'
            contract.gateway_reference = f"{contract.code}_{int(now().timestamp())}"
            contract.save()
            if issue_otp(contract.creator_email, 'create') is None:
                messages.error(
                    request,
                    "Your escrow was created but we could not email a verification code. "
                    "Please contact support.",
                )
            return redirect('verify_otp', code=contract.code)
    else:
        form = ContractCreationForm()
    return render(request, 'escrow/create_contract.html', {'form': form})


def _verify_and_advance(request, contract, intent, next_status, email):
    """
    Shared body for the two OTP verification views.

    One device holds one role per contract: if this browser already
    verified as the OTHER party for this escrow, refuse before burning
    an OTP attempt. Returns (redirect_response, error_message).
    consume_otp is called exactly once per submission so an attempt is
    only ever counted once.
    """
    email = normalize_email(email)
    existing = get_contract_verified_email(request, contract)
    if existing and existing != email:
        return None, (
            "This device already verified as "
            f"{existing}. The other party must verify on their own device."
        )
    result = consume_otp(email, request.POST.get('otp'), intent)
    if not result.ok:
        return None, result.message
    try:
        contract.transition_to(next_status)
    except TransitionError:
        logger.warning(
            "Rejected %s -> %s on contract %s", contract.status, next_status, contract.code
        )
        return redirect('contract_detail', code=contract.code), None
    set_contract_verified_email(request, contract.code, email)
    return redirect('contract_detail', code=contract.code), None


def verify_otp_view(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    error_message = None
    if request.method == 'POST':
        redirect_response, error_message = _verify_and_advance(
            request, contract, 'create', 'awaiting_counterparty',
            contract.creator_email,
        )
        if redirect_response is not None:
            return redirect_response
    return render(
        request, 'escrow/verify_otp.html',
        {'contract': contract, 'error_message': error_message},
    )


def counterparty_verify_otp(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    error_message = None
    if request.method == 'POST':
        redirect_response, error_message = _verify_and_advance(
            request, contract, 'join', 'awaiting_funding',
            contract.counterparty_email,
        )
        if redirect_response is not None:
            return redirect_response
    return render(
        request, 'escrow/counterparty_verify_otp.html',
        {'contract': contract, 'error_message': error_message},
    )


def contract_detail(request, code):
    contract = get_object_or_404(EscrowContract, code=code)

    if not is_party(contract, request):
        return render(
            request, 'escrow/contract_locked.html',
            {'contract': contract, 'status_label': STATUS_LABELS.get(contract.status, '')},
            status=403,
        )

    verified_email = get_contract_verified_email(request, contract)
    is_buyer = verified_email == contract.buyer_email
    is_seller = verified_email == contract.seller_email
    vault = getattr(contract, 'settlement_vault', None)

    context = {
        'contract': contract,
        'status_label': STATUS_LABELS.get(contract.status, contract.status),
        'badge_class': STATUS_BADGES.get(contract.status, 'bg-secondary'),
        'stage': STATUS_STAGE.get(contract.status, 1),
        'total_stages': MONEY_STAGES,
        'progress_pct': int(
            (STATUS_STAGE.get(contract.status, 1) / MONEY_STAGES) * 100
        ),
        'is_buyer': is_buyer,
        'is_seller': is_seller,
        'role_label': 'Buyer' if is_buyer else ('Seller' if is_seller else 'Observer'),
        'is_live': not contract.is_terminal and contract.status != 'disputed',
        'inspection_ends_display': contract.inspection_ends.strftime('%d %b %Y, %H:%M')
        if contract.inspection_ends else None,
        'inspection_expired': contract.is_inspection_expired,
        'show_payment_button': (contract.status == 'awaiting_funding' and is_buyer),
        'show_seller_dispatch_button': (contract.status == 'awaiting_dispatch' and is_seller),
        'show_buyer_confirm_receipt': (contract.status == 'in_transit' and is_buyer),
        'show_buyer_release_funds': (contract.status == 'in_inspection' and is_buyer),
        'show_buyer_raise_dispute': (contract.status == 'in_inspection' and is_buyer),
        'show_seller_raise_dispute': (
            contract.status in ('awaiting_dispatch', 'in_transit', 'in_inspection')
            and is_seller
        ),
        'show_cancel_button': (
            contract.status in EscrowContract.PRE_FUNDING_STATUSES
            and (is_buyer or is_seller)
        ),
        'show_seller_vault_form': (
            contract.status in ('awaiting_dispatch', 'in_transit', 'in_inspection')
            and is_seller and not vault
        ),
        'show_seller_waiting': (
            contract.status in ('in_transit', 'in_inspection') and is_seller and bool(vault)
        ),
        'show_seller_funds_released': (contract.status == 'pending_payout' and is_seller),
        'show_seller_completed': (contract.status == 'completed' and is_seller),
        'show_buyer_completed': (contract.status == 'completed' and is_buyer),
        'show_settlement_vault': bool(vault),
        'show_disputed_notice': (contract.status == 'disputed'),
        # One device holds one role per contract, so both sides are never
        # shown together. Kept as False for template compatibility.
        'show_role_overlap_notice': False,
        'formatted_amount': naira(contract.amount),
        'formatted_fee': naira(contract.service_fee),
        'formatted_seller_receives': naira(contract.seller_receives),
    }

    if vault:
        # The seller entered these, so only the seller sees them in full. The
        # buyer gets a masked reference to reduce redirection-fraud risk.
        context['vault_bank_name'] = vault.bank_name
        context['vault_account_number'] = (
            vault.account_number if is_seller else vault.masked_account_number
        )
        context['vault_account_name'] = vault.account_name
        context['vault_is_masked'] = not is_seller

    # HTMX polls for the panel only, so a status change swaps the card in place
    # instead of replacing the whole page.
    template = (
        'escrow/_contract_panel.html'
        if request.headers.get('HX-Request') == 'true'
        else 'escrow/contract_detail.html'
    )
    return render(request, template, context)


@require_POST
def initiate_payment(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    if get_contract_verified_email(request, contract) != contract.buyer_email:
        return HttpResponseForbidden("Only the verified buyer can fund this escrow.")
    if contract.status != 'awaiting_funding':
        return redirect('contract_detail', code=contract.code)

    gateway = get_payment_gateway()
    return_url = request.build_absolute_uri(
        reverse('payment_callback', kwargs={'code': contract.code})
    )
    result = gateway.initialize_vault_payment(contract, return_url)
    if result.get('status') and result.get('auth_url'):
        return redirect(result['auth_url'])

    logger.error("Paystack init failed for %s: %r", contract.code, result)
    messages.error(request, "We could not start the payment. Please try again in a moment.")
    return redirect('contract_detail', code=contract.code)


def payment_callback(request, code):
    """
    Browser return leg from Paystack Checkout.

    Webhooks are the primary confirmation, but they never arrive when the
    webhook URL isn't configured or reachable on the gateway dashboard —
    which used to leave escrows stuck on "confirming payment" forever. As a
    fallback, verify OUR stored reference directly with Paystack before
    showing that message. The browser's query string is never trusted and
    the move is a conditional UPDATE, so replays can't double-advance.
    """
    contract = get_object_or_404(EscrowContract, code=code)
    if contract.status == 'awaiting_funding' and contract.gateway_reference:
        try:
            result = get_payment_gateway().verify_transaction(
                contract.gateway_reference
            )
        except Exception:
            logger.exception(
                "Payment verification raised for contract %s", contract.code
            )
            result = None
        data = (
            result.get('data', {}) if isinstance(result, dict) else {}
        )
        if (
            isinstance(result, dict) and result.get('status')
            and isinstance(data, dict) and data.get('status') == 'success'
            and data.get('reference') == contract.gateway_reference
            and confirm_funding_payment(contract, data.get('amount'))
        ):
            messages.success(
                request,
                "Payment confirmed. The seller has been notified to dispatch.",
            )
            return redirect('contract_detail', code=contract.code)
        contract.refresh_from_db()
    if contract.status == 'awaiting_funding':
        messages.info(
            request,
            "Thanks. We're confirming your payment. This usually takes a few seconds.",
        )
    return redirect('contract_detail', code=contract.code)


@csrf_exempt
def paystack_webhook(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Method not allowed")

    payload = get_payment_gateway().verify_incoming_webhook(request.headers, request.body)
    if not payload:
        return HttpResponseBadRequest("Invalid signature")

    event = payload.get('event')
    data = payload.get('data', {})
    reference = data.get('reference')

    if event != 'charge.success' or not reference:
        logger.info("Ignoring Paystack event %s for reference %s", event, reference)
        return HttpResponse("Webhook received", status=200)

    contract = EscrowContract.objects.filter(gateway_reference=reference).first()
    if contract is None:
        logger.warning("Paystack charge for unknown reference %s", reference)
        return HttpResponse("Unknown reference", status=404)

    if contract.status != 'awaiting_funding':
        # Already advanced, or the amount never matched. Replaying a charge
        # success must not move the contract a second time.
        logger.info(
            "Ignoring charge.success for %s in status %s", contract.code, contract.status
        )
        return HttpResponse("Already processed", status=200)

    amount_kobo = data.get('amount')
    # Must equal what initialize_vault_payment charged: amount + fee when
    # the buyer is the fee payer, else amount alone.
    expected_kobo = contract.total_charge_kobo
    if amount_kobo is not None and int(amount_kobo) != expected_kobo:
        logger.error(
            "Amount mismatch on %s: got %s expected %s",
            contract.code, amount_kobo, expected_kobo,
        )
        return HttpResponse("Amount mismatch", status=400)

    moved = EscrowContract.objects.filter(
        pk=contract.pk, status='awaiting_funding'
    ).update(status='awaiting_dispatch')
    if moved:
        logger.info("Payment confirmed for contract %s", contract.code)
    return HttpResponse("Webhook received", status=200)


def _require_role(request, contract, role):
    email = contract.buyer_email if role == 'buyer' else contract.seller_email
    if get_contract_verified_email(request, contract) != email:
        return HttpResponseForbidden(
            f"Only the verified {role} can perform this action."
        )
    return None


@require_POST
def seller_confirm_dispatch(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    try:
        contract.transition_to('in_transit', seller_confirmed=True)
    except TransitionError:
        pass
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_confirm_receipt(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied
    inspection_ends = now() + datetime.timedelta(days=contract.inspection_days)
    try:
        contract.transition_to('in_inspection', inspection_ends=inspection_ends)
    except TransitionError:
        pass
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_release_funds(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied

    result = release_escrow_funds(contract)
    if result.success:
        messages.success(request, "Funds released. The seller has been paid.")
    else:
        messages.error(request, result.message)
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_raise_dispute(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied
    try:
        contract.transition_to('disputed')
    except TransitionError:
        pass
    messages.info(
        request,
        "The dispute has been raised. Funds are held until our team reviews the escrow.",
    )
    return redirect('contract_detail', code=contract.code)


@require_POST
def seller_raise_dispute(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    try:
        contract.transition_to('disputed')
    except TransitionError:
        pass
    messages.info(
        request,
        "The dispute has been raised. Funds are held until our team reviews the escrow.",
    )
    return redirect('contract_detail', code=contract.code)


@require_POST
def cancel_escrow(request, code):
    """Party-initiated cancel, allowed only before money has moved.

    Once funded, cancellation needs an ops refund via the Paystack
    dashboard/API, so funded contracts redirect with an explanatory message
    instead of transitioning here.
    """
    contract = get_object_or_404(EscrowContract, code=code)
    if not is_party(contract, request):
        return HttpResponseForbidden("Only a verified party can cancel this escrow.")
    if contract.status not in EscrowContract.PRE_FUNDING_STATUSES:
        messages.error(
            request,
            "This escrow is already funded. Cancellation now requires a refund — "
            "please contact support so our team can review it.",
        )
        return redirect('contract_detail', code=contract.code)
    try:
        contract.transition_to('cancelled')
    except TransitionError:
        pass
    messages.info(request, "The escrow has been cancelled. No payment was taken.")
    return redirect('contract_detail', code=contract.code)


def seller_add_vault(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    if contract.status not in ('awaiting_dispatch', 'in_transit', 'in_inspection'):
        return redirect('contract_detail', code=contract.code)
    if hasattr(contract, 'settlement_vault'):
        return redirect('contract_detail', code=contract.code)

    if request.method == 'POST':
        form = SettlementVaultForm(request.POST, banks=list_banks())
        if form.is_valid():
            vault = form.save(commit=False)
            # Verify with Paystack BEFORE saving anything. Nothing is
            # persisted unless the account number + bank resolve.
            verification = verify_bank_account(vault.account_number, vault.bank_code)
            if verification.get('status'):
                # Trust the resolver's answer over what was typed, so a payee
                # name cannot be spoofed.
                vault.account_name = verification['account_name'][:150]
                vault.bank_name = get_bank_name(vault.bank_code)[:100]
                vault.contract = contract
                vault.save()
                messages.success(
                    request,
                    f"Payout account verified and saved ({vault.account_name}).",
                )
                return redirect('contract_detail', code=contract.code)
            messages.error(
                request,
                f"Bank account verification failed: "
                f"{verification.get('error') or 'Unknown error'}",
            )
    else:
        form = SettlementVaultForm(banks=list_banks())

    return render(
        request, 'escrow/add_vault.html',
        {'form': form, 'contract': contract},
    )


def my_escrow(request):
    email = request.session.get('my_escrow_pending_email')
    if email and request.session.get('my_escrow_verified') == email:
        return redirect('my_escrow_list')

    error_message = None
    if request.method == 'POST':
        email = normalize_email(request.POST.get('email', ''))
        if not email:
            error_message = "Please enter a valid email address."
        elif not contracts_for_email(email).exists():
            error_message = "No active escrows found for this email."
        elif issue_otp(email, 'reauth') is None:
            error_message = (
                "We could not send a verification code right now. "
                "Please wait a moment and try again."
            )
        else:
            request.session['my_escrow_pending_email'] = email
            return redirect('my_escrow_verify_otp')

    return render(request, 'escrow/my_escrow_email.html', {'error_message': error_message})


def my_escrow_verify_otp(request):
    email = request.session.get('my_escrow_pending_email')
    if not email:
        return redirect('my_escrow')

    error_message = None
    if request.method == 'POST':
        result = consume_otp(email, request.POST.get('otp'), 'reauth')
        if result.ok:
            request.session['my_escrow_verified'] = email
            del request.session['my_escrow_pending_email']
            return redirect('my_escrow_list')
        error_message = result.message

    return render(
        request, 'escrow/my_escrow_otp.html',
        {'email': email, 'error_message': error_message},
    )


def my_escrow_list(request):
    email = request.session.get('my_escrow_verified')
    if not email:
        return redirect('my_escrow')

    escrow_data = []
    for contract in contracts_for_email(email).order_by('-id'):
        escrow_data.append({
            'code': contract.code,
            'title': contract.title,
            'description': contract.description,
            'status': contract.status,
            'status_label': STATUS_LABELS.get(contract.status, contract.status),
            'badge_class': STATUS_BADGES.get(contract.status, 'bg-secondary'),
            'user_role': (
                contract.creator_role if contract.creator_email == email
                else ('seller' if contract.creator_role == 'buyer' else 'buyer')
            ),
            'formatted_amount': naira(contract.amount),
        })

    return render(
        request, 'escrow/my_escrow_list.html',
        {'escrows': escrow_data, 'email': email},
    )


__all__ = [
    'home_view', 'join_escrow_page', 'create_contract', 'verify_otp_view',
    'contract_detail', 'counterparty_verify_otp', 'initiate_payment',
    'payment_callback', 'paystack_webhook', 'seller_confirm_dispatch',
    'buyer_confirm_receipt', 'buyer_release_funds', 'buyer_raise_dispute',
    'seller_raise_dispute', 'cancel_escrow',
    'seller_add_vault', 'my_escrow', 'my_escrow_verify_otp', 'my_escrow_list',
    'add_verified_email', 'get_verified_emails', 'is_party',
    'get_contract_verified_email', 'set_contract_verified_email',
]
