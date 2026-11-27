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
        "ceremony for Free Fire creators. It is not a third-party award.\n"
        "Nominate the creators you think deserve each award: at least 1 and up to 6 in every award. You can pick "
        "anyone, yourself included, and add someone who is not on the list. If you nominate only yourself in an award, that nomi"
        "nation is discarded, so pick at least one other creator in every award.\n"
        "Open your personal ballot here: {link}\n"
        "Your code: {code}\n"
        "{private} {until}"
    ),
    "fr": (
        "Bonjour {name}, ceci est votre invitation officielle à nominer pour les {title}, la cérémonie de remise de "
        "prix officielle de Garena pour les créateurs Free Fire. Ce n'est pas un prix organisé par un tiers.\n"
        "Nominez les créateurs qui méritent chaque prix : au moins 1 et jusqu'à 6 dans chaque prix. Vous pouvez "
        "choisir n'importe qui, y compris vous-même, et ajouter quelqu'un qui n'est pas sur la liste. "
        "Si vous ne nominez que vous-même dans un prix, cette nomination est écartée : choisissez au moins un autre créateur dans chaque prix.\n"
        "Ouvrez votre bulletin personnel ici : {link}\n"
        "Votre code : {code}\n"
        "{private} {until}"
    ),
    "pt": (
        "Olá {name}, este é o seu convite oficial para nomear nos {title}, a cerimónia oficial de prémios da própria "
        "Garena para os criadores de Free Fire. Não é um prémio de terceiros.\n"
        "Nomeie os criadores que merecem cada prémio: pelo menos 1 e até 6 em cada prémio. Pode escolher qualquer "
        "pessoa, incluindo a si próprio, e acrescentar alguém que não esteja na lista. "
        "Se nomear apenas a si próprio num prémio, essa nomeação é descartada, por isso escolha pelo menos outro criador em cada prémio.\n"
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
        "not on the list. If you nominate only yourself in an award, that nomination is discarded, so pick at least one other creator in every award.\n"
        "Open your ballot here: {link}\n"
        "Your code: {code}. The first time you open it, you say who you are (your name and TikTok @handle); from then "
        "on it is yours.\n"
        "{private} {until}"
    ),
    "fr": (
        "Bonjour, ceci est votre invitation officielle à nominer des créateurs pour les {title}, la cérémonie de "
        "remise de prix officielle de Garena pour les créateurs Free Fire. Ce n'est pas un prix organisé par un tiers.\n"
        "Nominez au moins 1 et jusqu'à 6 créateurs dans chaque prix, y compris vous-même, et vous pouvez ajouter "
        "quelqu'un qui n'est pas sur la liste. Si vous ne nominez que vous-même dans un prix, cette nomination est écartée : choisissez au moins un autre créateur dans chaque prix.\n"
        "Ouvrez votre bulletin ici : {link}\n"
        "Votre code : {code}. La première fois que vous l'ouvrez, vous dites qui vous êtes (votre nom et votre "
        "@pseudo TikTok) ; ensuite il est à vous.\n"
        "{private} {until}"
    ),
    "pt": (
        "Olá, este é o seu convite oficial para nomear criadores nos {title}, a cerimónia oficial de prémios da "
        "própria Garena para os criadores de Free Fire. Não é um prémio de terceiros.\n"
        "Nomeie pelo menos 1 e até 6 criadores em cada prémio, incluindo a si próprio, e pode acrescentar alguém que "
        "não esteja na lista. Se nomear apenas a si próprio num prémio, essa nomeação é descartada, por isso escolha pelo menos outro criador em cada prémio.\n"
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
#
# #660 (owner, 27 Nov 2026, on the first batch as received: "why is the email ugly like this?"): the first version
# pasted the copied chat message into the email as one block of text, so the long personal link printed raw and
# wrapped, the code sat inline, the private warning was buried and the button came last. An email is laid out as an
# email: a greeting, two short paragraphs, the code in a block of its own, the button straight after it, the rules as
# a short numbered list, the private note on its own surface, and the hosted-on line as a small footer. The link lives
# only inside the button (and the fallback words), never printed. The copied WhatsApp text (message_for above) is
# unchanged. Built on the shared AFC shell (afc_auth.views._email_shell): dark surface, no borders, no glow.

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

# #660: the words of the code email, in the order they appear. `rules` is the numbered list; the last rule is the
# UNTIL sentence (with the closing date when the round has one). `fallback` names the nominations page as a link
# (the page WITHOUT the code), for a mail app that will not open the button.
CODE_TEXT = {
    "en": {
        "preheader": "Your personal code for the {title} is inside.",
        "eyebrow": "Official invitation",
        "heading": "Nominate for the {title}",
        "hi": "Hi {name},",
        "intro": ("This is your official invitation to nominate creators for the {title}, Garena's own official "
                  "awards ceremony for Free Fire creators. It is not a third-party award."),
        "code_label": "Your personal code",
        "fallback": ("The button opens your ballot with your code already in. If it does not open, go to the "
                     "{page} and type your code."),
        "page": "nominations page",
        "rules_title": "How it works",
        "rules": [
            "Pick at least 1 and up to 6 creators in every award.",
            "You can pick anyone, yourself included, and add someone who is not on the list.",
            ("If you nominate only yourself in an award, that nomination is discarded, so pick at least one other "
             "creator in every award."),
        ],
        "private_title": "Keep it to yourself",
    },
    "fr": {
        "preheader": "Votre code personnel pour les {title} se trouve dans cet e-mail.",
        "eyebrow": "Invitation officielle",
        "heading": "Nominez pour les {title}",
        "hi": "Bonjour {name},",
        "intro": ("Ceci est votre invitation officielle à nominer des créateurs pour les {title}, la cérémonie de "
                  "remise de prix officielle de Garena pour les créateurs Free Fire. Ce n'est pas un prix organisé "
                  "par un tiers."),
        "code_label": "Votre code personnel",
        "fallback": ("Le bouton ouvre votre bulletin avec votre code déjà saisi. S'il ne s'ouvre pas, allez sur la "
                     "{page} et saisissez votre code."),
        "page": "page des nominations",
        "rules_title": "Comment ça marche",
        "rules": [
            "Choisissez au moins 1 et jusqu'à 6 créateurs dans chaque prix.",
            ("Vous pouvez choisir n'importe qui, y compris vous-même, et ajouter quelqu'un qui n'est pas sur la "
             "liste."),
            ("Si vous ne nominez que vous-même dans un prix, cette nomination est écartée : choisissez au moins un "
             "autre créateur dans chaque prix."),
        ],
        "private_title": "Gardez-le pour vous",
    },
    "pt": {
        "preheader": "O seu código pessoal para os {title} está neste email.",
        "eyebrow": "Convite oficial",
        "heading": "Nomeie nos {title}",
        "hi": "Olá {name},",
        "intro": ("Este é o seu convite oficial para nomear criadores nos {title}, a cerimónia oficial de prémios da "
                  "própria Garena para os criadores de Free Fire. Não é um prémio de terceiros."),
        "code_label": "O seu código pessoal",
        "fallback": ("O botão abre o seu boletim com o código já preenchido. Se não abrir, vá à {page} e escreva o "
                     "seu código."),
        "page": "página das nomeações",
        "rules_title": "Como funciona",
        "rules": [
            "Escolha pelo menos 1 e até 6 criadores em cada prémio.",
            ("Pode escolher qualquer pessoa, incluindo a si próprio, e acrescentar alguém que não esteja na "
             "lista."),
            ("Se nomear apenas a si próprio num prémio, essa nomeação é descartada, por isso escolha pelo menos "
             "outro criador em cada prémio."),
        ],
        "private_title": "Guarde-o só para si",
    },
}

PICKS_SUBJECT = {
    "en": "Your nominations for the {title}",
    "fr": "Vos nominations pour les {title}",
    "pt": "As suas nomeações para os {title}",
}
# `counts` / `missing` are the verdict at the top; `button` opens the nominations page WITHOUT a code (the picks email
# may be forwarded, so it never carries the code), where the creator's browser or their code lets them back in.
PICKS_TEXT = {
    "en": {
        "preheader": "Your nominations for the {title}, as they are saved now.",
        "eyebrow": "Your nominations",
        "hi": "Hi {name},",
        "intro": "Here are your nominations as they are saved now.",
        "none": "No pick yet",
        "only_you": "Only you: this award counts as empty until you add someone else.",
        "counts": "Every award has a pick, so your nominations count.",
        "missing": ("{n} of the {total} awards still have no pick. Your nominations count only once every award has "
                    "at least one."),
        "change": "Open your ballot with your personal link or code to change them.",
        "button": "Open the nominations page",
    },
    "fr": {
        "preheader": "Vos nominations pour les {title}, telles qu'elles sont enregistrées.",
        "eyebrow": "Vos nominations",
        "hi": "Bonjour {name},",
        "intro": "Voici vos nominations telles qu'elles sont enregistrées.",
        "none": "Aucun choix pour l'instant",
        "only_you": "Vous seul : ce prix compte comme vide tant que vous n'ajoutez personne d'autre.",
        "counts": "Chaque prix a au moins un choix : vos nominations comptent.",
        "missing": ("{n} des {total} prix n'ont encore aucun choix. Vos nominations ne comptent qu'une fois que chaque "
                    "prix en a au moins un."),
        "change": "Ouvrez votre bulletin avec votre lien ou votre code personnel pour les modifier.",
        "button": "Ouvrir la page des nominations",
    },
    "pt": {
        "preheader": "As suas nomeações para os {title}, tal como estão guardadas.",
        "eyebrow": "As suas nomeações",
        "hi": "Olá {name},",
        "intro": "Aqui estão as suas nomeações tal como estão guardadas.",
        "none": "Ainda sem escolha",
        "only_you": "Só o próprio: este prémio conta como vazio até acrescentar outra pessoa.",
        "counts": "Todos os prémios têm uma escolha, por isso as suas nomeações contam.",
        "missing": ("{n} dos {total} prémios ainda não têm escolha. As suas nomeações só contam quando todos os prémios "
                    "tiverem pelo menos uma."),
        "change": "Abra o seu boletim com o seu link ou código pessoal para as alterar.",
        "button": "Abrir a página das nomeações",
    },
}

# ── the pieces both emails are built from ──
# Colours are the shared shell's (afc_auth.views._GRADIENTS green): text #cdd6cf, muted #8a978f, accent #34d27b,
# gold #f5c518 for "still to do". Surfaces are flat fills one step off the page (#080d0a), radius 14, never a stroke.
_TEXT = "#cdd6cf"
_MUTED = "#8a978f"
_ACCENT = "#34d27b"
_GOLD = "#f5c518"
_SURFACE = "#111a14"
_CODE_SURFACE = "#0f1c14"
_CODE_WASH = "linear-gradient(135deg,#1b3d2a 0%,#0c1b13 100%)"
_PAD = "padding-left:40px;padding-right:40px;"


def _esc(text):
    return html.escape(text, quote=True)


def _nowrap_hyphens(escaped):
    """Keep "e-mail" (and any other hyphenated word) on one line: at 390px the French footer broke it as "e-" / "mail"."""
    import re

    return re.sub(r"(\w+-\w+)", r'<span style="white-space:nowrap;">\1</span>', escaped)


def _intro_rows(eyebrow, heading, hi, intro):
    """Eyebrow + heading, then the greeting and the first paragraph."""
    return f"""
  <tr><td style="{_PAD}padding-top:30px;">
    <div style="font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:{_ACCENT};">{_esc(eyebrow)}</div>
    <div style="font-size:24px;line-height:1.3;font-weight:800;color:#ffffff;margin-top:8px;">{_esc(heading)}</div>
  </td></tr>
  <tr><td style="{_PAD}padding-top:22px;">
    <div style="font-size:16px;line-height:1.6;color:#ffffff;font-weight:600;">{_esc(hi)}</div>
    <div style="font-size:15px;line-height:1.65;color:{_TEXT};margin-top:10px;">{_esc(intro)}</div>
  </td></tr>"""


def _button(label, href):
    """The shell's green button (same as the welcome email's), centred."""
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" align="center"><tr><td bgcolor="{_ACCENT}" style="background:{_ACCENT};border-radius:10px;">
      <a href="{_esc(href)}" style="display:inline-block;padding:15px 38px;font-size:16px;font-weight:700;color:#062012;text-decoration:none;">{_esc(label)}</a>
    </td></tr></table>"""


def _hosted_row(language):
    """#650's hosted-on line, set small and centred so it reads as the footer, just above the shell's own."""
    return f"""
  <tr><td style="{_PAD}padding-top:34px;text-align:center;">
    <div style="font-size:12px;line-height:1.7;color:#6b7a71;">{_nowrap_hyphens(_esc(CODE_HOSTED[language]))}</div>
  </td></tr>"""


def _shell(rows_html, language, preheader):
    """The shared AFC email shell, in the email's own language, with a preview line for the inbox list."""
    from afc_auth.views import _email_shell

    return _email_shell(rows_html, "green", preheader=preheader, lang=language)


def code_email(creator, code, link, closes_at, edition_title):
    """(subject, html, language) of the email carrying `creator`'s personal code (#660 layout). Same facts as
    message_for: official Garena ceremony, 1 to 6 per award, self-only discarded, PRIVATE, until when."""
    language = language_for(creator.country)
    words = CODE_TEXT[language]
    title = event_name(edition_title)
    subject = CODE_SUBJECT[language].format(title=title)
    with_date, without_date = UNTIL[language]
    rules = words["rules"] + [with_date.format(date=_date(closes_at, language)) if closes_at else without_date]
    page = link.split("#", 1)[0]    # the nominations page without the code (the code rides after "#", design 5.4)
    page_link = (f'<a href="{_esc(page)}" style="color:{_ACCENT};text-decoration:underline;">'
                 f'{_esc(words["page"])}</a>')
    fallback = _esc(words["fallback"]).replace(_esc("{page}"), page_link)
    rule_rows = "".join(f"""
      <tr>
        <td width="26" valign="top" style="width:26px;padding:7px 0;font-size:14px;line-height:1.6;font-weight:800;color:{_ACCENT};">{n}</td>
        <td valign="top" style="padding:7px 0;font-size:14px;line-height:1.6;color:{_TEXT};">{_esc(rule)}</td>
      </tr>""" for n, rule in enumerate(rules, 1))
    rows = _intro_rows(words["eyebrow"], words["heading"].format(title=title),
                       words["hi"].format(name=creator.name), words["intro"].format(title=title)) + f"""
  <tr><td style="{_PAD}padding-top:26px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td bgcolor="{_CODE_SURFACE}" align="center" style="background-color:{_CODE_SURFACE};background-image:{_CODE_WASH};border-radius:14px;padding:22px 16px;text-align:center;">
        <div style="font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:{_MUTED};">{_esc(words["code_label"])}</div>
        <div style="font-size:26px;line-height:1.3;font-weight:800;letter-spacing:3px;color:{_ACCENT};font-family:Consolas,Menlo,monospace;margin-top:10px;white-space:nowrap;">{_esc(code)}</div>
      </td>
    </tr></table>
  </td></tr>
  <tr><td style="{_PAD}padding-top:18px;" align="center">{_button(CODE_BUTTON[language], link)}
  </td></tr>
  <tr><td style="{_PAD}padding-top:14px;text-align:center;">
    <div style="font-size:13px;line-height:1.6;color:{_MUTED};">{fallback}</div>
  </td></tr>
  <tr><td style="{_PAD}padding-top:30px;">
    <div style="font-size:16px;font-weight:700;color:#ffffff;">{_esc(words["rules_title"])}</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{rule_rows}
    </table>
  </td></tr>
  <tr><td style="{_PAD}padding-top:22px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td bgcolor="{_SURFACE}" style="background-color:{_SURFACE};border-radius:14px;padding:18px 20px;">
        <div style="font-size:14px;font-weight:700;color:{_GOLD};">{_esc(words["private_title"])}</div>
        <div style="font-size:14px;line-height:1.6;color:{_TEXT};margin-top:6px;">{_esc(PRIVATE[language])}</div>
      </td>
    </tr></table>
  </td></tr>{_hosted_row(language)}"""
    return subject, _shell(rows, language, words["preheader"].format(title=title)), language


def picks_email(creator, edition_title, awards, closes_at=None, page_link=None):
    """(subject, html, language) of the email with `creator`'s own picks (#660 layout). `awards` = [(award name,
    [(name, handle)], counts)] in ballot order, `counts` False for an award with no pick or only the creator (#652).
    `page_link` is the nominations page WITHOUT a code (a button when given); no code and no link with a code in it
    anywhere: the email may be forwarded."""
    language = language_for(creator.country)
    words = PICKS_TEXT[language]
    title = event_name(edition_title)
    subject = PICKS_SUBJECT[language].format(title=title)
    missing = sum(1 for _name, _picks, counts in awards if not counts)
    verdict = words["counts"] if not missing else words["missing"].format(n=missing, total=len(awards))
    blocks = []
    for name, picks, counts in awards:
        if picks:
            lines = "".join(f"""
        <tr>
          <td width="24" valign="top" style="width:24px;padding:4px 0;font-size:14px;line-height:1.6;font-weight:700;color:{_MUTED};">{n}</td>
          <td valign="top" style="padding:4px 0;font-size:14px;line-height:1.6;color:#ffffff;">{_esc(person)} <span style="color:{_MUTED};">@{_esc(handle)}</span></td>
        </tr>""" for n, (person, handle) in enumerate(picks, 1))
            lines = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{lines}</table>'
        else:
            lines = f'<div style="font-size:14px;line-height:1.6;color:{_GOLD};margin-top:6px;">{_esc(words["none"])}</div>'
        if picks and not counts:
            lines += f'<div style="font-size:13px;line-height:1.6;color:{_GOLD};margin-top:6px;">{_esc(words["only_you"])}</div>'
        blocks.append(f"""
  <tr><td style="{_PAD}padding-top:12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td bgcolor="{_SURFACE}" style="background-color:{_SURFACE};border-radius:14px;padding:16px 20px;">
        <div style="font-size:15px;font-weight:700;color:#ffffff;">{_esc(name)}</div>{lines}
      </td>
    </tr></table>
  </td></tr>""")
    with_date, without_date = UNTIL[language]
    until = with_date.format(date=_date(closes_at, language)) if closes_at else without_date
    button = (f"""
  <tr><td style="{_PAD}padding-top:18px;" align="center">{_button(words["button"], page_link)}
  </td></tr>""" if page_link else "")
    rows = _intro_rows(words["eyebrow"], title, words["hi"].format(name=creator.name), words["intro"]) + f"""
  <tr><td style="{_PAD}padding-top:22px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td bgcolor="{_CODE_SURFACE}" style="background-color:{_CODE_SURFACE};border-radius:14px;padding:16px 20px;">
        <div style="font-size:15px;line-height:1.6;font-weight:700;color:{_ACCENT if not missing else _GOLD};">{_esc(verdict)}</div>
      </td>
    </tr></table>
  </td></tr>{"".join(blocks)}
  <tr><td style="{_PAD}padding-top:26px;">
    <div style="font-size:14px;line-height:1.65;color:{_TEXT};">{_esc(words["change"])} {_esc(until)}</div>
  </td></tr>{button}{_hosted_row(language)}"""
    return subject, _shell(rows, language, words["preheader"].format(title=title)), language
