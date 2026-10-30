from django import forms
from django.core.exceptions import ValidationError

from .models import EscrowContract, SettlementVault
from .utils import normalize_email

MIN_ESCROW_AMOUNT = 100
MAX_ESCROW_AMOUNT = 9999999999.99
MIN_INSPECTION_DAYS = 1
MAX_INSPECTION_DAYS = 30


class ContractCreationForm(forms.ModelForm):
    class Meta:
        model = EscrowContract
        fields = [
            'title', 'description', 'amount', 'fee_payer',
            'creator_email', 'creator_role', 'counterparty_email', 'inspection_days',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['amount'].min_value = MIN_ESCROW_AMOUNT
        self.fields['amount'].max_value = MAX_ESCROW_AMOUNT
        self.fields['inspection_days'].min_value = MIN_INSPECTION_DAYS
        self.fields['inspection_days'].max_value = MAX_INSPECTION_DAYS
        # Bootstrap styling for every field (Django renders plain widgets by
        # default, which would appear unstyled on the create page).
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault('class', 'form-select')
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            else:
                widget.attrs.setdefault('class', 'form-control')
        for name in ('creator_email', 'counterparty_email'):
            self.fields[name].widget.attrs.setdefault('autocapitalize', 'none')
            self.fields[name].widget.attrs.setdefault('autocomplete', 'email')
            self.fields[name].widget.attrs.setdefault('spellcheck', 'false')
        placeholders = {
            'title': 'e.g. MacBook Pro M3 or Web Design Milestone',
            'description': 'Specify the item condition, delivery terms, and acceptance criteria...',
            'amount': '50000',
            'creator_email': 'you@example.com',
            'counterparty_email': 'counterparty@example.com',
            'inspection_days': '3',
        }
        for name, ph in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault('placeholder', ph)

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < MIN_ESCROW_AMOUNT:
            raise ValidationError(
                f"The escrow amount must be at least {MIN_ESCROW_AMOUNT:,.2f}."
            )
        return amount

    def clean_inspection_days(self):
        days = self.cleaned_data['inspection_days']
        if days < MIN_INSPECTION_DAYS:
            raise ValidationError("The inspection window must be at least 1 day.")
        if days > MAX_INSPECTION_DAYS:
            raise ValidationError(f"The inspection window cannot exceed {MAX_INSPECTION_DAYS} days.")
        return days

    def clean(self):
        cleaned_data = super().clean()
        creator_email = normalize_email(cleaned_data.get('creator_email'))
        counterparty_email = normalize_email(cleaned_data.get('counterparty_email'))
        if creator_email:
            cleaned_data['creator_email'] = creator_email
        if counterparty_email:
            cleaned_data['counterparty_email'] = counterparty_email
        if creator_email and creator_email == counterparty_email:
            raise ValidationError(
                "The creator and counterparty cannot use the same email address."
            )
        return cleaned_data


class SettlementVaultForm(forms.ModelForm):
    # Bank + account number only. bank_name / account_name are never trusted
    # from the client — seller_add_vault fills them from Paystack's
    # bank/resolve response before saving, so a payee name can't be spoofed.
    # bank_code stays as data (Paystack resolve requires it) but is rendered
    # as a dropdown, not a free-text code field.
    bank_code = forms.ChoiceField(
        label="Bank",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = SettlementVault
        fields = ['bank_code', 'account_number']
        widgets = {
            'account_number': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': 10,
                'pattern': r'\d{10}',
                'inputmode': 'numeric',
                'autocomplete': 'off',
                'placeholder': '10-digit account number',
            }),
        }

    def __init__(self, *args, **kwargs):
        banks = kwargs.pop('banks', None)
        super().__init__(*args, **kwargs)
        if banks is None:
            # Imported here to avoid a forms <-> bank_verification cycle at import.
            from .bank_verification import list_banks
            try:
                banks = list_banks()
            except Exception:
                banks = {}
        self.fields['bank_code'].choices = [('', 'Select your bank')] + [
            (code, name) for code, name in sorted(banks.items(), key=lambda kv: kv[1])
        ]

    def clean_bank_code(self):
        code = (self.cleaned_data.get('bank_code') or '').strip()
        if not code:
            raise ValidationError("Please select your bank.")
        return code

    def clean_account_number(self):
        account_number = (self.cleaned_data.get('account_number') or '').strip()
        if not account_number.isdigit() or len(account_number) != 10:
            raise ValidationError("Enter the 10-digit account number.")
        return account_number
