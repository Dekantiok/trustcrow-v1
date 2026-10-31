"""The agent's only door into the platform: 12 approved tools.

Every handler receives the verified actor from the server-side session —
never from model arguments. Anything the model sends is treated as
untrusted input: validated, re-checked against escrow participation, and
subject to the same state machine as the manual forms.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.urls import reverse

from escrow.forms import ContractCreationForm, MAX_ESCROW_AMOUNT, MIN_ESCROW_AMOUNT
from escrow.gateways import get_payment_gateway
from escrow.models import EscrowContract, EscrowDraft
from escrow.services import (
    audit,
    calculate_service_fee,
    create_escrow_from_validated,
    issue_otp,
)

logger = logging.getLogger(__name__)

CONFIRMATION_TTL_SECONDS = 15 * 60

CONSEQUENTIAL_TOOLS = {
    'request_escrow_creation',
    'request_counterparty_invitation',
    'request_payment_link',
    'request_delivery_confirmation',
    'request_dispute_submission',
}


class ToolError(Exception):
    """A tool failure safe to relay to the model (no internals leak)."""


def _require_actor(actor_email):
    if not actor_email:
        raise ToolError(
            "The user has not verified their email yet. Guide them through "
            "email verification first."
        )
    return actor_email


def _get_participant_contract(escrow_reference, actor_email):
    try:
        contract = EscrowContract.objects.get(code=escrow_reference)
    except EscrowContract.DoesNotExist:
        raise ToolError("No escrow found with that reference.")
    if actor_email not in (contract.buyer_email, contract.seller_email):
        raise ToolError("You are not authorized for this escrow.")
    return contract


def _get_owned_draft(draft_id, actor_email, session_key=''):
    """A draft belongs to the session that started it, or to the verified
    email it claims once proven. Either credential opens it; neither leaks
    the other — a miss is just 'not authorized'."""
    try:
        draft = EscrowDraft.objects.get(id=draft_id)
    except (EscrowDraft.DoesNotExist, ValueError, TypeError):
        raise ToolError("No draft found with that id.")
    if actor_email and draft.owner_email == actor_email:
        return draft
    if session_key and draft.owner_session == session_key:
        return draft
    raise ToolError("You are not authorized for this draft.")


def _require_draft_identity(actor_email, session_key):
    if not actor_email and not session_key:
        raise ToolError("This conversation has no session to attach a draft to.")


def _parse_amount(raw):
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ToolError("The amount must be a number, e.g. 50000.")
    if amount < MIN_ESCROW_AMOUNT or amount > MAX_ESCROW_AMOUNT:
        raise ToolError(
            f"The amount must be between {MIN_ESCROW_AMOUNT:,.2f} and "
            f"{MAX_ESCROW_AMOUNT:,.2f}."
        )
    return amount


DRAFT_FIELDS = (
    'title', 'description', 'amount', 'currency', 'fee_payer',
    'creator_role', 'counterparty_email', 'inspection_days',
)


def tool_get_platform_information(actor, **kwargs):
    del kwargs
    return {'ok': True, 'data': {
        'name': 'Trustcrow',
        'how_it_works': [
            'Describe the deal: item, amount, and the other party’s email.',
            'Both sides verify their email with a one-time code.',
            'The buyer funds the escrow through Paystack.',
            'The seller dispatches; the buyer confirms receipt.',
            'After the agreed inspection window with no dispute, '
            'the seller is paid.',
        ],
        'fees': 'A tiered service fee applies; ask for a fee quote for exact numbers.',
        'auth': 'No account needed to start. When terms are confirmed, the '
                'creator verifies their email with a one-time code and the '
                'escrow is created.',
    }}


def tool_get_fee_quote(actor, **kwargs):
    del actor
    amount = _parse_amount(kwargs.get('amount'))
    fee_payer = kwargs.get('fee_payer', 'buyer')
    if fee_payer not in ('buyer', 'seller'):
        raise ToolError("fee_payer must be 'buyer' or 'seller'.")
    fee = calculate_service_fee(amount)
    total = amount + fee if fee_payer == 'buyer' else amount
    return {'ok': True, 'data': {
        'amount': str(amount),
        'currency': 'NGN',
        'service_fee': str(fee),
        'fee_payer': fee_payer,
        'buyer_pays': str(total),
        'seller_receives': str(amount - fee if fee_payer == 'seller' else amount),
    }}


def tool_validate_escrow_draft(actor, **kwargs):
    del actor
    form = ContractCreationForm({k: kwargs.get(k) for k in (
        'title', 'description', 'amount', 'fee_payer', 'creator_email',
        'creator_role', 'counterparty_email', 'inspection_days',
    )})
    if not form.is_valid():
        return {'ok': True, 'data': {
            'valid': False,
            'errors': {
                field: [str(e) for e in errors]
                for field, errors in form.errors.items()
            },
        }}
    cleaned = form.cleaned_data
    fee = calculate_service_fee(cleaned['amount'])
    return {'ok': True, 'data': {
        'valid': True,
        'service_fee': str(fee),
        'buyer_pays': str(
            cleaned['amount'] + fee if cleaned['fee_payer'] == 'buyer'
            else cleaned['amount']
        ),
    }}


def tool_get_authorized_escrow_status(actor, **kwargs):
    actor = _require_actor(actor)
    contract = _get_participant_contract(kwargs.get('escrow_reference'), actor)
    return {'ok': True, 'data': {
        'reference': contract.code,
        'title': contract.title,
        'status': contract.status,
        'amount': str(contract.amount),
        'currency': contract.currency,
        'you_are': (
            'buyer' if actor == contract.buyer_email else 'seller'
        ),
        'counterparty_verified': (
            contract.seller_confirmed if actor == contract.buyer_email
            else contract.buyer_confirmed
        ),
        'inspection_ends': (
            contract.inspection_ends.isoformat()
            if contract.inspection_ends else None
        ),
    }}


def tool_create_escrow_draft(actor, **kwargs):
    session_key = kwargs.get('_session_key', '')
    _require_draft_identity(actor, session_key)
    fields = {k: kwargs.get(k) for k in DRAFT_FIELDS if kwargs.get(k) not in (None, '')}
    claimed = (kwargs.get('creator_email') or '').strip()
    if claimed and actor and claimed != actor:
        raise ToolError(
            "That email does not match this verified chat. "
            "Verify it first or use the verified address."
        )
    owner_email = actor or claimed
    if 'counterparty_email' in fields and fields['counterparty_email'] == owner_email:
        raise ToolError("You cannot use your own email as the counterparty.")
    draft = EscrowDraft.objects.create(
        owner_email=owner_email, owner_session=session_key or '', **fields)
    audit('draft.created', actor=owner_email or 'anonymous',
          metadata={'draft_id': draft.id})
    data = {'draft_id': draft.id, 'status': draft.status}
    if not actor:
        data['note'] = (
            "Draft saved. Your email will be verified with a one-time code "
            "when you confirm creation."
        )
    return {'ok': True, 'data': data}


def tool_update_escrow_draft(actor, **kwargs):
    session_key = kwargs.get('_session_key', '')
    _require_draft_identity(actor, session_key)
    draft = _get_owned_draft(kwargs.get('draft_id'), actor, session_key)
    if draft.status not in ('draft', 'proposed'):
        raise ToolError("This draft can no longer be edited.")
    for key in DRAFT_FIELDS:
        if kwargs.get(key) not in (None, ''):
            setattr(draft, key, kwargs[key])
    claimed = (kwargs.get('creator_email') or '').strip()
    if claimed:
        if actor and claimed != actor:
            raise ToolError(
                "That email does not match this verified chat."
            )
        draft.owner_email = actor or claimed
    if draft.counterparty_email and draft.counterparty_email == draft.owner_email:
        raise ToolError("You cannot use your own email as the counterparty.")
    draft.save()
    return {'ok': True, 'data': {'draft_id': draft.id, 'status': draft.status}}


def tool_get_escrow_draft(actor, **kwargs):
    session_key = kwargs.get('_session_key', '')
    _require_draft_identity(actor, session_key)
    draft = _get_owned_draft(kwargs.get('draft_id'), actor, session_key)
    data = {key: getattr(draft, key) for key in ('id', 'status', *DRAFT_FIELDS)}
    data['amount'] = str(data['amount']) if data['amount'] is not None else None
    data['creator_email'] = draft.owner_email
    return {'ok': True, 'data': data}


def tool_request_escrow_creation(actor, **kwargs):
    session_key = kwargs.get('_session_key', '')
    _require_draft_identity(actor, session_key)
    draft = _get_owned_draft(kwargs.get('draft_id'), actor, session_key)
    if draft.status not in ('draft', 'proposed'):
        raise ToolError("This draft was already used or abandoned.")
    claimed = (draft.owner_email or '').strip()
    if claimed and actor and actor != claimed:
        raise ToolError(
            "This draft was started for a different email address."
        )
    creator = claimed or actor
    if not creator:
        raise ToolError(
            "I still need your email address before creating this escrow."
        )
    form = ContractCreationForm({
        'title': draft.title,
        'description': draft.description,
        'amount': draft.amount,
        'fee_payer': draft.fee_payer,
        'creator_email': creator,
        'creator_role': draft.creator_role,
        'counterparty_email': draft.counterparty_email,
        'inspection_days': draft.inspection_days,
    })
    if not form.is_valid():
        raise ToolError(
            "The draft is incomplete: "
            + "; ".join(
                f"{field}: {', '.join(errors)}"
                for field, errors in form.errors.items()
            )
        )
    if actor == creator:
        contract = create_escrow_from_validated(form.cleaned_data)
        draft.status = 'converted'
        draft.save(update_fields=['status'])
        issue_otp(actor, 'create')
        audit('escrow.created_via_agent', actor=actor, escrow=contract)
        return {'ok': True, 'data': {
            'reference': contract.code,
            'status': contract.status,
            'next': 'Verify the emailed code to activate the escrow.',
        }}
    # Unverified claim: prove the creator email first. The code is sent now;
    # the user pastes it in chat, the backend verifies it, and this exact
    # call resumes automatically — nothing is created before proof.
    # Re-confirming while a code is live must not burn the cooldown into an
    # error: the user is still on the same step, so remind them to paste it.
    from escrow.models import AuthOTP
    from django.utils import timezone as _tz
    if issue_otp(creator, 'create') is None:
        live = AuthOTP.objects.filter(
            email=creator, intent='create', is_used=False,
            expires_at__gt=_tz.now(),
        ).exists()
        if live:
            audit('escrow.creation_challenged', actor=creator,
                  metadata={'draft_id': draft.id, 'resend_skipped': True})
            return {'ok': True, 'data': {
                'verification_required': True,
                'email': creator,
                'draft_id': draft.id,
                'next': (
                    f"A verification code is already on its way to {creator}. "
                    "Ask the user to paste it here; creation completes on verification."
                ),
            }}
        raise ToolError(
            "I could not send a verification code just now. "
            "Please try confirming again in a minute."
        )
    audit('escrow.creation_challenged', actor=creator,
          metadata={'draft_id': draft.id})
    return {'ok': True, 'data': {
        'verification_required': True,
        'email': creator,
        'draft_id': draft.id,
        'next': (
            f"A verification code is on its way to {creator}. "
            "Ask the user to paste it here; creation completes on verification."
        ),
    }}


def tool_request_counterparty_invitation(actor, **kwargs):
    actor = _require_actor(actor)
    contract = _get_participant_contract(kwargs.get('escrow_reference'), actor)
    audit('invitation.requested', actor=actor, escrow=contract)
    return {'ok': True, 'data': {
        'counterparty_email': contract.counterparty_email,
        'join_path': reverse('join_escrow_page'),
        'code': contract.code,
        'note': 'Share the join page and code with the counterparty.',
    }}


def tool_request_payment_link(actor, **kwargs):
    actor = _require_actor(actor)
    contract = _get_participant_contract(kwargs.get('escrow_reference'), actor)
    if actor != contract.buyer_email:
        raise ToolError("Only the verified buyer can fund this escrow.")
    if contract.status != 'awaiting_funding':
        raise ToolError(
            f"This escrow cannot be funded right now (status: {contract.status})."
        )
    base_url = kwargs.get('_base_url', '')
    result = get_payment_gateway().initialize_vault_payment(
        contract,
        base_url + reverse('payment_callback', kwargs={'code': contract.code}),
    )
    if not result.get('status') or not result.get('auth_url'):
        raise ToolError("The payment could not be started. Please try again.")
    audit('payment_link.requested', actor=actor, escrow=contract)
    return {'ok': True, 'data': {'payment_url': result['auth_url']}}


def tool_request_delivery_confirmation(actor, **kwargs):
    actor = _require_actor(actor)
    contract = _get_participant_contract(kwargs.get('escrow_reference'), actor)
    stage = kwargs.get('stage')
    if stage == 'dispatch' and actor == contract.seller_email:
        target = 'in_transit'
    elif stage == 'receipt' and actor == contract.buyer_email:
        target = 'in_inspection'
    else:
        raise ToolError(
            "Stage must be 'dispatch' (seller) or 'receipt' (buyer), "
            "matching your role in this escrow."
        )
    if not contract.can_transition_to(target):
        raise ToolError(
            f"This escrow cannot move to {target} from {contract.status}."
        )
    contract.transition_to(
        target,
        **({'seller_confirmed': True} if stage == 'dispatch'
           else {'buyer_confirmed': True}),
    )
    audit(f'delivery.{stage}_confirmed', actor=actor, escrow=contract)
    return {'ok': True, 'data': {'reference': contract.code, 'status': contract.status}}


def tool_request_dispute_submission(actor, **kwargs):
    actor = _require_actor(actor)
    contract = _get_participant_contract(kwargs.get('escrow_reference'), actor)
    if not contract.can_transition_to('disputed'):
        raise ToolError(
            f"A dispute cannot be raised from status {contract.status}."
        )
    reason = (kwargs.get('reason') or '').strip()
    contract.transition_to('disputed')
    audit('dispute.submitted', actor=actor, escrow=contract,
          metadata={'reason': reason[:500]})
    return {'ok': True, 'data': {
        'reference': contract.code,
        'status': contract.status,
        'note': 'Funds are held until the dispute is resolved.',
    }}


HANDLERS = {
    'get_platform_information': tool_get_platform_information,
    'get_fee_quote': tool_get_fee_quote,
    'validate_escrow_draft': tool_validate_escrow_draft,
    'get_authorized_escrow_status': tool_get_authorized_escrow_status,
    'create_escrow_draft': tool_create_escrow_draft,
    'update_escrow_draft': tool_update_escrow_draft,
    'get_escrow_draft': tool_get_escrow_draft,
    'request_escrow_creation': tool_request_escrow_creation,
    'request_counterparty_invitation': tool_request_counterparty_invitation,
    'request_payment_link': tool_request_payment_link,
    'request_delivery_confirmation': tool_request_delivery_confirmation,
    'request_dispute_submission': tool_request_dispute_submission,
}


def _schema(name, description, properties, required=()):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': list(required),
                'additionalProperties': False,
            },
        },
    }


def _str(description):
    return {'type': 'string', 'description': description}


TOOLS = [
    _schema('get_platform_information',
            'Explain what Trustcrow is and how escrow works here.',
            {}, []),
    _schema('get_fee_quote',
            'Quote the service fee and totals for an amount.',
            {'amount': _str('Transaction amount in NGN, e.g. "500000".'),
             'fee_payer': _str("'buyer' or 'seller'.")},
            ['amount']),
    _schema('validate_escrow_draft',
            'Check proposed escrow terms without creating anything.',
            {'title': _str('What is being bought or delivered.'),
             'description': _str('Terms, condition, delivery details.'),
             'amount': _str('Agreed amount in NGN.'),
             'fee_payer': _str("'buyer' or 'seller'."),
             'creator_email': _str('Email of the person creating.'),
             'creator_role': _str("'buyer' or 'seller' role of the creator."),
             'counterparty_email': _str('Email of the other party.'),
             'inspection_days': _str('Inspection window, 1-30 days.')},
            ['title', 'description', 'amount', 'fee_payer',
             'creator_email', 'creator_role', 'counterparty_email']),
    _schema('get_authorized_escrow_status',
            'Read the status of an escrow the user participates in.',
            {'escrow_reference': _str('The escrow code.')},
            ['escrow_reference']),
    _schema('create_escrow_draft',
            'Save proposed terms as a draft — partial is fine, save early '
            'and top up as details arrive. Nothing is created yet. Only '
            'title, amount, creator_email and counterparty_email truly need '
            'the user; assume fee_payer buyer, inspection_days 3, and reuse '
            'the title as description unless told otherwise.',
            {'title': _str('What is being bought or delivered.'),
             'description': _str('Terms and delivery details.'),
             'amount': _str('Agreed amount in NGN.'),
             'currency': _str('Currency code, default NGN.'),
             'fee_payer': _str("'buyer' or 'seller'."),
             'creator_email': _str('The user’s own email address.'),
             'creator_role': _str('Your role: buyer or seller.'),
             'counterparty_email': _str('Email of the other party.'),
             'inspection_days': _str('Inspection window, 1-30 days.')},
            []),
    _schema('update_escrow_draft',
            'Top up or fix your draft with newly learned details before it '
            'is used.',
            {'draft_id': {'type': 'integer', 'description': 'Draft id.'},
             'title': _str('New title.'), 'description': _str('New terms.'),
             'amount': _str('New amount in NGN.'),
             'fee_payer': _str("'buyer' or 'seller'."),
             'creator_email': _str('The user’s own email address.'),
             'creator_role': _str('Your role: buyer or seller.'),
             'counterparty_email': _str('Email of the other party.'),
             'inspection_days': _str('Inspection window, 1-30 days.')},
            ['draft_id']),
    _schema('get_escrow_draft',
            'Read back your saved draft.',
            {'draft_id': {'type': 'integer', 'description': 'Draft id.'}},
            ['draft_id']),
    _schema('request_escrow_creation',
            'Create the escrow from a confirmed draft. Only call after the '
            'user explicitly confirmed the exact terms. For an unverified '
            'chat this sends a code to the creator email; the user pastes '
            'it here in chat and creation completes on verification.',
            {'draft_id': {'type': 'integer', 'description': 'Draft id.'}},
            ['draft_id']),
    _schema('request_counterparty_invitation',
            'Get the invite details for the counterparty. Only call after '
            'the user confirmed they want to invite now.',
            {'escrow_reference': _str('The escrow code.')},
            ['escrow_reference']),
    _schema('request_payment_link',
            'Start buyer funding and get the Paystack checkout link. Only '
            'call after the buyer explicitly confirmed they will pay now.',
            {'escrow_reference': _str('The escrow code.')},
            ['escrow_reference']),
    _schema('request_delivery_confirmation',
            'Confirm dispatch (seller) or receipt (buyer). Only call after '
            'the user explicitly confirmed the goods or service.',
            {'escrow_reference': _str('The escrow code.'),
             'stage': _str("'dispatch' or 'receipt'.")},
            ['escrow_reference', 'stage']),
    _schema('request_dispute_submission',
            'Raise a dispute, freezing release until resolution. Only call '
            'after the user explicitly confirmed.',
            {'escrow_reference': _str('The escrow code.'),
             'reason': _str('What went wrong, in the user’s words.')},
            ['escrow_reference']),
]


def execute(name, arguments, actor_email=None, base_url='', session_key=''):
    """Run one approved tool. Unknown tools and auth failures are errors,
    never exceptions — the loop relays them to the model."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {'ok': False, 'error': f"Unknown tool '{name}'."}
    try:
        # Underscore keys are server plumbing, never model input: a model
        # argument can never override the callback host or session binding.
        args = {k: v for k, v in dict(arguments or {}).items()
                if not k.startswith('_')}
        result = handler(actor_email, _base_url=base_url,
                         _session_key=session_key, **args)
    except ToolError as exc:
        logger.info("Tool %s refused: %s", name, exc)
        return {'ok': False, 'error': str(exc)}
    except Exception:
        logger.exception("Tool %s crashed", name)
        return {'ok': False, 'error': 'The operation failed. Please try again.'}
    if not isinstance(result, dict) or 'ok' not in result:
        return {'ok': False, 'error': 'The operation failed. Please try again.'}
    return result
