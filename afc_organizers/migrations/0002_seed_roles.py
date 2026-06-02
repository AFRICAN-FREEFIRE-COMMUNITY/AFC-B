# Data migration: create the two Roles rows the organizer feature relies on.
# Roles is a table of rows (UserRoles points at it), so adding choices in the model is not
# enough — the rows must exist. `organizer` is granted to every active OrganizationMember;
# `organizer_admin` is the AFC staff oversight role (see afc_organizers/permissions.py).
from django.db import migrations


def seed_roles(apps, schema_editor):
    Roles = apps.get_model("afc_auth", "Roles")
    rows = {
        "organizer": "Member of an organization (organizer dashboard access).",
        "organizer_admin": "AFC staff who provision and oversee organizations.",
    }
    for name, desc in rows.items():
        Roles.objects.get_or_create(role_name=name, defaults={"description": desc})


def unseed_roles(apps, schema_editor):
    Roles = apps.get_model("afc_auth", "Roles")
    Roles.objects.filter(role_name__in=("organizer", "organizer_admin")).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("afc_organizers", "0001_initial"),
        ("afc_auth", "0005_alter_roles_role_name"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
