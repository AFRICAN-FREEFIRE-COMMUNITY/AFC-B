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

# #637 (owner, 26 Nov 2026): self-nomination is allowed and a creator can add anybody who is not on the list, so the
# message says both, in place of the old line that ruled self-picks out.
TEMPLATES = {
    "en": (
        "Hi {name}, you are one of the creators in the {title}. Nominate the creators you think deserve each award: "
        "up to 6 per award. You can pick anyone, yourself included, and add someone who is not on the list.\n"
        "Open your personal ballot here: {link}\n"
        "Your code: {code} (it works for you only, so keep it to yourself). {until}"
    ),
    "fr": (
        "Bonjour {name}, vous faites partie des créateurs des {title}. Nominez les créateurs qui "
        "méritent chaque prix : jusqu'à 6 par prix. Vous pouvez choisir n'importe qui, y compris vous-même, et ajouter "
        "quelqu'un qui n'est pas sur la liste.\n"
        "Ouvrez votre bulletin personnel ici : {link}\n"
        "Votre code : {code} (il ne fonctionne que pour vous, gardez-le pour vous). {until}"
    ),
    "pt": (
        "Olá {name}, é um dos criadores dos {title}. Nomeie os criadores que merecem cada prémio: "
        "até 6 por prémio. Pode escolher qualquer pessoa, incluindo a si próprio, e acrescentar alguém que não "
        "esteja na lista.\n"
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


# A SPARE code's message (#639, owner 26 Nov 2026): it goes to a creator found after the list was made, so it has no
# name, and it tells them that the first time they open it they say who they are. The admin tab picks the language.
SPARE_TEMPLATES = {
    "en": (
        "Hi, you are invited to nominate creators for the {title}: up to 6 per award, yourself included, and you can "
        "add someone who is not on the list.\n"
        "Open your ballot here: {link}\n"
        "Your code: {code}. The first time you open it, you say who you are (your name and TikTok @handle); from then "
        "on it is yours only, so keep it to yourself. {until}"
    ),
    "fr": (
        "Bonjour, vous êtes invité à nominer des créateurs pour les {title} : jusqu'à 6 par prix, y compris "
        "vous-même, et vous pouvez ajouter quelqu'un qui n'est pas sur la liste.\n"
        "Ouvrez votre bulletin ici : {link}\n"
        "Votre code : {code}. La première fois que vous l'ouvrez, vous dites qui vous êtes (votre nom et votre "
        "@pseudo TikTok) ; ensuite il n'est qu'à vous, gardez-le pour vous. {until}"
    ),
    "pt": (
        "Olá, está convidado a nomear criadores para os {title}: até 6 por prémio, incluindo a si próprio, e pode "
        "acrescentar alguém que não esteja na lista.\n"
        "Abra o seu boletim aqui: {link}\n"
        "O seu código: {code}. Da primeira vez que o abrir, diz quem é (o seu nome e o seu @utilizador do TikTok); a "
        "partir daí é só seu, guarde-o para si. {until}"
    ),
}


def spare_message_for(code, link, closes_at, edition_title, language="en"):
    """The message for a spare `code` in `language` ("en", "fr" or "pt")."""
    language = language if language in SPARE_TEMPLATES else "en"
    with_date, without_date = UNTIL[language]
    until = with_date.format(date=_date(closes_at, language)) if closes_at else without_date
    return SPARE_TEMPLATES[language].format(title=edition_title, link=link, code=code, until=until)
