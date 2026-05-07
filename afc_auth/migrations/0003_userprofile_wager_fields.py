"""Additive migration for the wager/wallet feature.

Adds 5 nullable fields to UserProfile to support soft-KYC + leaderboard
opt-out. None of the existing afc_auth code reads these fields yet — they
are mirrored on `afc_wallet.KYCTier` for the wallet/wager service path.

Safe to apply on prod: all fields are nullable and unique constraints
permit nulls (so existing rows pass without modification).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("afc_auth", "0002_rename_id_user_user_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="whatsapp_number",
            field=models.CharField(
                blank=True, max_length=24, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="whatsapp_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="discord_user_id",
            field=models.CharField(
                blank=True, max_length=64, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="discord_linked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="show_on_leaderboard",
            field=models.BooleanField(default=True),
        ),
    ]
