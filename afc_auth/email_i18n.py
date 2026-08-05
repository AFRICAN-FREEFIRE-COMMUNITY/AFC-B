# afc_auth/email_i18n.py
#
# HAND-AUTHORED transactional-email copy catalog (owner 2026-07-13, rewritten 2026-08-05).
#
# WHAT THIS IS
#   The single source of truth for the FIXED transactional emails' text in the three site
#   languages: English (en, canonical), French (fr) and Portuguese (pt). Every string here is
#   written BY HAND, as complete natural sentences, NOT machine-translated. This exists so the
#   account / shop / sponsor / tournament / player-market emails read like a native wrote them,
#   and so they are DEPENDENCY-FREE from the DeepL engine in afc_auth.translation: even if the
#   translation API key is missing, over quota, or the network is down, a French or Portuguese
#   recipient still gets a clean, fully localized email.
#
# HOW IT CONNECTS (the two public helpers below)
#   - subject_for(key, lang, **fmt) -> str
#       Returns the localized SUBJECT line for a template key, with {placeholders} filled from
#       **fmt (e.g. event_name, order_no). Callers pass this as the `subject` to send_email(...).
#   - copy_for(template, lang) -> dict
#       Returns the localized BODY-copy dict for a template (a bag of whole sentences keyed by a
#       short name). The email BUILDER does copy_for(...)[key].format(username=<wrapped html>, ...)
#       to inject the dynamic values (usernames, codes, amounts) into the natural sentence, then
#       drops the result into the branded HTML shell. Dynamic values are injected AS-IS (a username
#       or a free-text reason is never translated, exactly like a proper i18n system).
#
#   Both helpers fall back to English when the language is unknown or a key is missing, so a caller
#   can never crash or send an empty line. Because the copy is already in the recipient's language,
#   every caller pairs this with send_email(..., prelocalized=True), which SKIPS the DeepL block.
#
# WHO CONSUMES THIS
#   - afc_auth/views.py           : email_verification_code / _welcome / _reset_token /
#                                   _password_changed / _change_code / _email_changed builders +
#                                   their signup / verify / resend / reset / change-email call sites.
#   - afc_shop/emails.py          : order received / shipped / completed builders + senders.
#   - afc_shop/fulfilment.py      : the vendor "new order to fulfil" heads-up (notify_vendor).
#   - afc_sponsors/engagements.py : the sponsor registration-rejection email (_notify_rejection).
#   - afc_tournament_and_scrims/views.py : team fully-registered + player accepted/rejected emails
#                                   (confirm_player / reject_player / check_and_activate_team).
#   - afc_player_market/views.py  : application received / rejected + trial started / invited /
#                                   accepted emails.
#   - afc_partner_apply/emails.py : the four transitions of a partner application.
#
# COPY RULES
#   - NO em/en dashes anywhere (AFC hard rule). Use commas, colons, parentheses, or a spaced hyphen.
#   - Every value is a str.format() template: only {placeholder} tokens are substituted, so the
#     natural sentence stays intact and the dynamic value is dropped in.
#   - A {placeholder} may ONLY appear in a sentence if the builder that renders that key actually
#     passes it. Most builders call .format() inside an f-string with no try/except, so an unknown
#     placeholder raises and the email never goes out; a placeholder the builder does not pass
#     would ship the literal "{team_name}" to a real person. tests_email_copy.py asserts the
#     placeholder sets match across en/fr/pt for every key, which is the failure that would
#     otherwise reach an inbox in one language only.
#
# HOW THIS COPY IS WRITTEN (owner backlog #18, 2026-08-05: "read natural, not generic AI slop")
#   Say what happened, say what it means for the reader, say what they do next. In that order.
#   Then stop. Concretely, the rules the rewrite applied to every sentence below:
#     - No sentence that describes the email instead of saying the thing. "We are writing to inform
#       you that your application was reviewed" is one sentence of packaging around zero facts;
#       "{team} has read your application and decided not to take it further" is the fact.
#     - No empty intensifiers, no "we are pleased/thrilled/delighted", no "after careful
#       consideration", no "we regret to inform you", no "don't let talent slip away".
#     - One sentence does one sentence's work. Three sentences that all mean "your order shipped"
#       become one.
#     - The tone matches the news. Warm on an approval, plain and respectful on a rejection,
#       specific and urgent on anything with a deadline or a security consequence. A rejection
#       email never ends on a cheery sign-off.
#     - Name something real: the event, the team, the order number, the address below, the
#       dashboard the details actually arrive in. Copy that would fit any website is the failure.
#     - Say only what is true of THIS system. Where a claim could not be verified in the code it
#       was cut rather than softened into a hedge.


def _norm(lang):
    """Normalize a raw language value to a supported 2-letter code, defaulting to English.

    Callers pass user.language (which may be "", None, "FR", "pt-BR" style values); we lower-case,
    take the leading 2 letters, and fall back to "en" for anything we do not hand-translate."""
    code = (str(lang or "en").strip().lower())[:2]
    return code if code in ("en", "fr", "pt") else "en"


def subject_for(key, lang, **fmt):
    """Localized subject line for a template `key` in `lang` (en/fr/pt), with {placeholders} from
    **fmt. Falls back to English when the language or key is unknown. Callers hand the result to
    send_email(..) as the subject and set prelocalized=True (the copy is already localized)."""
    row = SUBJECTS.get(key, {})
    text = row.get(_norm(lang)) or row.get("en") or ""
    try:
        return text.format(**fmt) if fmt else text
    except Exception:
        # A stray brace or a missing key must never break a send; return the raw sentence.
        return text


