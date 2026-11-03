from django.conf import settings

from .demo import DemoGateway
from .paystack import PaystackGateway


def get_payment_gateway():
    if getattr(settings, 'DEMO_MODE', False):
        return DemoGateway()
    return PaystackGateway()
