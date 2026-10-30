import django.utils.timezone
from django.db import migrations, models


def add_created_at(apps, schema_editor):
    """Backfill the new timestamp columns with the current time."""
    now = django.utils.timezone.now()
    EscrowContract = apps.get_model('escrow', 'EscrowContract')
    AuthOTP = apps.get_model('escrow', 'AuthOTP')
    EscrowContract.objects.filter(created_at__isnull=True).update(created_at=now)
    AuthOTP.objects.filter(created_at__isnull=True).update(created_at=now)


def normalize_emails(apps, schema_editor):
    """
    Canonicalise stored emails so lookups, session checks and comparisons agree.

    Rows already saved in lower case are left untouched.
    """
    EscrowContract = apps.get_model('escrow', 'EscrowContract')
    for field in ('creator_email', 'counterparty_email'):
        seen = set()
        rows = EscrowContract.objects.exclude(**{f'{field}__isnull': True})
        for pk, value in rows.values_list('pk', field):
            canonical = (value or '').strip().lower()
            if canonical != value and canonical not in seen:
                seen.add(canonical)
                EscrowContract.objects.filter(pk=pk).update(**{field: canonical})

    AuthOTP = apps.get_model('escrow', 'AuthOTP')
    seen = set()
    for pk, value in AuthOTP.objects.values_list('pk', 'email'):
        canonical = (value or '').strip().lower()
        if canonical != value and canonical not in seen:
            seen.add(canonical)
            AuthOTP.objects.filter(pk=pk).update(email=canonical)


class Migration(migrations.Migration):

    dependencies = [
        ('escrow', '0002_alter_escrowcontract_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='escrowcontract',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='authotp',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterModelOptions(
            name='escrowcontract',
            options={'ordering': ['-id']},
        ),
        migrations.AlterModelOptions(
            name='authotp',
            options={'ordering': ['-id']},
        ),
        migrations.AddIndex(
            model_name='escrowcontract',
            index=models.Index(
                fields=['status', 'inspection_ends'],
                name='escrow_status_inspection_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='authotp',
            index=models.Index(
                fields=['email', 'intent', 'is_used'],
                name='otp_lookup_idx',
            ),
        ),
        migrations.RunPython(add_created_at, migrations.RunPython.noop),
        migrations.RunPython(normalize_emails, migrations.RunPython.noop),
    ]
