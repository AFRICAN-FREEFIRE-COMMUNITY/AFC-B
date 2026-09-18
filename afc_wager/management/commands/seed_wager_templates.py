"""
manage.py seed_wager_templates - the six market kinds AFC's match stats can settle, plus a
manual one. Idempotent: existing codes are updated, nothing is deleted. Run after the first
migrate on any box (the deploy script runs makemigrations + migrate; this is the one extra line,
like the WhatsApp template sync).
"""
from django.core.management.base import BaseCommand

from afc_wager.models import MarketTemplate as T

TEMPLATES = [
    ("match_winner", "Match winner", "Which team wins the match (placement 1).",
     T.OPTIONS_TEAMS, T.SETTLE_TEAM_PLACEMENT_1, True, 10),
    ("team_most_kills", "Most kills (team)", "Which team records the most kills in the match.",
     T.OPTIONS_TEAMS, T.SETTLE_TEAM_MOST_KILLS, True, 20),
    ("player_most_kills", "Most kills (player)", "Which player records the most kills in the match.",
     T.OPTIONS_PLAYERS, T.SETTLE_PLAYER_MOST_KILLS, True, 30),
    ("match_mvp", "Match MVP", "Who is named MVP of the match.",
     T.OPTIONS_PLAYERS, T.SETTLE_MATCH_MVP, True, 40),
    ("total_kills_over_under", "Total kills over / under", "Will the match's total kills be over or under the line.",
     T.OPTIONS_OVER_UNDER, T.SETTLE_TOTAL_KILLS_OVER_UNDER, True, 50),
    ("custom", "Custom (settled by hand)", "Any question with custom answers; an admin picks the result.",
     T.OPTIONS_CUSTOM, T.SETTLE_MANUAL, False, 90),
]


class Command(BaseCommand):
    help = "Create or update the wager market templates."

    def handle(self, *args, **options):
        created = updated = 0
        for code, name, description, source, rule, needs_match, order in TEMPLATES:
            row, was_created = T.objects.update_or_create(
                code=code,
                defaults={"name": name, "description": description, "option_source": source,
                          "settle_rule": rule, "needs_match": needs_match, "sort_order": order, "is_active": True},
            )
            created += was_created
            updated += not was_created
        self.stdout.write(f"wager templates: {created} created, {updated} updated")
