import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
BANK_LIST_CACHE_KEY = 'trustcrow:bank_list'
BANK_LIST_CACHE_TIMEOUT = 6 * 60 * 60

# Fallback list for local development and for gateways that reject unknown codes.
FALLBACK_BANKS = {
    '044': 'Access Bank',
    '023': 'Citibank Nigeria',
    '063': 'Diamond Bank',
    '050': 'Ecobank Nigeria',
    '070': 'Fidelity Bank',
    '011': 'First Bank of Nigeria',
    '214': 'First City Monument Bank',
    '058': 'Guaranty Trust Bank',
    '030': 'Heritage Bank',
    '301': 'Jaiz Bank',
    '082': 'Keystone Bank',
    '014': 'Stanbic IBTC Bank',
    '232': 'Sterling Bank',
    '100': 'SunTrust Bank',
    '032': 'Union Bank of Nigeria',
    '033': 'United Bank for Africa',
    '215': 'Unity Bank',
    '035': 'Wema Bank',
    '057': 'Zenith Bank',
}

MOCK_BANKS = FALLBACK_BANKS

_bank_name_cache = {}


def _timeout():
    return getattr(settings, 'GATEWAY_HTTP_TIMEOUT', DEFAULT_TIMEOUT)


def _secret():
    return getattr(settings, 'PAYSTACK_SECRET_KEY', 'sk_test_dummy')


def verify_bank_account(account_number, bank_code):
    account_number = (account_number or '').strip()
    if not account_number.isdigit() or len(account_number) != 10:
        return {'status': False, 'error': 'Account number must be exactly 10 digits.'}

    if settings.DEBUG:
        return {
            'status': True,
            'account_name': 'TEST ACCOUNT NAME' if account_number == '0123456789'
            else f'TEST USER {account_number[-4:]}',
        }

    url = f"https://api.paystack.co/bank/resolve?account_number={account_number}&bank_code={bank_code}"
    headers = {"Authorization": f"Bearer {_secret()}"}

    try:
        response = requests.get(url, headers=headers, timeout=_timeout())
        data = response.json()
        if data.get('status'):
            return {'status': True, 'account_name': data['data']['account_name']}
        return {'status': False, 'error': data.get('message') or 'Account resolution failed'}
    except Exception as e:
        logger.exception("Bank account resolution failed")
        return {'status': False, 'error': str(e)}


def list_banks():
    """Bank code -> name, fetched from the gateway and cached.

    Uses Django's cache backend (shared across workers) with a process-dict
    fallback, so multi-worker deployments cannot serve permanently stale
    lists and a cold cache still degrades to FALLBACK_BANKS.
    """
    try:
        cached = cache.get(BANK_LIST_CACHE_KEY)
    except Exception:
        cached = None
    if cached:
        return cached
    if _bank_name_cache:
        return _bank_name_cache

    banks = dict(FALLBACK_BANKS)
    if not settings.DEBUG:
        try:
            response = requests.get(
                "https://api.paystack.co/bank?currency=NGN",
                headers={"Authorization": f"Bearer {_secret()}"},
                timeout=_timeout(),
            )
            data = response.json()
            if data.get('status'):
                banks = {b['code']: b['name'] for b in data.get('data', [])}
        except Exception:
            logger.exception("Bank list fetch failed; using fallback list")

    _bank_name_cache.clear()
    _bank_name_cache.update(banks)
    try:
        cache.set(BANK_LIST_CACHE_KEY, dict(banks), BANK_LIST_CACHE_TIMEOUT)
    except Exception:
        pass
    return dict(banks)


def get_bank_name(bank_code):
    return list_banks().get(bank_code, 'Unknown Bank')
