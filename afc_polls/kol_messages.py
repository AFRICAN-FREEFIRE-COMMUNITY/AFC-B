"""
afc_polls.kol_messages - the ready-to-send message for each creator's personal code (inbox #634).

Owner, 26 Nov 2026: "I and Garena both sends the codes." So the export carries one message per creator that either
of them can paste into an email, a WhatsApp chat or a TikTok DM as it is. It names the edition, never AFC and never
a sender (standing rule: the KOL Awards are Garena's event), and it is hand-written in the creator's language,
picked from the country on Garena's KOL List: Portuguese for Mozambique, French for the French-speaking countries
on the list, English for everyone else. Same register as the KOL pages: "vous" in French, "a sua" in Portuguese.

CONSUMED BY: afc_polls.kol_nominations.admin_codes (the CSV's `message` column).
"""

PORTUGUESE_COUNTRIES = {"mozambique", "angola", "cape verde", "cabo verde", "guinea-bissau", "sao tome and principe"}
FRENCH_COUNTRIES = {
    "benin", "togo", "ivory coast", "cote d'ivoire", "côte d'ivoire", "madagascar", "senegal", "cameroon",
    "mali", "burkina faso", "niger", "guinea", "gabon", "congo", "dr congo", "chad",
}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro"],
}

TEMPLATES = {
    "en": (
        "Hi {name}, you are one of the creators in the {title}. Nominate the creators you think deserve each award: "
        "up to 6 per award, and never yourself.\n"
        "Open your personal ballot here: {link}\n"
        "Your code: {code} (it works for you only, so keep it to yourself). {until}"
    ),
    "fr": (
        "Bonjour {name}, vous faites partie des créateurs des {title}. Nominez les créateurs qui "
        "méritent chaque prix : jusqu'à 6 par prix, et jamais vous-même.\n"
        "Ouvrez votre bulletin personnel ici : {link}\n"
        "Votre code : {code} (il ne fonctionne que pour vous, gardez-le pour vous). {until}"
    ),
    "pt": (
        "Olá {name}, é um dos criadores dos {title}. Nomeie os criadores que merecem cada prémio: "
        "até 6 por prémio, e nunca a si próprio.\n"
        "Abra o seu boletim pessoal aqui: {link}\n"
        "O seu código: {code} (só funciona para si, guarde-o só para si). {until}"
    ),
}

UNTIL = {
    "en": ("You can change your picks until {date} (UTC).", "You can change your picks until nominations close."),
    "fr": ("Vous pouvez modifier vos choix jusqu'au {date} (UTC).",
           "Vous pouvez modifier vos choix jusqu'à la clôture des nominations."),
    "pt": ("Pode alterar as suas escolhas até {date} (UTC).",
           "Pode alterar as suas escolhas até as nomeações fecharem."),
}


def language_for(country):
    """"pt", "fr" or "en" for a country as Garena's sheet spells it."""
    key = (country or "").strip().lower()
    if key in PORTUGUESE_COUNTRIES:
        return "pt"
    if key in FRENCH_COUNTRIES:
        return "fr"
    return "en"


def _date(moment, language):
    """"5 December 2026, 23:59" in UTC, month in the message's language. The message is read on any phone in any
    timezone, so it states UTC rather than guess the reader's."""
    from datetime import timezone as dt_timezone

    utc = moment.astimezone(dt_timezone.utc)
    return f"{utc.day} {MONTHS[language][utc.month - 1]} {utc.year}, {utc:%H:%M}"


def message_for(creator, code, link, closes_at, edition_title):
    """The message for `creator` with their `code` and personal `link`, in their language."""
    language = language_for(creator.country)
    with_date, without_date = UNTIL[language]
    until = with_date.format(date=_date(closes_at, language)) if closes_at else without_date
    return TEMPLATES[language].format(name=creator.name, title=edition_title, link=link, code=code, until=until)
