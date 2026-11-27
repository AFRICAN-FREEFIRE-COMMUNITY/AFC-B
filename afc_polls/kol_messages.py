"""
afc_polls.kol_messages - the ready-to-send message for each creator's personal code (inbox #634).

Owner, 26 Nov 2026: "I and Garena both sends the codes." So the export carries one message per creator that either
of them can paste into an email, a WhatsApp chat or a TikTok DM as it is. It names the edition, never AFC and never
a sender (standing rule: the KOL Awards are Garena's event), and it is hand-written in the creator's language,
picked from the country on Garena's KOL List: Portuguese for Mozambique, French for the French-speaking countries
on the list, English for everyone else. Same register as the KOL pages: "vous" in French, "a sua" in Portuguese.

CONSUMED BY: afc_polls.kol_nominations.admin_codes (the CSV's `message` column) and admin_code (Copy message), and
afc_polls.kol_emails (the same text as an email: code_email, and the creator's own picks: picks_email).

Owner, 27 Nov 2026: every message says the link and code are the creator's alone and that whoever they share them
with replaces their picks (#645: "if they share It to others, what the others vote for, will just replace theirs"),
and that every award needs at least one pick (#643: "tell them that they must vote in each").

Owner, 27 Nov 2026 (#650): "I need the wording of the mail sent to the KOLs for the nominee awards to be clear that this
is an official nomination for the official Garena awards is not a third party one..it's the official awards ceremony".
So every message opens by saying it is the OFFICIAL nomination round of Garena's own awards ceremony (event_name), and
the email adds where it is hosted, because it arrives from info@africanfreefirecommunity.com.
"""
import html

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
# message says both, in place of the old line that ruled self-picks out. #643 / #645 (27 Nov): every award needs a
# pick, and PRIVATE says what sharing the link does.
TEMPLATES = {
    "en": (
        "Hi {name}, this is your official invitation to nominate for the {title}, Garena's own official awards "
        "ceremony for Free Fire creators. It is not a third-party award. You are one of the creators on Garena's KOL "
        "list.\n"
        "Nominate the creators you think deserve each award: at least 1 and up to 6 in every award. You can pick "
        "anyone, yourself included, and add someone who is not on the list.\n"
        "Open your personal ballot here: {link}\n"
        "Your code: {code}\n"
        "{private} {until}"
    ),
    "fr": (
        "Bonjour {name}, ceci est votre invitation officielle à nominer pour les {title}, la cérémonie de remise de "
        "prix officielle de Garena pour les créateurs Free Fire. Ce n'est pas un prix organisé par un tiers. Vous "
        "faites partie des créateurs de la liste KOL de Garena.\n"
        "Nominez les créateurs qui méritent chaque prix : au moins 1 et jusqu'à 6 dans chaque prix. Vous pouvez "
        "choisir n'importe qui, y compris vous-même, et ajouter quelqu'un qui n'est pas sur la liste.\n"
        "Ouvrez votre bulletin personnel ici : {link}\n"
        "Votre code : {code}\n"
        "{private} {until}"
    ),
    "pt": (
        "Olá {name}, este é o seu convite oficial para nomear nos {title}, a cerimónia oficial de prémios da própria "
        "Garena para os criadores de Free Fire. Não é um prémio de terceiros. É um dos criadores da lista KOL da "
        "Garena.\n"
        "Nomeie os criadores que merecem cada prémio: pelo menos 1 e até 6 em cada prémio. Pode escolher qualquer "
        "pessoa, incluindo a si próprio, e acrescentar alguém que não esteja na lista.\n"
        "Abra o seu boletim pessoal aqui: {link}\n"
        "O seu código: {code}\n"
        "{private} {until}"
    ),
}

