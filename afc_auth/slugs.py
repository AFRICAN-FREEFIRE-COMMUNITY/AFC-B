"""
afc_auth/slugs.py - the one place a thing gets its address from its name, and keeps every
address it has ever had.

Owner's rule R22 (2026-09-13, "slugs everywhere, ids nowhere"): no numeric id in a URL a person
can see. The slug FOLLOWS the name (rename the thing, its address changes with it), every address
it has ever had keeps working (the retired slug goes into SlugHistory and the view answers with a
move the page follows), and a move is reported in the envelope with 200 rather than a 301, because
fetch() follows redirects transparently and a 301 carrying a frontend path would be chased against
the API host and arrive as a 404.

HOW IT CONNECTS
  - Models: a model with a `slug` SlugField and a name-like source field calls sync_slug(self) in
    save() BEFORE super().save() (Product in afc_shop/models.py is the first). When save() runs
    with update_fields that carries the source field, sync_slug adds "slug" to it; without that a
    rename computes the new slug and silently drops it, which is the entire rename path.
  - SlugHistory (afc_auth/models.py): (app_label, model, old_slug) -> object pk, written by
    sync_slug when a slug changes, read by resolve_or_redirect.
  - Views: resolve_or_redirect(Model, ref) answers (obj, moved_to_slug). `ref` may be the current
    slug (moved_to is None), a retired slug (moved_to is the current one), or a legacy numeric id
    (moved_to is the current slug, so an old /shop/12 link still opens the right page and the
    address is rewritten). The view puts `moved_to` on the envelope; the page does router.replace.
  - Tests: afc_auth/test_slugs.py.
"""
from __future__ import annotations

import secrets

from django.utils.text import slugify

# A slug never collides with a legacy numeric id: "12" would be ambiguous in resolve_or_redirect,
# so a name that slugifies to digits only gets a suffix.
_MAX_SUFFIX_TRIES = 1000


def unique_slug(model, base: str, exclude_pk=None, max_length: int = 90) -> str:
    """The first free slug for `base` on `model`: base, base-2, base-3, ... never digits only."""
    base = (slugify(base) or "item")[:max_length].strip("-") or "item"
    if base.isdigit():
        base = f"{base}-item"
    candidate = base
    i = 2
    while i < _MAX_SUFFIX_TRIES:
        qs = model.objects.filter(slug=candidate)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists() and not _history_holds(model, candidate, exclude_pk):
            return candidate
        suffix = f"-{i}"
        candidate = f"{base[: max_length - len(suffix)]}{suffix}"
        i += 1
    raise RuntimeError(f"could not find a free slug for {base!r} on {model.__name__}")


def _history_holds(model, slug: str, exclude_pk=None) -> bool:
    """A retired slug that still points at ANOTHER object must not be reissued: the old link would
    open the wrong page."""
    from afc_auth.models import SlugHistory
    qs = SlugHistory.objects.filter(app_label=model._meta.app_label, model=model._meta.model_name, old_slug=slug)
    if exclude_pk is not None:
        qs = qs.exclude(object_pk=str(exclude_pk))
    return qs.exists()


def sync_slug(instance, source_field: str = "name", update_fields=None):
    """Called from save() before super().save(). Computes the slug from the source field when the
    slug is empty or the name changed, records the retired slug in SlugHistory, and returns the
    update_fields to pass on (with "slug" added when the caller narrowed the save)."""
    model = type(instance)
    wanted_base = getattr(instance, source_field) or ""
    current = instance.slug or ""
    # Keep the current slug when it already derives from this name (a save that did not rename).
    if current and _derived_from(current, wanted_base):
        return update_fields
    new_slug = unique_slug(model, wanted_base, exclude_pk=instance.pk)
    if new_slug == current:
        return update_fields
    if current and instance.pk is not None:
        from afc_auth.models import SlugHistory
        SlugHistory.objects.get_or_create(
            app_label=model._meta.app_label, model=model._meta.model_name, old_slug=current,
            defaults={"object_pk": str(instance.pk)},
        )
    instance.slug = new_slug
    if update_fields is not None and "slug" not in update_fields:
        update_fields = list(update_fields) + ["slug"]
    return update_fields


def _derived_from(slug: str, name: str) -> bool:
    base = slugify(name) or ""
    if not base:
        return False
    if base.isdigit():
        base = f"{base}-item"
    if slug == base:
        return True
    # base-2, base-3 ... are the uniqueness suffixes of the same name
    tail = slug[len(base):] if slug.startswith(base) else ""
    return bool(tail) and tail.startswith("-") and tail[1:].isdigit()


# ── opaque public tokens, for things that have no name ───────────────────────────────────────
# An order and a market application cannot be named, so their address carries a token such as
# `o_7f3a9c2b`: stable, not enumerable, not the database key (sequential ids in URLs let anybody
# walk the whole table by counting). The prefix says what it is; ten hex characters is 40 bits.

def new_public_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def ensure_public_token(instance, prefix: str, field: str = "public_token", update_fields=None):
    """Called from save() before super().save(): fills the token once, never changes it. Returns
    the update_fields to pass on (with the field added when the caller narrowed the save)."""
    if getattr(instance, field, None):
        return update_fields
    model = type(instance)
    for _ in range(20):
        token = new_public_token(prefix)
        if not model.objects.filter(**{field: token}).exists():
            break
    else:  # pragma: no cover - 40 bits colliding twenty times in a row
        raise RuntimeError(f"could not mint a public token for {model.__name__}")
    setattr(instance, field, token)
    if update_fields is not None and field not in update_fields:
        update_fields = list(update_fields) + [field]
    return update_fields


def resolve_by_token(model, ref: str | None, prefix: str, field: str = "public_token", **filters):
    """(obj, moved_to_token): the object for its token (moved_to None) or for a legacy numeric id
    (moved_to = its token, minted on the spot for a row that predates tokens). `filters` narrow the
    lookup (an order is only ever resolved for its own buyer). (None, None) when nothing matches."""
    if not ref:
        return None, None
    ref = str(ref).strip()
    if ref.startswith(prefix + "_"):
        return model.objects.filter(**{field: ref}, **filters).first(), None
    if ref.isdigit():
        obj = model.objects.filter(pk=int(ref), **filters).first()
        if obj is None:
            return None, None
        if not getattr(obj, field, None):
            obj.save(update_fields=[field])
        return obj, getattr(obj, field)
    return None, None


def resolve_or_redirect(model, ref: str | None):
    """(obj, moved_to): the object for a current slug (moved_to None), a retired slug or a legacy
    numeric id (moved_to = the current slug), or (None, None) when nothing matches. Never raises."""
    if not ref:
        return None, None
    ref = str(ref).strip()
    obj = model.objects.filter(slug=ref).first()
    if obj is not None:
        return obj, None
    if ref.isdigit():
        obj = model.objects.filter(pk=int(ref)).first()
        if obj is not None:
            if not obj.slug:  # a row that predates slugs: give it one now, so the move has a target
                obj.save(update_fields=["slug"])
            return obj, obj.slug or None
        return None, None
    from afc_auth.models import SlugHistory
    hist = SlugHistory.objects.filter(app_label=model._meta.app_label, model=model._meta.model_name, old_slug=ref).first()
    if hist is None:
        return None, None
    obj = model.objects.filter(pk=hist.object_pk).first()
    if obj is None:
        return None, None
    return obj, obj.slug
