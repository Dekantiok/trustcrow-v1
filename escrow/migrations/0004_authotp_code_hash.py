from django.db import migrations, models


class Migration(migrations.Migration):
    """Store only a hash of one-time codes (spec: no plaintext secrets).

    Any live plaintext codes issued before this migration simply stop
    verifying — check_password rejects non-hash values — and expire
    within 15 minutes of issuance.
    """

    dependencies = [
        ("escrow", "0003_state_machine_and_audit_fields"),
    ]

    operations = [
        migrations.RenameField(
            model_name="authotp",
            old_name="otp_code",
            new_name="code_hash",
        ),
        migrations.AlterField(
            model_name="authotp",
            name="code_hash",
            field=models.CharField(max_length=128),
        ),
    ]