# #645 (owner, 27 Nov 2026): the link works only for them, and whoever they share it with replaces their picks.
PRIVATE = {
    "en": ("This link and code are yours alone: if you share them, whoever opens them can change your nominations, "
           "and what they pick replaces yours."),
    "fr": ("Ce lien et ce code ne sont qu'à vous : si vous les partagez, la personne qui les ouvre peut modifier vos "
           "nominations, et ce qu'elle choisit remplace vos choix."),
    "pt": ("Este link e este código são só seus: se os partilhar, quem os abrir pode alterar as suas nomeações, e o que "
           "essa pessoa escolher substitui as suas escolhas."),
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
    return TEMPLATES[language].format(name=creator.name, title=event_name(edition_title), link=link, code=code,
                                      until=until, private=PRIVATE[language])


def event_name(edition_title):
    """"Garena KOL Awards 2026" for the edition "KOL Awards 2026" (#650: the message says whose ceremony it is); a
    title that already names Garena is left as it is."""
    title = (edition_title or "").strip()
    return title if "garena" in title.lower() else f"Garena {title}"


# A SPARE code's message (#639, owner 26 Nov 2026): it goes to a creator found after the list was made, so it has no
# name, and it tells them that the first time they open it they say who they are. The admin tab picks the language.
SPARE_TEMPLATES = {
    "en": (
        "Hi, this is your official invitation to nominate creators for the {title}, Garena's own official awards "
        "ceremony for Free Fire creators. It is not a third-party award.\n"
        "Nominate at least 1 and up to 6 creators in every award, yourself included, and you can add someone who is "
        "not on the list.\n"
        "Open your ballot here: {link}\n"
        "Your code: {code}. The first time you open it, you say who you are (your name and TikTok @handle); from then "
        "on it is yours.\n"
        "{private} {until}"
    ),
    "fr": (
        "Bonjour, ceci est votre invitation officielle à nominer des créateurs pour les {title}, la cérémonie de "
        "remise de prix officielle de Garena pour les créateurs Free Fire. Ce n'est pas un prix organisé par un tiers.\n"
        "Nominez au moins 1 et jusqu'à 6 créateurs dans chaque prix, y compris vous-même, et vous pouvez ajouter "
        "quelqu'un qui n'est pas sur la liste.\n"
        "Ouvrez votre bulletin ici : {link}\n"
        "Votre code : {code}. La première fois que vous l'ouvrez, vous dites qui vous êtes (votre nom et votre "
        "@pseudo TikTok) ; ensuite il est à vous.\n"
        "{private} {until}"
    ),
    "pt": (
        "Olá, este é o seu convite oficial para nomear criadores nos {title}, a cerimónia oficial de prémios da "
        "própria Garena para os criadores de Free Fire. Não é um prémio de terceiros.\n"
        "Nomeie pelo menos 1 e até 6 criadores em cada prémio, incluindo a si próprio, e pode acrescentar alguém que "
        "não esteja na lista.\n"
        "Abra o seu boletim aqui: {link}\n"
        "O seu código: {code}. Da primeira vez que o abrir, diz quem é (o seu nome e o seu @utilizador do TikTok); a "
        "partir daí é seu.\n"
        "{private} {until}"
    ),
}


def spare_message_for(code, link, closes_at, edition_title, language="en"):
    """The message for a spare `code` in `language` ("en", "fr" or "pt")."""
    language = language if language in SPARE_TEMPLATES else "en"
    with_date, without_date = UNTIL[language]
    until = with_date.format(date=_date(closes_at, language)) if closes_at else without_date
    return SPARE_TEMPLATES[language].format(title=event_name(edition_title), link=link, code=code, until=until,
                                            private=PRIVATE[language])


# ── EMAILS (owner, 27 Nov 2026) ──────────────────────────────────────────────────────────────────────────────────
# #646: the code message as an email ("send an email to the KOLs ... individually or ... selected ... or all"), from
# info@ as "African Freefire Community" (owner's answer). #648: the creator's own picks ("request to send their own
# votes to their mails"). Hand-written per language and sent with prelocalized=True, so send_email never machine-
# translates them. Every value that comes from a person (names, handles) is HTML-escaped: a spare code's claimer types
# their own name. Built here, sent by afc_polls.kol_emails.

CODE_SUBJECT = {
    "en": "Official invitation: nominate for the {title}",
    "fr": "Invitation officielle : nominez pour les {title}",
    "pt": "Convite oficial: nomeie nos {title}",
}
# #650: the email arrives from info@africanfreefirecommunity.com, so it says where the official nominations are hosted.
CODE_HOSTED = {
    "en": "The official nominations for Garena's KOL Awards are hosted on the African Freefire Community website "
          "(africanfreefirecommunity.com), which sends this email.",
    "fr": "Les nominations officielles des KOL Awards de Garena sont hébergées sur le site d'African Freefire Community "
          "(africanfreefirecommunity.com), qui vous envoie cet e-mail.",
    "pt": "As nomeações oficiais dos KOL Awards da Garena estão alojadas no site da African Freefire Community "
          "(africanfreefirecommunity.com), que lhe envia este email.",
}
CODE_BUTTON = {"en": "Open my ballot", "fr": "Ouvrir mon bulletin", "pt": "Abrir o meu boletim"}

PICKS_SUBJECT = {
    "en": "Your nominations for the {title}",
    "fr": "Vos nominations pour les {title}",
    "pt": "As suas nomeações para os {title}",
}
PICKS_TEXT = {
    "en": {
        "hi": "Hi {name}, here are your nominations as they are saved now.",
        "none": "No pick yet",
        "counts": "Every award has a pick, so your nominations count.",
        "missing": ("{n} of the {total} awards still have no pick. Your nominations count only once every award has "
                    "at least one."),
        "change": "Open your ballot with your personal link or code to change them.",
    },
    "fr": {
        "hi": "Bonjour {name}, voici vos nominations telles qu'elles sont enregistrées.",
        "none": "Aucun choix pour l'instant",
        "counts": "Chaque prix a au moins un choix : vos nominations comptent.",
        "missing": ("{n} des {total} prix n'ont encore aucun choix. Vos nominations ne comptent qu'une fois que chaque "
                    "prix en a au moins un."),
        "change": "Ouvrez votre bulletin avec votre lien ou votre code personnel pour les modifier.",
    },
    "pt": {
        "hi": "Olá {name}, aqui estão as suas nomeações tal como estão guardadas.",
        "none": "Ainda sem escolha",
        "counts": "Todos os prémios têm uma escolha, por isso as suas nomeações contam.",
        "missing": ("{n} dos {total} prémios ainda não têm escolha. As suas nomeações só contam quando todos os prémios "
                    "tiverem pelo menos uma."),
        "change": "Abra o seu boletim com o seu link ou código pessoal para as alterar.",
    },
}


def _text_html(text):
    """Plain text as safe HTML, line breaks kept."""
    return html.escape(text).replace("\n", "<br>")


def _shell(heading, rows_html):
    from afc_auth.views import _email_shell

    return _email_shell(f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{html.escape(heading)}</div>
  </td></tr>{rows_html}""", "green")


def code_email(creator, code, link, closes_at, edition_title):
    """(subject, html, language) of the email carrying `creator`'s personal code: the same words as message_for,
    with the link as a button too."""
    language = language_for(creator.country)
    subject = CODE_SUBJECT[language].format(title=event_name(edition_title))
    body = message_for(creator, code, link, closes_at, edition_title)
    rows = f"""
  <tr><td style="padding:6px 44px 18px;">
    <div style="font-size:15px;line-height:1.7;color:#cdd6cf;">{_text_html(body)}</div>
  </td></tr>
  <tr><td style="padding:6px 44px 34px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#34d27b;border-radius:10px;">
      <a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:14px 40px;font-size:15px;font-weight:700;color:#062012;text-decoration:none;">{CODE_BUTTON[language]}</a>
    </td></tr></table>
  </td></tr>
  <tr><td style="padding:0 44px 34px;">
    <div style="font-size:13px;line-height:1.6;color:#8a978f;">{html.escape(CODE_HOSTED[language])}</div>
  </td></tr>"""
    return subject, _shell(subject, rows), language


def picks_email(creator, edition_title, awards):
    """(subject, html, language) of the email with `creator`'s own picks. `awards` = [(award name, [(name, handle)])]
    in ballot order. No code and no link with a code in it: the email may be forwarded."""
    language = language_for(creator.country)
    words = PICKS_TEXT[language]
    subject = PICKS_SUBJECT[language].format(title=event_name(edition_title))
    missing = sum(1 for _name, picks in awards if not picks)
    blocks = []
    for name, picks in awards:
        lines = "<br>".join(f"{html.escape(person)} <span style=\"color:#8a978f;\">@{html.escape(handle)}</span>"
                            for person, handle in picks) or f'<span style="color:#f5c518;">{words["none"]}</span>'
        blocks.append(f"""
  <tr><td style="padding:10px 44px 4px;">
    <div style="font-size:14px;font-weight:700;color:#ffffff;">{html.escape(name)}</div>
    <div style="font-size:14px;line-height:1.7;color:#cdd6cf;">{lines}</div>
  </td></tr>""")
    verdict = words["counts"] if not missing else words["missing"].format(n=missing, total=len(awards))
    rows = f"""
  <tr><td style="padding:6px 44px 8px;">
    <div style="font-size:15px;line-height:1.7;color:#cdd6cf;">{html.escape(words["hi"].format(name=creator.name))}</div>
  </td></tr>{"".join(blocks)}
  <tr><td style="padding:18px 44px 34px;">
    <div style="font-size:15px;line-height:1.7;color:{"#34d27b" if not missing else "#f5c518"};font-weight:600;">{html.escape(verdict)}</div>
    <div style="font-size:14px;line-height:1.7;color:#aab5ae;margin-top:8px;">{html.escape(words["change"])}</div>
  </td></tr>"""
    return subject, _shell(subject, rows), language
