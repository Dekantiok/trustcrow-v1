import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings

from .base import PaymentGatewayBase

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


def _timeout():
    return getattr(settings, 'GATEWAY_HTTP_TIMEOUT', DEFAULT_TIMEOUT)


class PaystackGateway(PaymentGatewayBase):
    def __init__(self):
        self.secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', 'sk_test_dummy')
        self.base_url = "https://api.paystack.co"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def initialize_vault_payment(self, escrow_record, return_url):
        # Must match EscrowContract.total_charge_kobo, which the webhook
        # validates against. Do not re-derive the fee rule here.
        amount_in_kobo = escrow_record.total_charge_kobo
        payer_email = escrow_record.buyer_email

        payload = {
            "email": payer_email,
            "amount": amount_in_kobo,
            "reference": escrow_record.gateway_reference,
            "callback_url": return_url
        }
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(
                f"{self.base_url}/transaction/initialize",
                json=payload,
                headers=self._headers(),
                timeout=_timeout(),
            )
            data = response.json()
            return {
                "status": data.get("status"),
                "auth_url": data.get("data", {}).get("authorization_url"),
            }
        except Exception as e:
            logger.exception("Paystack transaction initialize failed")
            return {"status": False, "error": str(e)}

    def verify_incoming_webhook(self, request_headers, request_body):
        signature = request_headers.get('x-paystack-signature')
        if not signature:
            logger.warning("Paystack webhook rejected: missing signature")
            return False

        digest = hmac.new(
            self.secret_key.encode('utf-8'),
            request_body,
            digestmod=hashlib.sha512,
        ).hexdigest()

        if not hmac.compare_digest(digest, signature):
            logger.warning("Paystack webhook rejected: signature mismatch")
            return False

        try:
            return json.loads(request_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            logger.warning("Paystack webhook rejected: body was not valid JSON")
            return False

    def _create_transfer_recipient(self, settlement_vault_record):
        """Create (or fetch existing) Paystack recipient; returns recipient_code or None.

        Paystack dedupes on account number, so calling this on every payout
        is idempotent — a retry returns the existing recipient record.
        """
        payload = {
            "type": "nuban",
            "name": settlement_vault_record.account_name,
            "account_number": settlement_vault_record.account_number,
            "bank_code": settlement_vault_record.bank_code,
            "currency": "NGN",
        }
        try:
            response = requests.post(
                f"{self.base_url}/transferrecipient",
                json=payload,
                headers=self._headers(),
                timeout=_timeout(),
            )
            data = response.json()
            if data.get("status"):
                return data.get("data", {}).get("recipient_code")
            logger.error(
                "Paystack recipient creation rejected for account %s: %s",
                settlement_vault_record.masked_account_number,
                data.get("message"),
            )
            return None
        except Exception:
            logger.exception("Paystack recipient creation failed")
            return None

    def execute_seller_payout(self, settlement_vault_record, total_disbursement):
        amount_in_kobo = int(total_disbursement * 100)
        recipient_code = self._create_transfer_recipient(settlement_vault_record)
        if not recipient_code:
            return {"status": False, "error": "recipient creation failed"}

        try:
            contract_code = settlement_vault_record.contract.code
        except Exception:
            contract_code = "unknown"
        payload = {
            "source": "balance",
            "amount": amount_in_kobo,
            "currency": "NGN",
            "recipient": recipient_code,
            "reason": f"Trustcrow escrow {contract_code} settlement",
            "reference": f"trustcrow-{contract_code}-{amount_in_kobo}",
        }
        try:
            response = requests.post(
                f"{self.base_url}/transfer",
                json=payload,
                headers=self._headers(),
                timeout=_timeout(),
            )
            data = response.json()
            if not data.get("status"):
                logger.error(
                    "Paystack transfer rejected for account %s: %s",
                    settlement_vault_record.masked_account_number,
                    data.get("message"),
                )
            return data
        except Exception as e:
            logger.exception("Paystack transfer failed")
            return {"status": False, "error": str(e)}

    def verify_transaction(self, gateway_reference):
        """Fetch a transaction's live status from Paystack.

        Returns the parsed response body, or {"status": False, ...} when the
        lookup itself failed. Callers must still check data.status ==
        "success", the reference echo, and the amount before treating funds
        as received.
        """
        try:
            response = requests.get(
                f"{self.base_url}/transaction/verify/{gateway_reference}",
                headers=self._headers(),
                timeout=_timeout(),
            )
            return response.json()
        except Exception as e:
            logger.exception(
                "Paystack transaction verify failed for %s", gateway_reference
            )
            return {"status": False, "error": str(e)}

    def refund_payment(self, gateway_reference, amount_kobo=None):
        """Attempt a Paystack refund for a captured transaction reference."""
        payload = {"transaction": gateway_reference}
        try:
            response = requests.post(
                f"{self.base_url}/refund",
                json=payload,
                headers=self._headers(),
                timeout=_timeout(),
            )
            return response.json()
        except Exception as e:
            logger.exception("Paystack refund failed for %s", gateway_reference)
            return {"status": False, "error": str(e)}
