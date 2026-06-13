"""
audit_schema - read-only detector for SCHEMA DRIFT between the Django models and the
actual database tables.

WHY (incident 2026-06-13): the prod DB was restored with `migrate --fake-initial`, which
marks an initial migration as applied WITHOUT verifying every column/table actually exists.
A regenerated migration's recorded state then matches the models, so `makemigrations` keeps
saying "no changes" while a column or a many-to-many through table is silently missing
underneath (the roundrobingroup.order column + its teams M2M table were both missing, which
500'd event duplication). This command finds ALL such gaps in one pass so they can be fixed
before a user trips over them, instead of one 500 at a time.

WHAT IT CHECKS, for every concrete model in every installed app:
  - the model's own table exists;
  - every concrete local field's DB column exists on that table;
  - every ManyToMany field's through table exists, and its two FK columns exist.

It NEVER writes anything (pure information_schema reads). It does not detect type
mismatches or extra columns, only MISSING tables/columns (the drift that breaks reads).

RUN:  python manage.py audit_schema
Exit code is 0 always; read the printed report. For each gap it prints the model and the
missing table/column so the fix is an obvious ADD COLUMN / CREATE TABLE.
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import ManyToManyField


class Command(BaseCommand):
    help = "Report DB columns/tables that the models expect but the database is missing (drift detector)."

    def handle(self, *args, **options):
        # All tables that actually exist in the current database.
        with connection.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE()"
            )
            existing_tables = {row[0] for row in cur.fetchall()}

        def columns_of(table):
            """Set of column names on `table`, or None when the table itself is missing."""
            if table not in existing_tables:
                return None
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s",
                    [table],
                )
                return {row[0] for row in cur.fetchall()}

        gaps = []  # (severity, message)

        for model in apps.get_models():
            meta = model._meta
            if not meta.managed:
                continue
            table = meta.db_table
            cols = columns_of(table)
            if cols is None:
                gaps.append(("TABLE", f"{model.__name__}: table `{table}` is MISSING"))
                continue

            # Concrete own columns (skip relations' reverse side; include FK id columns).
            for field in meta.local_concrete_fields:
                col = field.column
                if col and col not in cols:
                    gaps.append((
                        "COLUMN",
                        f"{model.__name__}: column `{table}`.`{col}` is MISSING "
                        f"(field {field.name})",
                    ))

            # ManyToMany through tables + their two join columns.
            for field in meta.local_many_to_many:
                if not isinstance(field, ManyToManyField):
                    continue
                through = field.remote_field.through
                if through is None or not through._meta.auto_created:
                    # Explicit through models are real models; they get checked in the main
                    # loop on their own. Only auto-created M2M tables are checked here.
                    continue
                m2m_table = through._meta.db_table
                m2m_cols = columns_of(m2m_table)
                if m2m_cols is None:
                    gaps.append((
                        "TABLE",
                        f"{model.__name__}.{field.name}: M2M table `{m2m_table}` is MISSING",
                    ))
                    continue
                for jf in through._meta.local_concrete_fields:
                    if jf.column and jf.column not in m2m_cols:
                        gaps.append((
                            "COLUMN",
                            f"{model.__name__}.{field.name}: column `{m2m_table}`.`{jf.column}` is MISSING",
                        ))

        if not gaps:
            self.stdout.write(self.style.SUCCESS(
                "No schema drift found. Every model column and M2M table exists in the database."
            ))
            return

        self.stdout.write(self.style.ERROR(f"Found {len(gaps)} schema gap(s):"))
        for severity, msg in gaps:
            self.stdout.write(f"  [{severity}] {msg}")
        self.stdout.write("")
        self.stdout.write(
            "Each gap is fixable with a plain ADD COLUMN / CREATE TABLE. Send this list to "
            "get the exact DDL (matched to a healthy DB)."
        )
