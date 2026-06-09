"""
Idempotent seeding of the super_admin role + its default holder.

Why a command (not a data migration): this repo keeps Django migrations GITIGNORED (they are
generated on the server), so a committed data migration would not travel. A management command does,
and can be re-run safely on every deploy.

What it does:
  1. Ensures a Roles row exists for super_admin (and head_admin, for safety).
  2. Grants super_admin to the default owner email (ladilawalt@gmail.com, overridable via --email),
     and sets that user's base role to "admin" so existing admin gates keep working.

super_admin is the top role: it can manage the super_admin role itself, and a head_admin cannot
remove it (enforced in afc_auth/views.py assign_roles_to_user / edit_user_roles).

Run on prod after deploy:  python manage.py ensure_super_admin
Optionally:                python manage.py ensure_super_admin --email someone@example.com
"""
from django.core.management.base import BaseCommand

from afc_auth.models import Roles, User, UserRoles

DEFAULT_SUPER_ADMIN_EMAIL = "ladilawalt@gmail.com"


class Command(BaseCommand):
    help = "Ensure the super_admin role exists and is granted to the default owner (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default=DEFAULT_SUPER_ADMIN_EMAIL,
            help=f"Email of the user to grant super_admin (default: {DEFAULT_SUPER_ADMIN_EMAIL}).",
        )

    def handle(self, *args, **options):
        # 1) Seed the roles (idempotent). description is required (no default on the model).
        super_role, _ = Roles.objects.get_or_create(
            role_name="super_admin",
            defaults={"description": "Top-level role with full access; only a super admin can grant or remove it."},
        )
        Roles.objects.get_or_create(
            role_name="head_admin",
            defaults={"description": "Head administrator."},
        )
        self.stdout.write(self.style.SUCCESS(f"super_admin role ready (role_id={super_role.role_id})."))

        # 2) Grant super_admin to the target user, if they exist.
        email = options["email"]
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                f"No user with email {email!r} yet - role seeded but not granted. "
                f"Re-run this command after that account exists."
            ))
            return

        # Keep the base role consistent with the existing admin gates.
        if user.role != "admin":
            user.role = "admin"
            user.save(update_fields=["role"])

        _, created = UserRoles.objects.get_or_create(user=user, role=super_role)
        if created:
            self.stdout.write(self.style.SUCCESS(f"Granted super_admin to {user.username} <{email}>."))
        else:
            self.stdout.write(f"{user.username} <{email}> already has super_admin - nothing to do.")