def copy_for(template, lang):
    """Localized body-copy dict for a `template` in `lang` (en/fr/pt). Falls back to English for an
    unknown language or missing template. The builder pulls individual sentences out of it and
    .format()s in the HTML-wrapped dynamic values."""
    row = COPY.get(template, {})
    return row.get(_norm(lang)) or row.get("en") or {}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SUBJECTS: one entry per subject line. Some templates reuse a builder under two subjects (e.g. the
# verification-code email is sent both at signup and on resend), which is exactly why subjects are
# catalogued by their own key rather than tied 1:1 to a builder.
#
# A subject is read in a list of forty other subjects, so it says the thing and stops. No "AFC
# Registration Update:" prefix in front of the actual news, no Title Case On Every Word, and no
# placeholder the call site does not pass (see the COPY RULES above).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
SUBJECTS = {
    # ── afc_auth ──
    "verify_account": {
        "en": "Verify your AFC account",
        "fr": "Vérifiez votre compte AFC",
        "pt": "Verifique a sua conta AFC",
    },
    "resend_code": {
        "en": "Your new AFC verification code",
        "fr": "Votre nouveau code de vérification AFC",
        "pt": "O seu novo código de verificação AFC",
    },
    "welcome": {
        "en": "Your AFC account is ready",
        "fr": "Votre compte AFC est prêt",
        "pt": "A sua conta AFC está pronta",
    },
    "reset_password": {
        "en": "Reset your AFC password",
        "fr": "Réinitialisez votre mot de passe AFC",
        "pt": "Redefina a sua palavra-passe AFC",
    },
    "resend_reset": {
        "en": "Your new AFC password reset token",
        "fr": "Votre nouveau jeton de réinitialisation de mot de passe AFC",
        "pt": "O seu novo código de redefinição de palavra-passe AFC",
    },
    "password_changed": {
        "en": "Your AFC password was changed",
        "fr": "Votre mot de passe AFC a été modifié",
        "pt": "A sua palavra-passe AFC foi alterada",
    },
    "confirm_new_email": {
        "en": "Confirm your new AFC email",
        "fr": "Confirmez votre nouvelle adresse e-mail AFC",
        "pt": "Confirme o seu novo e-mail AFC",
    },
    "email_changed": {
        "en": "Your AFC account email was changed",
        "fr": "L'adresse e-mail de votre compte AFC a été modifiée",
        "pt": "O e-mail da sua conta AFC foi alterado",
    },
    "email_updated_admin": {
        "en": "Your AFC account email was updated",
        "fr": "L'adresse e-mail de votre compte AFC a été mise à jour",
        "pt": "O e-mail da sua conta AFC foi atualizado",
    },

    # ── afc_shop ──
    "order_received": {
        "en": "We received your order",
        "fr": "Nous avons reçu votre commande",
        "pt": "Recebemos a sua encomenda",
    },
    "order_shipped": {
        "en": "Your order is on the way",
        "fr": "Votre commande est en route",
        "pt": "A sua encomenda está a caminho",
    },
    "order_completed": {
        "en": "Your order is complete",
        "fr": "Votre commande est terminée",
        "pt": "A sua encomenda está concluída",
    },
    "vendor_new_order": {
        "en": "New AFC order #{order_no} to fulfil",
        "fr": "Nouvelle commande AFC n° {order_no} à traiter",
        "pt": "Nova encomenda AFC n.º {order_no} para processar",
    },

    # ── afc_sponsors (reason is free text, injected untranslated) ──
    "sponsor_reject_final": {
        "en": "Registration rejected for {event_name}",
        "fr": "Inscription refusée pour {event_name}",
        "pt": "Inscrição recusada para {event_name}",
    },
    "sponsor_reject_retry": {
        "en": "Action needed: fix your {label} for {event_name}",
        "fr": "Action requise : corrigez votre {label} pour {event_name}",
        "pt": "Ação necessária: corrija o seu {label} para {event_name}",
    },

    # ── afc_tournament_and_scrims ──
    # These four used to open with "AFC Registration Update:" and then Title Case the news. The
    # prefix is the part a player skips, so the news is now the whole subject.
    "team_registered": {
        "en": "{team_name} is registered for {event_name}",
        "fr": "{team_name} est inscrite à {event_name}",
        "pt": "{team_name} está inscrita em {event_name}",
    },
    "player_accepted": {
        "en": "You're in: {event_name}",
        "fr": "Vous êtes inscrit : {event_name}",
        "pt": "Está inscrito: {event_name}",
    },
    "player_accepted_owner": {
        "en": "{player} is cleared for {event_name}",
        "fr": "{player} est validé pour {event_name}",
        "pt": "{player} está validado para {event_name}",
    },
    "player_rejected": {
        "en": "About your registration for {event_name}",
        "fr": "Au sujet de votre inscription à {event_name}",
        "pt": "Sobre a sua inscrição em {event_name}",
    },
    "player_rejected_owner": {
        "en": "{player} was not accepted for {event_name}",
        "fr": "{player} n'a pas été accepté pour {event_name}",
        "pt": "{player} não foi aceite para {event_name}",
    },

    # ── afc_player_market ──
    # NOTE: pm_application_received is sent with NO format kwargs (afc_player_market/views.py calls
    # subject_for("pm_application_received", lang) bare), so this subject must stay placeholder-free.
    "pm_application_received": {
        "en": "New applications on your Player Market post",
        "fr": "Nouvelles candidatures sur votre annonce Player Market",
        "pt": "Novas candidaturas na sua publicação do Player Market",
    },
    "pm_application_rejected": {
        "en": "About your application to {team_name}",
        "fr": "Au sujet de votre candidature à {team_name}",
        "pt": "Sobre a sua candidatura a {team_name}",
    },
    "pm_trial_started_player": {
        "en": "{team_name} has started your trial",
        "fr": "{team_name} a lancé votre essai",
        "pt": "{team_name} iniciou o seu teste",
    },
    "pm_trial_started_team": {
        "en": "{player} is on trial with your team",
        "fr": "{player} est à l'essai avec votre équipe",
        "pt": "{player} está em teste com a sua equipa",
    },
    "pm_trial_invite": {
        "en": "Trial invite from {team_name}",
        "fr": "Invitation à un essai de {team_name}",
        "pt": "Convite para teste de {team_name}",
    },
    "pm_trial_accepted_team": {
        "en": "{player} accepted your trial invite",
        "fr": "{player} a accepté votre invitation à l'essai",
        "pt": "{player} aceitou o seu convite para teste",
    },

    # ── afc_partner_apply: an organisation applying to become an AFC partner ──
    # The recipient here is an ORGANISATION, not a player, so the register is a shade more formal
    # than the rest of this catalog and never uses an in-game name. {reference} is the application
    # handle (AFC-P-XXXXXX) and appears in the subject on purpose: these four emails arrive days
    # apart and the reference is what threads them together in a shared inbox.
    "partner_apply_received": {
        "en": "We have your AFC partner application ({reference})",
        "fr": "Nous avons bien reçu votre demande de partenariat AFC ({reference})",
        "pt": "Recebemos a sua candidatura a parceiro AFC ({reference})",
    },
    "partner_apply_changes": {
        "en": "Action needed on your AFC partner application ({reference})",
        "fr": "Action requise sur votre demande de partenariat AFC ({reference})",
        "pt": "Ação necessária na sua candidatura a parceiro AFC ({reference})",
    },
    "partner_apply_approved": {
        "en": "Your AFC partner application is approved ({reference})",
        "fr": "Votre demande de partenariat AFC est approuvée ({reference})",
        "pt": "A sua candidatura a parceiro AFC foi aprovada ({reference})",
    },
    "partner_apply_rejected": {
        "en": "About your AFC partner application ({reference})",
        "fr": "Au sujet de votre demande de partenariat AFC ({reference})",
        "pt": "Sobre a sua candidatura a parceiro AFC ({reference})",
    },
}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# COPY: template -> language -> {sentence-key: str}. Each string is a str.format() template; the
# builder injects the HTML-wrapped dynamic value(s). Keys are shared in meaning across languages.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
COPY = {
    # ── afc_auth: verification code (signup + resend) ──
    # Rendered by afc_auth/views.py email_verification_code. The six-digit code sits in its own
    # green box BETWEEN "intro" and "expires", which is why the intro can just say where to type it
    # and stop; repeating the code in prose would be a second sentence doing the first one's job.
    "verification_code": {
        "en": {
            "heading": "Verify your account",
            "intro": "Hi {username}. One step left: enter this code on {site} and your AFC account is live.",
            "expires": "The code is good for 10 minutes.",
            "disclaimer": "Did not sign up for AFC? Ignore this email and nothing happens. AFC staff will never ask you for this code, so do not share it with anyone.",
        },
        "fr": {
            "heading": "Vérifiez votre compte",
            "intro": "Bonjour {username}. Il ne reste qu'une étape : saisissez ce code sur {site} et votre compte AFC est actif.",
            "expires": "Le code est valable 10 minutes.",
            "disclaimer": "Vous n'avez pas créé de compte AFC ? Ignorez cet e-mail, rien ne se passera. L'équipe AFC ne vous demandera jamais ce code, ne le partagez avec personne.",
        },
        "pt": {
            "heading": "Verifique a sua conta",
            "intro": "Olá {username}. Falta um passo: introduza este código em {site} e a sua conta AFC fica ativa.",
            "expires": "O código é válido durante 10 minutos.",
            "disclaimer": "Não criou uma conta AFC? Ignore este e-mail e nada acontece. A equipa da AFC nunca lhe vai pedir este código, não o partilhe com ninguém.",
        },
    },

    # ── afc_auth: welcome ──
    # The three feat* labels are icon captions under the button, so the intro must NOT re-list
    # "tournaments, rankings, teams" or the reader gets the same three nouns twice in one screen.
    # It gives a first move instead.
    "welcome": {
        "en": {
            "heading": "You're in, {username}",
            "intro": "Your account is verified. Most players start by joining a tournament that is taking entries, or by creating a team and inviting their squad.",
            "cta": "Open AFC",
            "feat1": "Compete in tournaments",
            "feat2": "Climb the rankings",
            "feat3": "Find your team",
        },
        "fr": {
            "heading": "Vous y êtes, {username}",
            "intro": "Votre compte est vérifié. La plupart des joueurs commencent par s'inscrire à un tournoi dont les inscriptions sont ouvertes, ou par créer une équipe et y inviter leurs coéquipiers.",
            "cta": "Ouvrir AFC",
            "feat1": "Participez à des tournois",
            "feat2": "Grimpez au classement",
            "feat3": "Trouvez votre équipe",
        },
        "pt": {
            "heading": "Está dentro, {username}",
            "intro": "A sua conta está verificada. A maioria dos jogadores começa por se inscrever num torneio com inscrições abertas, ou por criar uma equipa e convidar os seus colegas.",
            "cta": "Abrir a AFC",
            "feat1": "Compita em torneios",
            "feat2": "Suba na classificação",
            "feat3": "Encontre a sua equipa",
        },
    },

    # ── afc_auth: password reset token ──
    # "token" is deliberate in English: the frontend reset screen (messages/en/auth.json,
    # "Enter the token sent to") calls it a token, and an email that renames it would send the
    # reader looking for a field that does not exist.
    "reset_token": {
        "en": {
            "heading": "Reset your password",
            "intro": "Here is your reset token. Enter it on the reset page and you can set a new password.",
            "expires": "The token is good for 10 minutes, then you will need a fresh one.",
            "disclaimer": "Did not ask for this? Ignore this email. Your password stays exactly as it is, and nobody can change it without this token. Never share it.",
        },
        "fr": {
            "heading": "Réinitialisez votre mot de passe",
            "intro": "Voici votre jeton de réinitialisation. Saisissez-le sur la page de réinitialisation et vous pourrez choisir un nouveau mot de passe.",
            "expires": "Le jeton est valable 10 minutes, ensuite il vous en faudra un nouveau.",
            "disclaimer": "Vous n'avez rien demandé ? Ignorez cet e-mail. Votre mot de passe reste exactement le même, et personne ne peut le changer sans ce jeton. Ne le partagez jamais.",
        },
        "pt": {
            "heading": "Redefina a sua palavra-passe",
            "intro": "Aqui está o seu código de redefinição. Introduza-o na página de redefinição e poderá escolher uma nova palavra-passe.",
            "expires": "O código é válido durante 10 minutos, depois disso precisa de um novo.",
            "disclaimer": "Não pediu nada disto? Ignore este e-mail. A sua palavra-passe fica exatamente como está, e ninguém a pode alterar sem este código. Nunca o partilhe.",
        },
    },

    # ── afc_auth: password changed confirmation ──
    # This is a tripwire email: its whole value is the "warning" line, so that line names the actual
    # consequence (someone else is in the account) and gives two ordered actions, not a vague
    # "your account may be at risk".
    "password_changed": {
        "en": {
            "heading": "Your password was changed",
            "intro": "The password for {username} was changed on {when}.",
            "warning": "If that was not you, someone else is in your account. Reset your password now and tell {support} straight away.",
            "support_label": "support",
        },
        "fr": {
            "heading": "Votre mot de passe a été modifié",
            "intro": "Le mot de passe de {username} a été modifié le {when}.",
            "warning": "Si ce n'était pas vous, quelqu'un d'autre est dans votre compte. Réinitialisez votre mot de passe maintenant et prévenez {support} sans attendre.",
            "support_label": "le support",
        },
        "pt": {
            "heading": "A sua palavra-passe foi alterada",
            "intro": "A palavra-passe de {username} foi alterada em {when}.",
            "warning": "Se não foi você, outra pessoa está na sua conta. Redefina já a palavra-passe e avise {support} de imediato.",
            "support_label": "o suporte",
        },
    },

    # ── afc_auth: confirm new email (code to the new address) ──
    "change_code": {
        "en": {
            "heading": "Confirm your new email",
            "intro": "An AFC account is being moved to this email address. Enter the code below in your profile settings to confirm the address is yours.",
            "expires": "The code is good for 10 minutes.",
            "disclaimer": "If this was not you, ignore this email. Nothing has changed on any account, and nothing will without this code.",
        },
        "fr": {
            "heading": "Confirmez votre nouvelle adresse e-mail",
            "intro": "Un compte AFC est en train d'être transféré vers cette adresse e-mail. Saisissez le code ci-dessous dans les paramètres de votre profil pour confirmer que l'adresse est bien la vôtre.",
            "expires": "Le code est valable 10 minutes.",
            "disclaimer": "Si ce n'était pas vous, ignorez cet e-mail. Aucun compte n'a été modifié, et rien ne le sera sans ce code.",
        },
        "pt": {
            "heading": "Confirme o seu novo e-mail",
            "intro": "Uma conta AFC está a ser transferida para este endereço de e-mail. Introduza o código abaixo nas definições do seu perfil para confirmar que o endereço é seu.",
            "expires": "O código é válido durante 10 minutos.",
            "disclaimer": "Se não foi você, ignore este e-mail. Nenhuma conta foi alterada, e nada será alterado sem este código.",
        },
    },

    # ── afc_auth: email changed confirmation (to old + new address) ──
    # Sent to BOTH addresses, so the warning has to make sense in the OLD inbox: it names why losing
    # the address matters (password resets now go elsewhere) rather than saying "may be at risk".
    "email_changed": {
        "en": {
            "heading": "Your account email was changed",
            "intro": "The email on {username}'s AFC account is now {new_email}, changed on {when}. Sign in with that address from now on.",
            "warning": "If that was not you, contact {support} straight away. Whoever made the change can now receive your password resets.",
            "support_label": "support",
        },
        "fr": {
            "heading": "L'adresse e-mail de votre compte a été modifiée",
            "intro": "L'adresse e-mail du compte AFC de {username} est désormais {new_email}, modifiée le {when}. Connectez-vous avec cette adresse à partir de maintenant.",
            "warning": "Si ce n'était pas vous, contactez {support} sans attendre. La personne qui a fait ce changement peut désormais recevoir vos réinitialisations de mot de passe.",
            "support_label": "le support",
        },
        "pt": {
            "heading": "O e-mail da sua conta foi alterado",
            "intro": "O e-mail da conta AFC de {username} é agora {new_email}, alterado em {when}. A partir de agora, inicie sessão com esse endereço.",
            "warning": "Se não foi você, contacte {support} de imediato. Quem fez a alteração passa a receber as suas redefinições de palavra-passe.",
            "support_label": "o suporte",
        },
    },

    # ── afc_shop: order lifecycle + shared summary labels ──
    # All three order emails render the shared summary card (items, totals, delivery address)
    # between the intro and the closing line, so the prose never repeats what the card already
    # shows. "the address below" in order_shipped points at that card and is literally true.
    "order_received": {
        "en": {
            "heading": "We received your order",
            "intro": "Thanks {buyer}. Your payment went through and the seller is packing your order. We will email you again the moment it ships.",
            "track": "You can check the status any time at {link}.",
        },
        "fr": {
            "heading": "Nous avons reçu votre commande",
            "intro": "Merci {buyer}. Votre paiement a été accepté et le vendeur prépare votre colis. Nous vous écrirons dès qu'il partira.",
            "track": "Vous pouvez consulter l'état de la commande à tout moment sur {link}.",
        },
        "pt": {
            "heading": "Recebemos a sua encomenda",
            "intro": "Obrigado {buyer}. O seu pagamento foi aceite e o vendedor está a preparar a encomenda. Voltamos a escrever-lhe assim que ela seguir.",
            "track": "Pode consultar o estado a qualquer momento em {link}.",
        },
    },
    "order_shipped": {
        "en": {
            "heading": "Your order is on the way",
            "intro": "{buyer}, the seller has shipped your order. It is on its way to the address below.",
            "ship_label": "Estimated ship date:",
            "questions": "If anything is wrong with the delivery, tell us at {link}.",
        },
        "fr": {
            "heading": "Votre commande est en route",
            "intro": "{buyer}, le vendeur a expédié votre commande. Elle est en route vers l'adresse indiquée ci-dessous.",
            "ship_label": "Date d'expédition estimée :",
            "questions": "Si quelque chose ne va pas avec la livraison, dites-le-nous sur {link}.",
        },
        "pt": {
            "heading": "A sua encomenda está a caminho",
            "intro": "{buyer}, o vendedor expediu a sua encomenda. Segue para a morada indicada abaixo.",
            "ship_label": "Data de expedição estimada:",
            "questions": "Se houver algum problema com a entrega, diga-nos em {link}.",
        },
    },
    "order_completed": {
        "en": {
            "heading": "Your order is complete",
            "intro": "{buyer}, your order is marked delivered and closed. If it never reached you, tell us and we will look into it.",
            "shop_again": "Everything else in the shop is at {link}.",
        },
        "fr": {
            "heading": "Votre commande est terminée",
            "intro": "{buyer}, votre commande est marquée comme livrée et clôturée. Si elle ne vous est jamais parvenue, dites-le-nous et nous vérifierons.",
            "shop_again": "Le reste de la boutique est sur {link}.",
        },
        "pt": {
            "heading": "A sua encomenda está concluída",
            "intro": "{buyer}, a sua encomenda está marcada como entregue e fechada. Se nunca lhe chegou, diga-nos e vamos verificar.",
            "shop_again": "O resto da loja está em {link}.",
        },
    },
    # Shared order-summary labels (items table + totals + delivery), used by all three shop emails.
    "order_summary": {
        "en": {
            "order_no": "Order #{id}",
            "subtotal": "Subtotal",
            "discount": "Discount",
            "tax": "Tax",
            "total": "Total",
            "delivery_to": "Delivery to",
        },
        "fr": {
            "order_no": "Commande n° {id}",
            "subtotal": "Sous-total",
            "discount": "Remise",
            "tax": "Taxe",
            "total": "Total",
            "delivery_to": "Livraison à",
        },
        "pt": {
            "order_no": "Encomenda n.º {id}",
            "subtotal": "Subtotal",
            "discount": "Desconto",
            "tax": "Imposto",
            "total": "Total",
            "delivery_to": "Entrega para",
        },
    },
    # ── afc_shop: vendor heads-up ──
    "vendor_new_order": {
        "en": {
            "heading": "You have a new order",
            "intro": "Order #{order_no} is paid. Buyer: {buyer}. Open your fulfilment page at {link} to accept it and set a ship date.",
        },
        "fr": {
            "heading": "Vous avez une nouvelle commande",
            "intro": "La commande n° {order_no} est payée. Acheteur : {buyer}. Ouvrez votre page de traitement sur {link} pour l'accepter et fixer une date d'expédition.",
        },
        "pt": {
            "heading": "Tem uma nova encomenda",
            "intro": "A encomenda n.º {order_no} está paga. Comprador: {buyer}. Abra a sua página de processamento em {link} para a aceitar e definir uma data de expedição.",
        },
    },

    # ── afc_sponsors: registration rejection (reason is free text, injected untranslated) ──
    # "title" mirrors the SUBJECTS entry of the same key; afc_sponsors/engagements.py takes its
    # subject from subject_for() and renders only "body", so the two must not drift apart.
    # The retry variant repeats {sponsor} at the end on purpose: the player needs to know WHO is
    # still holding their registration, not just that it is "pending".
    "sponsor_reject_final": {
        "en": {
            "title": "Registration rejected for {event_name}",
            "body": "{sponsor} has rejected your registration for {event_name}. The reason given: {reason}. Your slot is now free for another player.",
        },
        "fr": {
            "title": "Inscription refusée pour {event_name}",
            "body": "{sponsor} a refusé votre inscription à {event_name}. Motif indiqué : {reason}. Votre place est désormais libre pour un autre joueur.",
        },
        "pt": {
            "title": "Inscrição recusada para {event_name}",
            "body": "{sponsor} recusou a sua inscrição em {event_name}. Motivo indicado: {reason}. A sua vaga está agora livre para outro jogador.",
        },
    },
    "sponsor_reject_retry": {
        "en": {
            "title": "Action needed: fix your {label} for {event_name}",
            "body": "{sponsor} rejected your {label} for {event_name}. The reason given: {reason}. Open the event page and enter the correct value. Your registration stays pending until {sponsor} approves it.",
        },
        "fr": {
            "title": "Action requise : corrigez votre {label} pour {event_name}",
            "body": "{sponsor} a refusé votre {label} pour {event_name}. Motif indiqué : {reason}. Ouvrez la page de l'événement et saisissez la bonne valeur. Votre inscription reste en attente tant que {sponsor} ne l'a pas approuvée.",
        },
        "pt": {
            "title": "Ação necessária: corrija o seu {label} para {event_name}",
            "body": "{sponsor} recusou o seu {label} para {event_name}. Motivo indicado: {reason}. Abra a página do evento e introduza o valor correto. A sua inscrição fica pendente até {sponsor} a aprovar.",
        },
    },

    # ── afc_tournament_and_scrims: team fully registered (to the team owner) ──
    # Order on the page: congrats (h1), dear, verified, box (highlighted), match_details, stay,
    # need_help, look_forward, regards + board, then the two footer buttons.
    # "match_details" names the ONE place room IDs actually arrive (dashboard notifications), which
    # is the single most useful fact in this email and used to be buried in a parenthetical.
    "team_registered": {
        "en": {
            "congrats": "Your team is in",
            "dear": "Hi {leader} ({team_name}),",
            "verified": "Every player on your roster has been checked and accepted. There is nothing else we need from you.",
            "box": "{team_name} is fully registered for {event_name}.",
            "match_details": "Room IDs, passwords and match times arrive as notifications in your AFC dashboard. That is where to look for them.",
            "stay": "Check the dashboard on the day you play, so nothing catches you out.",
            "need_help": "Something not right? Write to {email}",
            "look_forward": "Good luck out there.",
            "regards": "Best regards,",
            "board": "AFC Management Board",
            "visit_website": "Visit Website",
            "join_discord": "Join Discord",
        },
        "fr": {
            "congrats": "Votre équipe est inscrite",
            "dear": "Bonjour {leader} ({team_name}),",
            "verified": "Chaque joueur de votre effectif a été vérifié et accepté. Nous n'avons plus besoin de rien de votre côté.",
            "box": "{team_name} est entièrement inscrite à {event_name}.",
            "match_details": "Les identifiants de salle, les mots de passe et les horaires des matchs arrivent sous forme de notifications dans votre tableau de bord AFC. C'est là qu'il faut les chercher.",
            "stay": "Passez sur le tableau de bord le jour où vous jouez, pour ne pas être pris de court.",
            "need_help": "Un souci ? Écrivez à {email}",
            "look_forward": "Bonne chance.",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
            "visit_website": "Visiter le site",
            "join_discord": "Rejoindre Discord",
        },
        "pt": {
            "congrats": "A sua equipa está inscrita",
            "dear": "Olá {leader} ({team_name}),",
            "verified": "Todos os jogadores do seu plantel foram verificados e aceites. Não precisamos de mais nada da sua parte.",
            "box": "{team_name} está totalmente inscrita em {event_name}.",
            "match_details": "Os IDs de sala, as palavras-passe e os horários das partidas chegam como notificações no seu painel AFC. É aí que os deve procurar.",
            "stay": "Passe pelo painel no dia em que jogam, para não ser apanhado de surpresa.",
            "need_help": "Algum problema? Escreva para {email}",
            "look_forward": "Boa sorte.",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
            "visit_website": "Visitar o site",
            "join_discord": "Juntar-se ao Discord",
        },
    },

    # ── afc_tournament_and_scrims: player accepted (to the player) ──
    # "status_word" is rendered by the builder inside a bold green <span> and injected into
    # "accepted" as {status}, so the sentence carries the full stop and status_word carries none.
    "player_accepted": {
        "en": {
            "heading": "You're in",
            "dear": "Hi {player},",
            "accepted": "Your registration for {event_name} is {status}.",
            "status_word": "confirmed",
            "eligible": "You are on the player list. Room IDs, passwords and match times arrive as notifications in your AFC dashboard.",
            "questions": "Anything unclear? Write to {email}",
            "good_luck": "Good luck.",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Vous êtes inscrit",
            "dear": "Bonjour {player},",
            "accepted": "Votre inscription à {event_name} est {status}.",
            "status_word": "confirmée",
            "eligible": "Vous figurez sur la liste des joueurs. Les identifiants de salle, les mots de passe et les horaires des matchs arrivent sous forme de notifications dans votre tableau de bord AFC.",
            "questions": "Une question ? Écrivez à {email}",
            "good_luck": "Bonne chance.",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Está inscrito",
            "dear": "Olá {player},",
            "accepted": "A sua inscrição em {event_name} está {status}.",
            "status_word": "confirmada",
            "eligible": "Está na lista de jogadores. Os IDs de sala, as palavras-passe e os horários das partidas chegam como notificações no seu painel AFC.",
            "questions": "Alguma dúvida? Escreva para {email}",
            "good_luck": "Boa sorte.",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player accepted (to the team owner) ──
    # Here "status_word" stands ALONE after "status_label" ("Status: Accepted"), so unlike the
    # player-facing template above it has to be a standalone word, not a sentence fragment.
    "player_accepted_owner": {
        "en": {
            "heading": "Roster update",
            "dear": "Hi {leader} ({team_name}),",
            "reviewed": "{player} has been checked for {event_name}.",
            "status_label": "Status:",
            "status_word": "Accepted",
            "track": "The rest of your roster's status is in your dashboard.",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "thanks": "Thanks for getting the roster in on time.",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Mise à jour de l'effectif",
            "dear": "Bonjour {leader} ({team_name}),",
            "reviewed": "{player} a été vérifié pour {event_name}.",
            "status_label": "Statut :",
            "status_word": "Accepté",
            "track": "L'état du reste de votre effectif se trouve dans votre tableau de bord.",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "thanks": "Merci d'avoir envoyé votre effectif à temps.",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Atualização do plantel",
            "dear": "Olá {leader} ({team_name}),",
            "reviewed": "{player} foi verificado para {event_name}.",
            "status_label": "Estado:",
            "status_word": "Aceite",
            "track": "O estado do resto do seu plantel está no seu painel.",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "thanks": "Obrigado por ter enviado o plantel a tempo.",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player rejected (to the player) ──
    # Bad news, so: no exclamation, no encouragement, no "we regret to inform you". The reason box
    # is rendered unconditionally right after "rejected", and "correct" points at it.
    # "status_word" is injected as {status} inside a bold red <span>, which is why the sentence is
    # built around it rather than ending in it.
    "player_rejected": {
        "en": {
            "heading": "About your registration",
            "dear": "Hi {player},",
            "rejected": "Your registration for {event_name} {status}.",
            "status_word": "was not accepted",
            "reason_label": "Reason:",
            "correct": "Correct the problem above and submit your registration again.",
            "update_btn": "Update Registration",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Au sujet de votre inscription",
            "dear": "Bonjour {player},",
            "rejected": "Votre inscription à {event_name} {status}.",
            "status_word": "n'a pas été retenue",
            "reason_label": "Motif :",
            "correct": "Corrigez le point indiqué ci-dessus et renvoyez votre inscription.",
            "update_btn": "Mettre à jour l'inscription",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Sobre a sua inscrição",
            "dear": "Olá {player},",
            "rejected": "A sua inscrição em {event_name} {status}.",
            "status_word": "não foi aceite",
            "reason_label": "Motivo:",
            "correct": "Corrija o ponto indicado acima e volte a submeter a sua inscrição.",
            "update_btn": "Atualizar inscrição",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player rejected (to the team owner) ──
    # "reviewed" stays neutral because the verdict is already spelled out one line below in
    # "status_label" + "status_word"; saying it twice is the three-sentences-for-one problem.
    "player_rejected_owner": {
        "en": {
            "heading": "Roster update",
            "dear": "Hi {leader} ({team_name}),",
            "reviewed": "{player}'s entry for {event_name} has been reviewed.",
            "status_label": "Status:",
            "status_word": "Not accepted",
            "reason_label": "Reason:",
            "monitor": "Your full roster status is in your dashboard.",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Mise à jour de l'effectif",
            "dear": "Bonjour {leader} ({team_name}),",
            "reviewed": "L'inscription de {player} à {event_name} a été examinée.",
            "status_label": "Statut :",
            "status_word": "Non accepté",
            "reason_label": "Motif :",
            "monitor": "L'état complet de votre effectif se trouve dans votre tableau de bord.",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Atualização do plantel",
            "dear": "Olá {leader} ({team_name}),",
            "reviewed": "A inscrição de {player} em {event_name} foi analisada.",
            "status_label": "Estado:",
            "status_word": "Não aceite",
            "reason_label": "Motivo:",
            "monitor": "O estado completo do seu plantel está no seu painel.",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_player_market: application received (to team staff) ──
    # "applied_sub" sits directly under a very large application COUNT, so it reads as the caption
    # of that number and stays a fragment on purpose. "message" replaced "Don't let talent slip
    # away, log in to review, shortlist, and invite" with the one fact that matters: nothing happens
    # to these applications until a human opens them.
    "pm_application_received": {
        "en": {
            "header": "Your post is getting applications",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt}. Players are applying to your recruitment post for {team}.",
            "total_label": "Applications so far",
            "applied_sub": "players have applied to join your team",
            "message": "Applications sit in your queue until someone opens them. Read the profiles, then invite anyone worth a trial.",
            "cta": "Review applications",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Votre annonce reçoit des candidatures",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt}. Des joueurs postulent à votre annonce de recrutement pour {team}.",
            "total_label": "Candidatures reçues",
            "applied_sub": "joueurs ont postulé pour rejoindre votre équipe",
            "message": "Les candidatures restent dans votre file tant que personne ne les ouvre. Lisez les profils, puis invitez à l'essai ceux qui le méritent.",
            "cta": "Examiner les candidatures",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "A sua publicação está a receber candidaturas",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt}. Há jogadores a candidatar-se à sua publicação de recrutamento para {team}.",
            "total_label": "Candidaturas recebidas",
            "applied_sub": "jogadores candidataram-se para entrar na sua equipa",
            "message": "As candidaturas ficam na sua fila até alguém as abrir. Leia os perfis e convide para teste quem valer a pena.",
            "cta": "Analisar candidaturas",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: application rejected (to the player) ──
    # The worst offender in the old catalog: "After careful consideration, we regret to inform
    # you...", "keep honing your skills", "Every great player started somewhere", "your next
    # opportunity could be just around the corner", "We wish you the best of luck in your esports
    # journey." Five sentences of padding around one fact.
    # Now: the team decided, here is the reason, here is what you can actually do, and the footer
    # says whose decision it was. NOTE "footer" is rendered WITHOUT .format(), so it must carry no
    # placeholder, which is why it says "the team" rather than {team}.
    "pm_application_rejected": {
        "en": {
            "header": "About your application",
            "hi": "Hi {player},",
            "body": "{team} has read your application and decided not to take it further.",
            "reason_label": "Reason",
            "keep_going_title": "What to do next",
            "keep_going_body": "Other teams on the Player Market are recruiting right now, and nothing stops you applying to several at once.",
            "cta": "See who else is recruiting",
            "footer": "This decision was made by the team, not by AFC.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Au sujet de votre candidature",
            "hi": "Bonjour {player},",
            "body": "{team} a lu votre candidature et a décidé de ne pas y donner suite.",
            "reason_label": "Motif",
            "keep_going_title": "La suite",
            "keep_going_body": "D'autres équipes recrutent en ce moment sur le Player Market, et rien ne vous empêche de postuler à plusieurs à la fois.",
            "cta": "Voir qui recrute",
            "footer": "Cette décision vient de l'équipe, pas de l'AFC.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Sobre a sua candidatura",
            "hi": "Olá {player},",
            "body": "{team} leu a sua candidatura e decidiu não avançar com ela.",
            "reason_label": "Motivo",
            "keep_going_title": "O que fazer a seguir",
            "keep_going_body": "Há outras equipas a recrutar neste momento no Player Market, e nada o impede de se candidatar a várias ao mesmo tempo.",
            "cta": "Ver quem está a recrutar",
            "footer": "Esta decisão é da equipa, não da AFC.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: trial started (to the player) ──
    # "whatnext_body" tells the player where to reply, because this email arrives from
    # info@africanfreefirecommunity.com and a reply to it reaches AFC, not the team.
    "pm_trial_started_player": {
        "en": {
            "header": "Your trial has started",
            "hey": "{player}, {team} has picked you for a trial. There is now a trial chat where you and the team's staff can talk directly.",
            "team_label": "Team",
            "whatnext_title": "What happens next?",
            "whatnext_body": "The team uses that chat to set times and tell you what they want to see. Reply there, not to this email.",
            "cta": "Open Trial Chat",
            "footer": "This trial was started because you applied to {team} on the AFC Player Market.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Votre essai a commencé",
            "hey": "{player}, {team} vous a retenu pour un essai. Un chat d'essai est ouvert : vous pouvez y parler directement avec le staff de l'équipe.",
            "team_label": "Équipe",
            "whatnext_title": "Que se passe-t-il ensuite ?",
            "whatnext_body": "L'équipe se sert de ce chat pour fixer les horaires et vous dire ce qu'elle attend de vous. Répondez là-bas, pas à cet e-mail.",
            "cta": "Ouvrir le chat d'essai",
            "footer": "Cet essai a été lancé parce que vous avez postulé auprès de {team} sur le Player Market AFC.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "O seu teste começou",
            "hey": "{player}, {team} escolheu-o para um teste. Já existe um chat de teste onde pode falar diretamente com o staff da equipa.",
            "team_label": "Equipa",
            "whatnext_title": "O que acontece a seguir?",
            "whatnext_body": "A equipa usa esse chat para marcar horários e dizer-lhe o que quer ver. Responda por lá, não a este e-mail.",
            "cta": "Abrir chat de teste",
            "footer": "Este teste foi iniciado porque se candidatou a {team} no Player Market da AFC.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: trial started (to the team staff) ──
    "pm_trial_started_team": {
        "en": {
            "header": "Trial started",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt},",
            "body": "{player} is now on trial with your team. The trial chat is open, so you can set times and talk to them there.",
            "player_label": "Player on Trial",
            "cta": "Open Trial Chat",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Essai lancé",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt},",
            "body": "{player} est désormais à l'essai avec votre équipe. Le chat d'essai est ouvert : vous pouvez y fixer les horaires et lui parler.",
            "player_label": "Joueur à l'essai",
            "cta": "Ouvrir le chat d'essai",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Teste iniciado",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt},",
            "body": "{player} está agora em teste com a sua equipa. O chat de teste está aberto, por isso pode marcar horários e falar com ele por lá.",
            "player_label": "Jogador em teste",
            "cta": "Abrir chat de teste",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: direct trial invite (to the player) ──
    # This one has a real deadline, so the window copy is the loudest thing in it and says what
    # happens when the clock runs out. {hours} is injected as "72 hours" (hours_text) by the
    # builder, which is why the sentence reads around it rather than hardcoding the number.
    "pm_trial_invite": {
        "en": {
            "header": "A team wants you",
            "hey": "{player}, {team} saw your availability post and wants you in for a trial.",
            "team_inviting": "Team Inviting You",
            "message_label": "Message",
            "window_title": "You have 72 hours",
            "window_body": "Accept or decline within {hours}. After that the invite expires and the team would have to send a new one.",
            "hours_text": "72 hours",
            "cta": "See the invite",
            "footer": "This invite was sent because you have an active availability post on the AFC Player Market.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Une équipe vous veut",
            "hey": "{player}, {team} a vu votre annonce de disponibilité et vous veut à l'essai.",
            "team_inviting": "Équipe qui vous invite",
            "message_label": "Message",
            "window_title": "Vous avez 72 heures",
            "window_body": "Acceptez ou refusez dans un délai de {hours}. Passé ce délai, l'invitation expire et l'équipe devrait en envoyer une nouvelle.",
            "hours_text": "72 heures",
            "cta": "Voir l'invitation",
            "footer": "Cette invitation vous est envoyée parce que vous avez une annonce de disponibilité active sur le Player Market AFC.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Uma equipa quer contar consigo",
            "hey": "{player}, {team} viu a sua publicação de disponibilidade e quer contar consigo para um teste.",
            "team_inviting": "Equipa que o convida",
            "message_label": "Mensagem",
            "window_title": "Tem 72 horas",
            "window_body": "Aceite ou recuse dentro de {hours}. Passado esse prazo o convite expira e a equipa teria de enviar um novo.",
            "hours_text": "72 horas",
            "cta": "Ver o convite",
            "footer": "Este convite foi enviado porque tem uma publicação de disponibilidade ativa no Player Market da AFC.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: direct trial invite accepted (to the team staff) ──
    "pm_trial_accepted_team": {
        "en": {
            "header": "Trial accepted",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt},",
            "body": "{player} accepted your trial invite. The trial chat is open.",
            "player_label": "Player on Trial",
            "cta": "Open Trial Chat",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Essai accepté",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt},",
            "body": "{player} a accepté votre invitation à l'essai. Le chat d'essai est ouvert.",
            "player_label": "Joueur à l'essai",
            "cta": "Ouvrir le chat d'essai",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Teste aceite",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt},",
            "body": "{player} aceitou o seu convite para teste. O chat de teste está aberto.",
            "player_label": "Jogador em teste",
            "cta": "Abrir chat de teste",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ─────────────────────────────────────────────────────────────────────────────────────────
    # afc_partner_apply: the four transitions of a partner application (owner 2026-08-04)
    # ─────────────────────────────────────────────────────────────────────────────────────────
    # Rendered by afc_partner_apply/emails.py, which lays the listed keys out as one heading plus
    # one paragraph each. {link} and {claim_link} arrive as finished HTML anchors, so the sentence
    # reads naturally around them rather than around a bare URL.
    #
    # The recipient is an ORGANISATION, not a player, so the register is a shade more formal than
    # the rest of this catalog and never uses an in-game name.
    #
    # LEFT AS WRITTEN by the backlog-#18 rewrite: these four were authored last and already follow
    # the rules at the top of this file, so they were re-read and kept rather than re-worded.
    #
    # NOTE WHAT THE APPROVAL COPY DOES NOT SAY: it never contains a client secret or an API key.
    # It points at a single-use link that expires, because an inbox does not. See the module
    # header of afc_partner_apply/emails.py.
    "partner_apply_received": {
        "en": {
            "heading": "Your application is with us",
            "intro": "Thanks for applying to become an AFC partner. We have your application for {organisation}, and its reference is {reference}.",
            "next_steps": "An AFC admin will read it and decide, and we aim to get back to you within a few working days. We will email you either way, and if anything needs correcting we will tell you exactly what.",
            "what_it_is": "You have applied for Sign in with AFC. It lets an AFC player sign in to your site with their AFC account, using standard OpenID Connect, so you never handle their password. Once you are approved you receive a client id and a client secret, and you choose which player details you need; AFC grants the smallest set that does the job.",
            "guide": "You do not have to wait to start reading. The full integration guide is here: {guide}. It covers the endpoints, the scopes, the claims each one returns, the error responses and a complete worked example.",
            "keep_link": "You can check the status at any time here: {link}. Keep this email, the link is how you reach your application.",
        },
        "fr": {
            "heading": "Nous avons votre demande",
            "intro": "Merci d'avoir demandé à devenir partenaire AFC. Nous avons bien reçu la demande de {organisation}, dont la référence est {reference}.",
            "next_steps": "Un administrateur AFC va la lire et se prononcer, et nous visons une réponse sous quelques jours ouvrés. Nous vous écrirons dans les deux cas, et si quelque chose doit être corrigé, nous vous dirons précisément quoi.",
            "what_it_is": "Vous avez demandé Sign in with AFC. Cela permet à un joueur AFC de se connecter à votre site avec son compte AFC, via OpenID Connect standard, sans que vous ayez jamais à manipuler son mot de passe. Une fois approuvé, vous recevez un identifiant client et un secret client, et vous choisissez les informations dont vous avez besoin ; l'AFC accorde le strict nécessaire.",
            "guide": "Vous pouvez commencer à lire dès maintenant. Le guide d'intégration complet est ici : {guide}. Il couvre les points de terminaison, les portées, les données renvoyées par chacune, les réponses d'erreur et un exemple complet.",
            "keep_link": "Vous pouvez suivre l'état de votre demande à tout moment ici : {link}. Conservez cet e-mail, ce lien est votre accès à votre demande.",
        },
        "pt": {
            "heading": "Temos a sua candidatura",
            "intro": "Obrigado por se candidatar a parceiro da AFC. Recebemos a candidatura de {organisation} e a respetiva referência é {reference}.",
            "next_steps": "Um administrador da AFC vai lê-la e decidir, e procuramos responder dentro de alguns dias úteis. Enviaremos um e-mail em qualquer dos casos e, se algo precisar de ser corrigido, diremos exatamente o quê.",
            "what_it_is": "Candidatou-se ao Sign in with AFC. Permite que um jogador da AFC inicie sessão no seu site com a conta AFC, através de OpenID Connect padrão, sem que alguma vez tenha de lidar com a palavra-passe dele. Depois de aprovado, recebe um id de cliente e um segredo de cliente, e escolhe que dados de jogador precisa; a AFC concede o mínimo necessário.",
            "guide": "Não precisa de esperar para começar a ler. O guia de integração completo está aqui: {guide}. Cobre os endpoints, os scopes, os dados que cada um devolve, as respostas de erro e um exemplo completo.",
            "keep_link": "Pode consultar o estado a qualquer momento aqui: {link}. Guarde este e-mail, esta ligação é a sua forma de aceder à candidatura.",
        },
    },
    "partner_apply_changes": {
        "en": {
            "heading": "One thing to fix",
            "intro": "We have read the application for {organisation} ({reference}) and we need something changed before we can decide.",
            "note": "Here is what AFC asked for: {note}",
            "how_to_fix": "Open your application here to make the change: {link}. Once you send it back it returns to our queue automatically, so there is nothing else to do.",
        },
        "fr": {
            "heading": "Un point à corriger",
            "intro": "Nous avons lu la demande de {organisation} ({reference}) et un point doit être modifié avant que nous puissions décider.",
            "note": "Voici ce que l'AFC demande : {note}",
            "how_to_fix": "Ouvrez votre demande ici pour la modifier : {link}. Dès que vous la renvoyez, elle revient automatiquement dans notre file, vous n'avez rien d'autre à faire.",
        },
        "pt": {
            "heading": "Um ponto a corrigir",
            "intro": "Lemos a candidatura de {organisation} ({reference}) e é preciso alterar um ponto antes de podermos decidir.",
            "note": "Eis o que a AFC pediu: {note}",
            "how_to_fix": "Abra a sua candidatura aqui para fazer a alteração: {link}. Assim que a reenviar, volta automaticamente para a nossa fila, não tem mais nada a fazer.",
        },
    },
    "partner_apply_approved": {
        "en": {
            "heading": "You are an AFC partner",
            "intro": "The application for {organisation} ({reference}) is approved and everything on the AFC side is set up.",
            "credentials": "Collect your credentials here: {claim_link}. We deliberately do not send them by email. The link works once, and your client secret is shown a single time on that page, so have somewhere safe to paste it before you open it.",
            "expiry": "The link stops working after {hours} hours. If you miss it, or you lose the secret afterwards, ask us and we will send a new link.",
            "guide": "Your application page stays available here: {link}. AFC will also send you the partner integration guide, which covers every endpoint, what each scope releases, and a full worked integration.",
        },
        "fr": {
            "heading": "Vous êtes partenaire AFC",
            "intro": "La demande de {organisation} ({reference}) est approuvée et tout est en place du côté de l'AFC.",
            "credentials": "Récupérez vos identifiants ici : {claim_link}. Nous ne les envoyons volontairement pas par e-mail. Le lien ne fonctionne qu'une fois et votre secret client n'est affiché qu'une seule fois sur cette page, alors prévoyez un endroit sûr où le coller avant de l'ouvrir.",
            "expiry": "Le lien cesse de fonctionner au bout de {hours} heures. Si vous le manquez, ou si vous perdez le secret ensuite, demandez-nous et nous vous en enverrons un nouveau.",
            "guide": "La page de votre demande reste accessible ici : {link}. L'AFC vous transmettra également le guide d'intégration partenaire, qui couvre chaque endpoint, ce que chaque scope communique et une intégration complète commentée.",
        },
        "pt": {
            "heading": "É agora parceiro da AFC",
            "intro": "A candidatura de {organisation} ({reference}) foi aprovada e está tudo preparado do lado da AFC.",
            "credentials": "Recolha as suas credenciais aqui: {claim_link}. Não as enviamos por e-mail, e isso é intencional. A ligação funciona uma única vez e o seu segredo de cliente é mostrado uma só vez nessa página, por isso tenha um local seguro para o colar antes de a abrir.",
            "expiry": "A ligação deixa de funcionar ao fim de {hours} horas. Se a perder, ou se perder o segredo depois, peça-nos e enviamos uma nova.",
            "guide": "A página da sua candidatura continua disponível aqui: {link}. A AFC enviará também o guia de integração para parceiros, que cobre todos os endpoints, o que cada scope disponibiliza e uma integração completa comentada.",
        },
    },
    "partner_apply_rejected": {
        "en": {
            "heading": "About your application",
            "intro": "We have read the application for {organisation} ({reference}), and we are not able to approve it.",
            "note": "Here is why: {note}",
            "reapply": "This is not permanent. If what we raised changes, you are welcome to apply again, and you can reply to this email if anything is unclear.",
        },
        "fr": {
            "heading": "Au sujet de votre demande",
            "intro": "Nous avons lu la demande de {organisation} ({reference}) et nous ne sommes pas en mesure de l'approuver.",
            "note": "En voici la raison : {note}",
            "reapply": "Ce n'est pas définitif. Si le point soulevé évolue, vous pouvez tout à fait déposer une nouvelle demande, et vous pouvez répondre à cet e-mail si quelque chose n'est pas clair.",
        },
        "pt": {
            "heading": "Sobre a sua candidatura",
            "intro": "Lemos a candidatura de {organisation} ({reference}) e não nos é possível aprová-la.",
            "note": "O motivo é o seguinte: {note}",
            "reapply": "Isto não é definitivo. Se o ponto que levantámos mudar, pode voltar a candidatar-se, e pode responder a este e-mail se algo não estiver claro.",
        },
    },
}
