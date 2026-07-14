# Demo seed for the Organizer feature — idempotent (re-runnable).
# Creates a few organizations with owners + sub-organizers at varied permission levels
# so every surface shows populated data:
#   - /a/organizations (admin list + detail/members tabs)
#   - /organizer/* (headadmin is a member of TWO orgs -> the org switcher appears;
#                   owner of one, limited sub-organizer of the other)
#   - /organizations/<slug> (public branded pages)
# All demo users share the password "Demo@12345". Run:
#   .venv/Scripts/python.exe manage.py shell -c "exec(open('seed_organizers_demo.py').read())"
from afc_auth.models import User, Roles, UserRoles
from afc_organizers.models import Organization, OrganizationMember

PW = "Demo@12345"
organizer_role, _ = Roles.objects.get_or_create(role_name="organizer")


def grant_organizer(u):
    UserRoles.objects.get_or_create(user=u, role=organizer_role)


def user(username, full_name, email):
    u = User.objects.filter(username=username).first()
    if not u:
        u = User.objects.create_user(username=username, email=email, password=PW,
                                     full_name=full_name, role="player")
    return u


def org(slug, name, email, description, socials):
    o, _ = Organization.objects.get_or_create(
        slug=slug,
        defaults=dict(name=name, email=email, description=description, socials=socials, status="active"),
    )
    # keep an existing org's profile fresh on re-run
    o.name, o.email, o.description, o.socials, o.status = name, email, description, socials, "active"
    o.save()
    return o


def member(o, u, role="sub_organizer", **perms):
    m, _ = OrganizationMember.objects.get_or_create(organization=o, user=u, defaults={"role": role})
    m.role = role
    m.status = "active"
    for k, v in perms.items():
        setattr(m, k, v)
    m.save()
    grant_organizer(u)
    return m


# ── people ───────────────────────────────────────────────────────────────────
headadmin = User.objects.get(username="headadmin")
nova_owner = user("demo_nova_owner", "Nimi Novak", "nova.owner@demo.afc")
apex_owner = user("demo_apex_owner", "Ade Apex", "apex.owner@demo.afc")
alex = user("demo_sub_alex", "Alex Stone", "alex@demo.afc")
bola = user("demo_sub_bola", "Bola Ade", "bola@demo.afc")
chidi = user("demo_sub_chidi", "Chidi Eze", "chidi@demo.afc")
dami = user("demo_sub_dami", "Dami Cole", "dami@demo.afc")

# ── Org 1: AFC Demo Org (headadmin = OWNER) ──────────────────────────────────
afc = org("afc-demo-org", "AFC Demo Org", "demo@afc.com",
          "Demo organization for verifying the organizer feature. Runs Free Fire community tournaments.",
          {"x": "https://x.com/afc", "instagram": "https://instagram.com/afc",
           "youtube": "https://youtube.com/@afc", "discord": "https://discord.gg/afc"})
member(afc, headadmin, role="owner")
member(afc, alex, can_create_events=True, can_edit_events=True, can_upload_results=True)
member(afc, bola, can_manage_registrations=True, can_view_metrics=True, can_view_reviews=True)

# ── Org 2: Nova Esports (headadmin = SUB-ORGANIZER, limited) ──────────────────
nova = org("nova-esports", "Nova Esports", "contact@novaesports.gg",
           "Premier Free Fire org running weekly community cups across West Africa.",
           {"x": "https://x.com/novaesports", "instagram": "https://instagram.com/novaesports",
            "youtube": "https://youtube.com/@novaesports", "discord": "https://discord.gg/nova"})
member(nova, nova_owner, role="owner")
# headadmin is a LIMITED sub here -> Profile read-only, member mgmt hidden, but can see metrics/reviews
member(nova, headadmin, can_view_metrics=True, can_manage_registrations=True, can_view_reviews=True)
member(nova, chidi, can_create_events=True, can_manage_members=True)

# ── Org 3: Apex Gaming (headadmin NOT a member -> only in /a/organizations) ───
apex = org("apex-gaming", "Apex Gaming", "hello@apexgaming.gg",
           "Competitive Free Fire tournaments plus grassroots scrims and talent scouting.",
           {"x": "https://x.com/apexgaming", "instagram": "https://instagram.com/apexgaming",
            "youtube": "", "discord": "https://discord.gg/apex"})
member(apex, apex_owner, role="owner")
member(apex, dami, can_upload_results=True, can_submit_designs=True)

# ── summary ──────────────────────────────────────────────────────────────────
print("Organizations:")
for o in Organization.objects.filter(slug__in=["afc-demo-org", "nova-esports", "apex-gaming"]).order_by("slug"):
    subs = o.members.filter(status="active").count()
    print(f"  - {o.name} ({o.slug}) | status={o.status} | members={subs}")
print("\nheadadmin memberships (drives the organizer dashboard switcher):")
for m in OrganizationMember.objects.filter(user=headadmin, status="active").select_related("organization"):
    print(f"  - {m.organization.name}: {m.role}")
print(f"\nDemo users password: {PW}")
print("SEED DONE.")
