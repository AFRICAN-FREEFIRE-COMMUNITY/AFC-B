"""
afc_auth/email_typos.py - "Did you mean ...@gmail.com?" for an address typed at sign-up (inbox #273).

WHY (owner 2026-10-16, answer "A" to #238: accept any real address, catch typos). AFC used to keep a
list of 16 allowed providers. Sign-up stopped checking it on 2026-04-01, but verify-code, resend-code,
the password reset steps and the email change kept refusing everything else, so a person at a real
school or company address could sign up and then never verify. That list is gone (is_valid_email
checks the format only). What it was half doing, stopping a mistyped address, is done here instead.

WHAT PRODUCTION SHOWS (read-only, 16 Oct 2026): 9,356 accounts on gmail.com, then gamil.com 10,
gmail.con 9, gmail.co 8, gmail.come 2, gmai.com 2, gemail.com 2 and two dozen one-offs (gmial.com,
g.mail.com, gmail.vom, ivloud.com ...). A code sent to those never arrived.

HOW: a domain that is not a known real one, but sits within two keystrokes (insert, delete, replace,
or swap two neighbours) of a common provider, gets that provider as a suggestion. Real providers sit
one keystroke from gmail.com themselves (mail.com, email.com), so they are named in KNOWN_REAL and
never flagged, and the suggestion is only ever a QUESTION: signup (afc_auth.views.signup) answers
code email_domain_typo once, and the same address goes through when the person says it is right
(email_domain_confirmed). The frontend (components/auth/CreateAccountForm.tsx) asks the question.
"""

# The providers a typo is most likely aimed at, in the order a tie is broken (gmail.com first: 98% of
# AFC accounts). Only these are ever suggested.
SUGGEST = (
    "gmail.com",
    "icloud.com",
    "yahoo.com",
    "yahoo.fr",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "outlook.fr",
    "live.com",
    "live.fr",
)

# Real providers that happen to sit close to a suggestion, plus the common ones of the old list. A
# domain here is never questioned. Anything else is questioned only when it is close to SUGGEST.
KNOWN_REAL = frozenset(SUGGEST) | frozenset({
    "mail.com", "email.com", "gmx.com", "gmx.fr", "gmx.de", "ymail.com", "rocketmail.com",
    "aol.com", "aol.fr", "msn.com", "me.com", "mac.com", "protonmail.com", "proton.me", "pm.me",
    "zoho.com", "yandex.com", "inbox.com", "laposte.net", "orange.fr", "free.fr", "sfr.fr",
    "yahoo.co.uk", "hotmail.co.uk", "live.co.uk", "googlemail.com", "africanfreefirecommunity.com",
})

MAX_DISTANCE = 2


def _distance(a, b):
    """Optimal string alignment distance: insert, delete, replace, or swap two neighbours."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def suggest_email_domain(email):
    """The corrected full address when the domain looks like a typo of a common provider, else None.

    "kofi@gamil.com" -> "kofi@gmail.com"; "kofi@leadsageafrica.com" -> None; "kofi@mail.com" -> None.
    """
    text = str(email or "").strip()
    if text.count("@") != 1:
        return None
    local, domain = text.split("@")
    domain = domain.lower()
    if not local or not domain or domain in KNOWN_REAL:
        return None
    best, best_distance = None, MAX_DISTANCE + 1
    for candidate in SUGGEST:
        d = _distance(domain, candidate)
        if d < best_distance:
            best, best_distance = candidate, d
    return f"{local}@{best}" if best else None
