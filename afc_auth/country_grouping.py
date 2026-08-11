# ──────────────────────────────────────────────────────────────────────────────
# ONE COUNTRY, ONE TARGET.
#
# THE PROBLEM THIS EXISTS FOR (found while walking the audience builder, 2026-08-04):
# afc_auth.User.country is a free CharField that has been written by more than one
# source over the years, so the SAME country sits in the table under several
# spellings. Live production data, top of the list:
#
#     'Nigeria' 2892   |   'NG' 1817
#     'South Africa' 123   |   'ZA' 129
#     'Ghana' 38   |   'GH' 107
#     'Cabo Verde' 22   |   'CV' 124   ... and so on for a dozen more.
#
# Grouping by the raw string, which is what the broadcast audience options endpoint
# used to do, therefore offered the admin TWO chips for Nigeria and made each of them
# look like the whole country. Picking "Nigeria" would have reached 2,892 people and
# silently missed 1,817. On a feature whose entire job is "see exactly who this
# reaches before you send", that is the worst possible failure: it is invisible, and
# the number shown is confidently wrong.
#
# THE FIX, AND WHY IT IS A READ-TIME FIX RATHER THAN A DATA MIGRATION:
# every raw value is folded to a canonical key at read time, and a chosen key is
# expanded back to every raw spelling when the filter runs. Rewriting the column
# instead would touch ~6,600 live rows to fix a display and matching problem, and it
# would not stay fixed, because the writers that produced 'NG' and 'Nigeria' are
# still there (registration, profile edit, and the IP lookup that fills ip_country
# all supply their own spelling). Folding on read is correct for every value the
# table already holds AND every value it gains later.
#
# THE CANONICAL KEY is whatever afc_tournament_and_scrims.views.normalize_country
# returns: a pycountry-backed lowercase country name that resolves ISO alpha-2 codes
# ('NG', 'ZA') and full names, fuzzily. That function is ALREADY the house
# normalizer, used by afc_auth.language_utils.language_for_country to pick a new
# user's language, so this module deliberately reuses it rather than introducing a
# second, competing idea of what a country is. It is imported lazily inside the
# helper for the same reason language_utils does it: that views module imports
# afc_auth, so a module-level import would be circular.
#
# UNRESOLVABLE VALUES ARE KEPT, NOT DROPPED. 'Unknown' (16 accounts) is not a
# country and pycountry cannot resolve it, so it folds to itself and remains
# targetable. Losing rows would be a worse bug than showing an odd label.
#
# USED BY:
#   * afc_auth/views_broadcast_audience.py - builds the country chip list, so each
#     country appears once with its true combined count.
#   * afc_auth/audience.py _category_q - expands the picked keys back to raw values
#     so the filter matches every spelling, on both User.country and User.ip_country.
# ──────────────────────────────────────────────────────────────────────────────


def canonical_country(value):
    """Fold one raw country value to its canonical key.

    Returns "" for blank input. Anything pycountry cannot resolve folds to its own
    lowercased, stripped self, so nothing is ever lost.
    """
    if not value:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    try:
        # Lazy: afc_tournament_and_scrims.views imports afc_auth, so importing it at
        # module load would be circular. Same reasoning as language_utils.
        from afc_tournament_and_scrims.views import normalize_country

        folded = normalize_country(text)
        if folded:
            return folded
    except Exception:
        # pycountry missing, circular import, anything: fall through to the plain
        # fold. Worst case the two spellings stay apart, which is exactly today's
        # behaviour, so this can never be worse than not having this module.
        pass

    return text.lower()


def canonical_country_name(value):
    """The proper-cased country NAME to STORE for a typed value, or "" if it is not a country.

    The write-side companion to canonical_country above. That one folds whatever is already in the
    column so two spellings group together; this one decides what a NEW value should look like, so
    the column stops gaining spellings in the first place.

      canonical_country_name("NG")      -> "Nigeria"
      canonical_country_name("nigeria") -> "Nigeria"
      canonical_country_name("Naija")   -> ""          (refused by the caller)

    Returning "" for anything pycountry cannot resolve is the DELIBERATE difference from
    canonical_country, which keeps unknown values rather than losing rows. Here there is no row to
    lose: the caller is a person typing into a form and can be asked to pick a real country.

    USED BY: afc_auth/views_admin_identity.py admin_set_user_country (head-admin repair). Anything
    else writing User.country from typed input should go through this too.
    """
    if not value:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    try:
        import pycountry

        # ISO-2 first, matching afc_tournament_and_scrims.views.normalize_country: `lookup` would
        # otherwise resolve some two-letter strings by fuzzy name match rather than as a code.
        found = pycountry.countries.get(alpha_2=text.upper()) if len(text) == 2 else None
        if found is None:
            found = pycountry.countries.lookup(text)
        return found.name
    except Exception:
        # LookupError (not a country) and anything else (pycountry missing) mean the same thing to
        # the caller: we cannot vouch for this value, so do not write it.
        return ""


def country_label(canonical_key, raw_values=()):
    """The name to SHOW an admin for a canonical key.

    Prefers a real spelling that exists in the data (so a country reads the way AFC's
    own users wrote it), and falls back to title-casing the canonical key.

    `raw_values` is the set of raw spellings that folded to this key. The longest one
    is chosen deliberately: between 'NG' and 'Nigeria' the longer value is the human
    name rather than the ISO code, which is what an admin wants to read on a chip.
    """
    spelled_out = [str(v).strip() for v in raw_values if v and len(str(v).strip()) > 2]
    if spelled_out:
        return max(spelled_out, key=len)
    return (canonical_key or "").title()


def group_country_counts(raw_counts):
    """Collapse {raw value: count} into a per-country list.

    Returns a list of dicts, ordered by count descending then label, each shaped:

        {"value": canonical key, "label": what to show, "count": combined count,
         "raw_values": [every spelling this covers]}

    `value` is what travels back in the filter spec, because it is stable: it does
    not change when a new spelling of the same country appears in the table.
    """
    grouped = {}
    for raw, count in raw_counts.items():
        key = canonical_country(raw)
        if not key:
            continue
        bucket = grouped.setdefault(key, {"count": 0, "raw_values": set()})
        bucket["count"] += count
        bucket["raw_values"].add(raw)

    entries = [
        {
            "value": key,
            "label": country_label(key, bucket["raw_values"]),
            "count": bucket["count"],
            "raw_values": sorted(bucket["raw_values"]),
        }
        for key, bucket in grouped.items()
    ]
    entries.sort(key=lambda entry: (-entry["count"], entry["label"]))
    return entries


def expand_country_keys(keys, raw_values):
    """Every raw spelling that the picked `keys` should match.

    `raw_values` is the set of country strings actually present in the data; the
    caller supplies it so this stays a pure function with no query of its own.

    The picked keys are canonicalized on the way in, so a spec saved before this
    module existed (holding 'Nigeria' rather than 'nigeria') still resolves. Any key
    that matches nothing in the data is passed through unchanged rather than
    discarded, so a filter can never silently widen to "no country condition".
    """
    wanted = {canonical_country(key) for key in keys if canonical_country(key)}
    if not wanted:
        return []

    matched = {raw for raw in raw_values if canonical_country(raw) in wanted}

    # Nothing in the data folds to this key: keep the original strings so the query
    # asks for something impossible (an empty audience) instead of dropping the
    # clause and mailing a wider group than the admin picked.
    if not matched:
        return sorted({str(key).strip() for key in keys if str(key).strip()})

    return sorted(matched)
