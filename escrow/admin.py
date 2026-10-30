from django.contrib import admin, messages
from django.db.models import Q
from django.utils import timezone

from .gateways import get_payment_gateway
from .models import AuthOTP, EscrowContract, SettlementVault
from .services import confirm_funding_payment, settle_pending_payout


@admin.register(EscrowContract)
class EscrowContractAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'title', 'amount', 'status', 'creator_email',
        'counterparty_email', 'inspection_ends', 'created_at',
    )
    list_filter = ('status', 'creator_role', 'fee_payer')
    search_fields = ('code', 'creator_email', 'counterparty_email', 'title')
    readonly_fields = (
        'code', 'gateway_reference', 'service_fee', 'created_at',
        'inspection_ends', 'buyer_confirmed', 'seller_confirmed',
    )
    date_hierarchy = 'created_at'
    actions = (
        'verify_pending_payment',
        'resolve_dispute_to_payout',
        'resolve_dispute_cancelled',
        'cancel_contract',
        'extend_inspection_window',
    )

    @admin.action(description="Verify pending payment with Paystack")
    def verify_pending_payment(self, request, queryset):
        """Recover escrows stuck in awaiting_funding after a real payment.

        Asks Paystack for each contract's stored reference and advances the
        ones that are actually paid. Safe to re-run: confirmation is a
        conditional UPDATE that only fires once.
        """
        verified = 0
        for contract in queryset:
            if contract.status != 'awaiting_funding' or not contract.gateway_reference:
                self.message_user(
                    request,
                    f"{contract.code} is not awaiting funding; skipped.",
                    level=messages.WARNING,
                )
                continue
            try:
                result = get_payment_gateway().verify_transaction(
                    contract.gateway_reference
                )
            except Exception:
                result = None
            data = result.get('data', {}) if isinstance(result, dict) else {}
            if (
                isinstance(result, dict) and result.get('status')
                and isinstance(data, dict) and data.get('status') == 'success'
                and data.get('reference') == contract.gateway_reference
                and confirm_funding_payment(contract, data.get('amount'))
            ):
                verified += 1
            else:
                self.message_user(
                    request,
                    f"{contract.code}: no successful payment found "
                    f"(check Paystack reference {contract.gateway_reference}).",
                    level=messages.WARNING,
                )
        self.message_user(
            request, f"Verified {verified} payment(s).", level=messages.INFO
        )

    def _cancel(self, request, queryset, verb):
        changed = 0
        for contract in queryset:
            if not contract.can_transition_to('cancelled'):
                self.message_user(
                    request,
                    f"{contract.code} cannot be cancelled from {contract.status}.",
                    level=messages.WARNING,
                )
                continue
            was_funded = contract.status not in EscrowContract.PRE_FUNDING_STATUSES
            contract.transition_to('cancelled')
            changed += 1
            if was_funded:
                self.message_user(
                    request,
                    f"{contract.code} was already funded: process a Paystack refund "
                    f"for reference {contract.gateway_reference} if the buyer was charged.",
                    level=messages.WARNING,
                )
        self.message_user(
            request, f"{verb} {changed} escrow(s).", level=messages.INFO
        )

    @admin.action(description="Resolve dispute: release funds to seller")
    def resolve_dispute_to_payout(self, request, queryset):
        for contract in queryset:
            if not contract.can_transition_to('pending_payout'):
                self.message_user(
                    request,
                    f"{contract.code} cannot be released from {contract.status}.",
                    level=messages.WARNING,
                )
                continue
            contract.transition_to('pending_payout')
            result = settle_pending_payout(contract)
            self.message_user(
                request,
                f"{contract.code}: "
                + ('paid' if result.success else f'NOT paid ({result.reason})'),
                level=messages.INFO if result.success else messages.ERROR,
            )

    @admin.action(description="Resolve dispute: cancel escrow")
    def resolve_dispute_cancelled(self, request, queryset):
        self._cancel(request, queryset, 'Resolved and cancelled')

    @admin.action(description="Cancel escrow")
    def cancel_contract(self, request, queryset):
        self._cancel(request, queryset, 'Cancelled')

    @admin.action(description="Extend inspection window by 3 days")
    def extend_inspection_window(self, request, queryset):
        for contract in queryset:
            if not contract.inspection_ends:
                self.message_user(
                    request,
                    f"{contract.code} has no inspection deadline to extend.",
                    level=messages.WARNING,
                )
                continue
            contract.inspection_ends += timezone.timedelta(days=3)
            contract.save(update_fields=['inspection_ends'])
        self.message_user(request, f"Extended {queryset.count()} escrow(s).",
                          level=messages.INFO)


@admin.register(AuthOTP)
class AuthOTPAdmin(admin.ModelAdmin):
    list_display = ('email', 'intent', 'attempts_left', 'is_used', 'expires_at', 'created_at')
    list_filter = ('intent', 'is_used')
    search_fields = ('email', 'otp_code')
    readonly_fields = ('otp_code', 'created_at')
    actions = ('purge_stale',)

    @admin.action(description="Delete expired or already-used codes")
    def purge_stale(self, request, queryset):
        stale = queryset.filter(Q(is_used=True) | Q(expires_at__lt=timezone.now()))
        count, _ = stale.delete()
        self.message_user(request, f"Deleted {count} stale code(s).", level=messages.INFO)


@admin.register(SettlementVault)
class SettlementVaultAdmin(admin.ModelAdmin):
    list_display = ('contract', 'bank_name', 'masked_account', 'account_name')
    search_fields = ('contract__code', 'account_number', 'account_name')

    @admin.display(description='Account number')
    def masked_account(self, obj):
        return obj.masked_account_number


admin.site.site_header = 'Trustcrow Operations'
admin.site.site_title = 'Trustcrow'
admin.site.index_title = 'Escrow operations'
