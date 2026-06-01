from django.db import migrations


class Migration(migrations.Migration):
    """Rename GhostTeam.region -> country (AFC tracks team origin by country, not
    macro-region). No data migration needed — the ghost-team feature has no rows yet."""

    dependencies = [
        ("afc_rankings", "0002_remove_annualleaderboardentry_uniq_year_team_annual_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="ghostteam",
            old_name="region",
            new_name="country",
        ),
    ]
