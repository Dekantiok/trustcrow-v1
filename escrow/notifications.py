import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


def send_otp_via_termii(email, otp_code):
    if settings.DEBUG:
        # Never emit a live credential to stdout; the dev banner is opt-in.
        if getattr(settings, 'LOG_OTP_TO_CONSOLE', False):
            logger.warning("DEVELOPMENT OTP for %s: %s", email, otp_code)
        return True

    api_key = getattr(settings, 'TERMII_API_KEY', 'dummy_key')
    email_config_id = getattr(settings, 'TERMII_EMAIL_CONFIG_ID', 'default_config')

    url = "https://api.ng.termii.com/api/email/otp/send"

    # Email Token API: `code` is REQUIRED — it is the exact OTP the user
    # receives. The pin_* / message_text fields belong to the SMS Token API
    # and are ignored here; omitting `code` was why production emails never
    # matched the code stored in AuthOTP.
    payload = {
        "api_key": api_key,
        "email_address": email,
        "code": otp_code,
        "email_configuration_id": email_config_id,
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    if api_key in ('', 'dummy_key') or email_config_id in ('', 'default_config'):
        logger.error(
            "Termii OTP not attempted for %s: TERMII_API_KEY/TERMII_EMAIL_CONFIG_ID "
            "look unset in this environment.", email,
        )
        return False

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=getattr(settings, 'GATEWAY_HTTP_TIMEOUT', DEFAULT_TIMEOUT),
        )
        response_data = response.json()
        if response_data.get("code") != "ok":
            logger.error(
                "Termii rejected OTP for %s: %s (response: %r)",
                email, response_data.get("message"), response_data,
            )
        return response_data.get("code") == "ok"
    except Exception:
        logger.exception("Termii OTP delivery failed for %s", email)
        return False