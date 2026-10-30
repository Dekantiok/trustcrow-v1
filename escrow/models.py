
from django.db import models
from django.utils import timezone

from .utils import normalize_email


class TransitionError(Exception):
    """Raised when a status change is not permitted by the state machine."""


class EscrowContract(models.Model):
    STATUS_CHOICES = [
        ('pending_verification', 'Pending Verification'),
        ('awaiting_counterparty', 'Awaiting Counterparty'),
        ('awaiting_funding', 'Awaiting Funding'),
        ('awaiting_dispatch', 'Awaiting Dispatch'),
        ('in_transit', 'In Transit'),
        ('in_inspection', 'In Inspection'),
        ('pending_payout', 'Pending Payout'),
        ('completed', 'Completed'),
        ('disputed', 'Disputed'),
        ('cancelled', 'Cancelled'),
    ]

    ROLE_CHOICES = [
        ('buyer', 'Buyer'),
        ('seller', 'Seller'),
    ]

    # The single source of truth for the escrow lifecycle. Every status change
    # must appear here or transition_to() will refuse it. `cancelled` after
    # funding requires an ops refund check (see admin warning); `disputed`
    # is reachable from either counterparty once value is at risk.
    TRANSITIONS = {
        'pending_verification': {'awaiting_counterparty', 'cancelled'},
        'awaiting_counterparty': {'awaiting_funding', 'cancelled'},
        'awaiting_funding': {'awaiting_dispatch', 'disputed', 'cancelled'},
        'awaiting_dispatch': {'in_transit', 'disputed', 'cancelled'},
        'in_transit': {'in_inspection', 'disputed', 'cancelled'},
        'in_inspection': {'pending_payout', 'disputed', 'cancelled'},
        'pending_payout': {'completed', 'disputed', 'cancelled'},
        'disputed': {'pending_payout', 'cancelled'},
        'completed': set(),
        'cancelled': set(),
    }

    # States where no money has moved yet, so a party can cancel outright
    # with no refund required.
    PRE_FUNDING_STATUSES = {
        'pending_verification', 'awaiting_counterparty', 'awaiting_funding',
    }

    TERMINAL_STATUSES = {'completed', 'cancelled'}

    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=50, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2)
    fee_payer = models.CharField(max_length=10, choices=ROLE_CHOICES)
    creator_email = models.EmailField(db_index=True)
    creator_role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    counterparty_email = models.EmailField(db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending_verification')
    inspection_days = models.IntegerField(default=3)
    inspection_ends = models.DateTimeField(null=True, blank=True)
    buyer_confirmed = models.BooleanField(default=False)
    seller_confirmed = models.BooleanField(default=False)
    gateway_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['status', 'inspection_ends'], name='escrow_status_inspection_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.code})"

    def save(self, *args, **kwargs):
        self.creator_email = normalize_email(self.creator_email)
        self.counterparty_email = normalize_email(self.counterparty_email)
        super().save(*args, **kwargs)

    @property
    def buyer_email(self):
        if self.creator_role == 'buyer':
            return self.creator_email
        return self.counterparty_email

    @property
    def seller_email(self):
        if self.creator_role == 'seller':
            return self.creator_email
        return self.counterparty_email

    @property
    def parties(self):
        return (self.buyer_email, self.seller_email)

    @property
    def seller_receives(self):
        if self.fee_payer == 'seller':
            return self.amount - self.service_fee
        return self.amount

    @property
    def total_charge(self):
        """What the buyer actually pays at checkout (amount + fee if buyer pays)."""
        if self.fee_payer == 'buyer':
            return self.amount + self.service_fee
        return self.amount

    @property
    def total_charge_kobo(self):
        return int(self.total_charge * 100)

    @property
    def is_inspection_expired(self):
        if not self.inspection_ends:
            return False
        return self.inspection_ends <= timezone.now()

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    def can_transition_to(self, target):
        return target in self.TRANSITIONS.get(self.status, set())

    def transition_to(self, target, **fields):
        if not self.can_transition_to(target):
            raise TransitionError(
                f"Cannot move contract {self.code} from {self.status} to {target}."
            )
        for field, value in fields.items():
            setattr(self, field, value)
        self.status = target
        self.save()
        return self


class AuthOTP(models.Model):
    INTENT_CHOICES = [
        ('create', 'Create'),
        ('join', 'Join'),
        ('reauth', 'Reauth'),
    ]

    MAX_ATTEMPTS = 5

    email = models.EmailField(db_index=True)
    otp_code = models.CharField(max_length=6)
    intent = models.CharField(max_length=15, choices=INTENT_CHOICES)
    attempts_left = models.IntegerField(default=MAX_ATTEMPTS)
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['email', 'intent', 'is_used'], name='otp_lookup_idx'),
        ]

    def __str__(self):
        return f"OTP for {self.email} ({self.intent})"

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self):
        return not self.is_used and not self.is_expired and self.attempts_left > 0


class SettlementVault(models.Model):
    contract = models.OneToOneField(
        EscrowContract, on_delete=models.CASCADE, related_name='settlement_vault'
    )
    bank_name = models.CharField(max_length=100)
    bank_code = models.CharField(max_length=10)
    account_number = models.CharField(max_length=10)
    account_name = models.CharField(max_length=150)

    def __str__(self):
        return f"Vault for {self.contract.code}"

    @property
    def masked_account_number(self):
        """Only ever expose the last four digits outside the seller's own view."""
        if len(self.account_number) <= 4:
            return '*' * len(self.account_number)
        return '*' * (len(self.account_number) - 4) + self.account_number[-4:]
