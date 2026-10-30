from .paystack import PaystackGateway

def get_payment_gateway():
    # In the future, we can read a setting here to swap to Squadco or Flutterwave
    return PaystackGateway()