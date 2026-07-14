# afc_auth/email_i18n.py
#
# HAND-AUTHORED transactional-email copy catalog (owner 2026-07-13).
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
#
# COPY RULES
#   - NO em/en dashes anywhere (AFC hard rule). Use commas, colons, parentheses, or a spaced hyphen.
#   - English copy is kept identical to what shipped before this catalog (the ONLY exception being
#     a few legacy em dashes in player-market copy, which are replaced with a comma to satisfy the
#     hard rule above).
#   - Every value is a str.format() template: only {placeholder} tokens are substituted, so the
#     natural sentence stays intact and the dynamic value is dropped in.


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
        "en": "Welcome to African Free Fire Community",
        "fr": "Bienvenue dans African Free Fire Community",
        "pt": "Bem-vindo à African Free Fire Community",
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
    "team_registered": {
        "en": "AFC Registration Update: Your Team {team_name} is now Fully Registered for {event_name}",
        "fr": "Mise à jour d'inscription AFC : votre équipe {team_name} est désormais entièrement inscrite à {event_name}",
        "pt": "Atualização de inscrição AFC: a sua equipa {team_name} está agora totalmente inscrita em {event_name}",
    },
    "player_accepted": {
        "en": "AFC Registration Update: Your Application for {event_name} Has Been Accepted",
        "fr": "Mise à jour d'inscription AFC : votre candidature pour {event_name} a été acceptée",
        "pt": "Atualização de inscrição AFC: a sua candidatura para {event_name} foi aceite",
    },
    "player_accepted_owner": {
        "en": "AFC Registration Update: Player {player} Accepted for {event_name}",
        "fr": "Mise à jour d'inscription AFC : joueur {player} accepté pour {event_name}",
        "pt": "Atualização de inscrição AFC: jogador {player} aceite para {event_name}",
    },
    "player_rejected": {
        "en": "AFC Registration Update: Your Application for {event_name} Has Been Rejected",
        "fr": "Mise à jour d'inscription AFC : votre candidature pour {event_name} a été refusée",
        "pt": "Atualização de inscrição AFC: a sua candidatura para {event_name} foi recusada",
    },
    "player_rejected_owner": {
        "en": "AFC Registration Update: Player {player} Rejected for {event_name}",
        "fr": "Mise à jour d'inscription AFC : joueur {player} refusé pour {event_name}",
        "pt": "Atualização de inscrição AFC: jogador {player} recusado para {event_name}",
    },

    # ── afc_player_market ──
    "pm_application_received": {
        "en": "Your Player Market post is getting attention!",
        "fr": "Votre annonce sur le Player Market attire l'attention !",
        "pt": "A sua publicação no Player Market está a chamar a atenção!",
    },
    "pm_application_rejected": {
        "en": "Application Update from {team_name}",
        "fr": "Mise à jour de votre candidature de la part de {team_name}",
        "pt": "Atualização da candidatura de {team_name}",
    },
    "pm_trial_started_player": {
        "en": "You've Been Added to a Trial with {team_name}!",
        "fr": "Vous avez été ajouté à un essai avec {team_name} !",
        "pt": "Foi adicionado a um teste com {team_name}!",
    },
    "pm_trial_started_team": {
        "en": "Trial Started: {player} has been added!",
        "fr": "Essai lancé : {player} a été ajouté !",
        "pt": "Teste iniciado: {player} foi adicionado!",
    },
    "pm_trial_invite": {
        "en": "Trial Invite from {team_name}",
        "fr": "Invitation à un essai de la part de {team_name}",
        "pt": "Convite para teste de {team_name}",
    },
    "pm_trial_accepted_team": {
        "en": "{player} accepted your trial invite!",
        "fr": "{player} a accepté votre invitation à un essai !",
        "pt": "{player} aceitou o seu convite para teste!",
    },
}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# COPY: template -> language -> {sentence-key: str}. Each string is a str.format() template; the
# builder injects the HTML-wrapped dynamic value(s). Keys are shared in meaning across languages.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
COPY = {
    # ── afc_auth: verification code (signup + resend) ──
    "verification_code": {
        "en": {
            "heading": "Verify your account",
            "intro": "Hi {username}, welcome to the arena. Enter this code on {site} to finish creating your account.",
            "expires": "This code expires in 10 minutes.",
            "disclaimer": "If you did not create an AFC account, you can safely ignore this email. Never share this code with anyone, AFC staff will never ask for it.",
        },
        "fr": {
            "heading": "Vérifiez votre compte",
            "intro": "Bonjour {username}, bienvenue dans l'arène. Saisissez ce code sur {site} pour finaliser la création de votre compte.",
            "expires": "Ce code expire dans 10 minutes.",
            "disclaimer": "Si vous n'avez pas créé de compte AFC, vous pouvez ignorer cet e-mail en toute sécurité. Ne partagez jamais ce code avec qui que ce soit, l'équipe AFC ne vous le demandera jamais.",
        },
        "pt": {
            "heading": "Verifique a sua conta",
            "intro": "Olá {username}, bem-vindo à arena. Introduza este código em {site} para concluir a criação da sua conta.",
            "expires": "Este código expira em 10 minutos.",
            "disclaimer": "Se não criou uma conta AFC, pode ignorar este e-mail com segurança. Nunca partilhe este código com ninguém, a equipa da AFC nunca o irá pedir.",
        },
    },

    # ── afc_auth: welcome ──
    "welcome": {
        "en": {
            "heading": "You're in, {username}",
            "intro": "Your account is verified and ready. Join tournaments, climb the rankings, build your team, and rep your country across Africa.",
            "cta": "Enter the Community",
            "feat1": "Compete in tournaments",
            "feat2": "Climb the rankings",
            "feat3": "Find your team",
        },
        "fr": {
            "heading": "Vous y êtes, {username}",
            "intro": "Votre compte est vérifié et prêt. Participez à des tournois, grimpez au classement, montez votre équipe et représentez votre pays à travers l'Afrique.",
            "cta": "Entrer dans la communauté",
            "feat1": "Participez à des tournois",
            "feat2": "Grimpez au classement",
            "feat3": "Trouvez votre équipe",
        },
        "pt": {
            "heading": "Está dentro, {username}",
            "intro": "A sua conta está verificada e pronta. Participe em torneios, suba na classificação, construa a sua equipa e represente o seu país por toda a África.",
            "cta": "Entrar na comunidade",
            "feat1": "Compita em torneios",
            "feat2": "Suba na classificação",
            "feat3": "Encontre a sua equipa",
        },
    },

    # ── afc_auth: password reset token ──
    "reset_token": {
        "en": {
            "heading": "Reset your password",
            "intro": "We received a request to reset your password. Use the token below to set a new one.",
            "expires": "This token expires in 10 minutes.",
            "disclaimer": "If you did not request a password reset, ignore this email, your password stays unchanged. Never share this token.",
        },
        "fr": {
            "heading": "Réinitialisez votre mot de passe",
            "intro": "Nous avons reçu une demande de réinitialisation de votre mot de passe. Utilisez le jeton ci-dessous pour en définir un nouveau.",
            "expires": "Ce jeton expire dans 10 minutes.",
            "disclaimer": "Si vous n'avez pas demandé de réinitialisation de mot de passe, ignorez cet e-mail, votre mot de passe reste inchangé. Ne partagez jamais ce jeton.",
        },
        "pt": {
            "heading": "Redefina a sua palavra-passe",
            "intro": "Recebemos um pedido para redefinir a sua palavra-passe. Utilize o código abaixo para definir uma nova.",
            "expires": "Este código expira em 10 minutos.",
            "disclaimer": "Se não solicitou a redefinição da palavra-passe, ignore este e-mail, a sua palavra-passe permanece inalterada. Nunca partilhe este código.",
        },
    },

    # ── afc_auth: password changed confirmation ──
    "password_changed": {
        "en": {
            "heading": "Your password was changed",
            "intro": "This confirms the password for {username} was updated on {when}.",
            "warning": "Did not do this? Your account may be at risk. Reset your password immediately and contact {support}.",
            "support_label": "support",
        },
        "fr": {
            "heading": "Votre mot de passe a été modifié",
            "intro": "Ceci confirme que le mot de passe de {username} a été mis à jour le {when}.",
            "warning": "Vous n'êtes pas à l'origine de cette action ? Votre compte pourrait être menacé. Réinitialisez immédiatement votre mot de passe et contactez {support}.",
            "support_label": "le support",
        },
        "pt": {
            "heading": "A sua palavra-passe foi alterada",
            "intro": "Isto confirma que a palavra-passe de {username} foi atualizada em {when}.",
            "warning": "Não foi você? A sua conta pode estar em risco. Redefina imediatamente a sua palavra-passe e contacte {support}.",
            "support_label": "o suporte",
        },
    },

    # ── afc_auth: confirm new email (code to the new address) ──
    "change_code": {
        "en": {
            "heading": "Confirm your new email",
            "intro": "Someone (hopefully you) asked to switch an AFC account's email to this address. Enter the code below on the profile settings page to confirm it.",
            "expires": "This code expires in 10 minutes.",
            "disclaimer": "If you did not request this, you can ignore this email, no account was changed. Never share this code.",
        },
        "fr": {
            "heading": "Confirmez votre nouvelle adresse e-mail",
            "intro": "Quelqu'un (nous espérons que c'est vous) a demandé à remplacer l'adresse e-mail d'un compte AFC par cette adresse. Saisissez le code ci-dessous sur la page des paramètres du profil pour le confirmer.",
            "expires": "Ce code expire dans 10 minutes.",
            "disclaimer": "Si vous n'êtes pas à l'origine de cette demande, vous pouvez ignorer cet e-mail, aucun compte n'a été modifié. Ne partagez jamais ce code.",
        },
        "pt": {
            "heading": "Confirme o seu novo e-mail",
            "intro": "Alguém (esperamos que tenha sido você) pediu para mudar o e-mail de uma conta AFC para este endereço. Introduza o código abaixo na página de definições do perfil para o confirmar.",
            "expires": "Este código expira em 10 minutos.",
            "disclaimer": "Se não solicitou isto, pode ignorar este e-mail, nenhuma conta foi alterada. Nunca partilhe este código.",
        },
    },

    # ── afc_auth: email changed confirmation (to old + new address) ──
    "email_changed": {
        "en": {
            "heading": "Your account email was changed",
            "intro": "The email on {username}'s AFC account was changed to {new_email} on {when}. Sign in with your new email from now on.",
            "warning": "Did not do this? Your account may be at risk. Contact {support} right away.",
            "support_label": "support",
        },
        "fr": {
            "heading": "L'adresse e-mail de votre compte a été modifiée",
            "intro": "L'adresse e-mail du compte AFC de {username} a été remplacée par {new_email} le {when}. Connectez-vous désormais avec votre nouvelle adresse e-mail.",
            "warning": "Vous n'êtes pas à l'origine de cette action ? Votre compte pourrait être menacé. Contactez {support} immédiatement.",
            "support_label": "le support",
        },
        "pt": {
            "heading": "O e-mail da sua conta foi alterado",
            "intro": "O e-mail da conta AFC de {username} foi alterado para {new_email} em {when}. A partir de agora, inicie sessão com o seu novo e-mail.",
            "warning": "Não foi você? A sua conta pode estar em risco. Contacte {support} imediatamente.",
            "support_label": "o suporte",
        },
    },

    # ── afc_shop: order lifecycle + shared summary labels ──
    "order_received": {
        "en": {
            "heading": "We received your order",
            "intro": "Hi {buyer}, thank you for your purchase. Your payment is confirmed and the seller is preparing your order. We will email you again when it ships.",
            "track": "You can track this order any time at {link}.",
        },
        "fr": {
            "heading": "Nous avons reçu votre commande",
            "intro": "Bonjour {buyer}, merci pour votre achat. Votre paiement est confirmé et le vendeur prépare votre commande. Nous vous enverrons un nouvel e-mail lors de l'expédition.",
            "track": "Vous pouvez suivre cette commande à tout moment sur {link}.",
        },
        "pt": {
            "heading": "Recebemos a sua encomenda",
            "intro": "Olá {buyer}, obrigado pela sua compra. O seu pagamento está confirmado e o vendedor está a preparar a sua encomenda. Iremos enviar-lhe um novo e-mail quando for expedida.",
            "track": "Pode acompanhar esta encomenda a qualquer momento em {link}.",
        },
    },
    "order_shipped": {
        "en": {
            "heading": "Your order is on the way",
            "intro": "Good news, {buyer}. Your order has been shipped and is heading to you.",
            "ship_label": "Estimated ship date:",
            "questions": "Questions about delivery? Reach us at {link}.",
        },
        "fr": {
            "heading": "Votre commande est en route",
            "intro": "Bonne nouvelle, {buyer}. Votre commande a été expédiée et arrive vers vous.",
            "ship_label": "Date d'expédition estimée :",
            "questions": "Des questions sur la livraison ? Contactez-nous sur {link}.",
        },
        "pt": {
            "heading": "A sua encomenda está a caminho",
            "intro": "Boas notícias, {buyer}. A sua encomenda foi expedida e está a caminho.",
            "ship_label": "Data de expedição estimada:",
            "questions": "Dúvidas sobre a entrega? Contacte-nos em {link}.",
        },
    },
    "order_completed": {
        "en": {
            "heading": "Your order is complete",
            "intro": "Thank you, {buyer}. Your order has been delivered and is now complete. We hope you enjoy it.",
            "shop_again": "Shop again any time at {link}.",
        },
        "fr": {
            "heading": "Votre commande est terminée",
            "intro": "Merci, {buyer}. Votre commande a été livrée et est désormais terminée. Nous espérons qu'elle vous plaira.",
            "shop_again": "Faites vos achats à tout moment sur {link}.",
        },
        "pt": {
            "heading": "A sua encomenda está concluída",
            "intro": "Obrigado, {buyer}. A sua encomenda foi entregue e está agora concluída. Esperamos que goste.",
            "shop_again": "Compre novamente a qualquer momento em {link}.",
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
            "intro": "Order #{order_no} is paid and ready to fulfil. Buyer: {buyer}. Open your fulfilment page on {link} to acknowledge it and set a ship date.",
        },
        "fr": {
            "heading": "Vous avez une nouvelle commande",
            "intro": "La commande n° {order_no} est payée et prête à être traitée. Acheteur : {buyer}. Ouvrez votre page de traitement sur {link} pour la confirmer et définir une date d'expédition.",
        },
        "pt": {
            "heading": "Tem uma nova encomenda",
            "intro": "A encomenda n.º {order_no} está paga e pronta a ser processada. Comprador: {buyer}. Abra a sua página de processamento em {link} para a confirmar e definir uma data de expedição.",
        },
    },

    # ── afc_sponsors: registration rejection (reason is free text, injected untranslated) ──
    "sponsor_reject_final": {
        "en": {
            "title": "Registration rejected for {event_name}",
            "body": "{sponsor} rejected your registration for {event_name}. Reason: {reason}. Your slot has been released.",
        },
        "fr": {
            "title": "Inscription refusée pour {event_name}",
            "body": "{sponsor} a refusé votre inscription pour {event_name}. Raison : {reason}. Votre place a été libérée.",
        },
        "pt": {
            "title": "Inscrição recusada para {event_name}",
            "body": "{sponsor} recusou a sua inscrição para {event_name}. Motivo: {reason}. A sua vaga foi libertada.",
        },
    },
    "sponsor_reject_retry": {
        "en": {
            "title": "Action needed: fix your {label} for {event_name}",
            "body": "{sponsor} rejected your {label} for {event_name}. Reason: {reason}. Open the event page and re-enter the correct value; your registration stays pending until the sponsor approves it.",
        },
        "fr": {
            "title": "Action requise : corrigez votre {label} pour {event_name}",
            "body": "{sponsor} a refusé votre {label} pour {event_name}. Raison : {reason}. Ouvrez la page de l'événement et saisissez à nouveau la valeur correcte ; votre inscription reste en attente jusqu'à ce que le sponsor l'approuve.",
        },
        "pt": {
            "title": "Ação necessária: corrija o seu {label} para {event_name}",
            "body": "{sponsor} recusou o seu {label} para {event_name}. Motivo: {reason}. Abra a página do evento e volte a introduzir o valor correto; a sua inscrição permanece pendente até que o patrocinador a aprove.",
        },
    },

    # ── afc_tournament_and_scrims: team fully registered (to the team owner) ──
    "team_registered": {
        "en": {
            "congrats": "Congratulations",
            "dear": "Dear {leader} (Team {team_name}),",
            "verified": "We are pleased to inform you that all members of your team have been successfully verified and accepted.",
            "box": "Your team {team_name} is now fully registered for {event_name}.",
            "match_details": "All match details (room IDs, passwords, schedules) will be available in your AFC dashboard notifications.",
            "stay": "Stay prepared and keep checking the platform regularly.",
            "need_help": "Need help? Contact us at {email}",
            "look_forward": "We look forward to seeing your team compete!",
            "regards": "Best regards,",
            "board": "AFC Management Board",
            "visit_website": "Visit Website",
            "join_discord": "Join Discord",
        },
        "fr": {
            "congrats": "Félicitations",
            "dear": "Bonjour {leader} (équipe {team_name}),",
            "verified": "Nous avons le plaisir de vous informer que tous les membres de votre équipe ont été vérifiés et acceptés avec succès.",
            "box": "Votre équipe {team_name} est désormais entièrement inscrite à {event_name}.",
            "match_details": "Tous les détails des matchs (identifiants de salle, mots de passe, horaires) seront disponibles dans les notifications de votre tableau de bord AFC.",
            "stay": "Restez prêts et consultez régulièrement la plateforme.",
            "need_help": "Besoin d'aide ? Contactez-nous à {email}",
            "look_forward": "Nous avons hâte de voir votre équipe en compétition !",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
            "visit_website": "Visiter le site",
            "join_discord": "Rejoindre Discord",
        },
        "pt": {
            "congrats": "Parabéns",
            "dear": "Olá {leader} (equipa {team_name}),",
            "verified": "Temos o prazer de informar que todos os membros da sua equipa foram verificados e aceites com sucesso.",
            "box": "A sua equipa {team_name} está agora totalmente inscrita em {event_name}.",
            "match_details": "Todos os detalhes das partidas (IDs de sala, palavras-passe, horários) estarão disponíveis nas notificações do seu painel AFC.",
            "stay": "Mantenham-se preparados e consultem a plataforma regularmente.",
            "need_help": "Precisa de ajuda? Contacte-nos em {email}",
            "look_forward": "Estamos ansiosos por ver a sua equipa competir!",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
            "visit_website": "Visitar o site",
            "join_discord": "Juntar-se ao Discord",
        },
    },

    # ── afc_tournament_and_scrims: player accepted (to the player) ──
    "player_accepted": {
        "en": {
            "heading": "Registration Accepted",
            "dear": "Dear {player},",
            "accepted": "Your registration for {event_name} has been {status}",
            "status_word": "verified and accepted!",
            "eligible": "You are now eligible to participate. Match details will be available in your dashboard.",
            "questions": "If you have questions, contact: {email}",
            "good_luck": "Good luck in the tournament!",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Inscription acceptée",
            "dear": "Bonjour {player},",
            "accepted": "Votre inscription pour {event_name} a été {status}",
            "status_word": "vérifiée et acceptée !",
            "eligible": "Vous êtes désormais éligible pour participer. Les détails des matchs seront disponibles dans votre tableau de bord.",
            "questions": "Si vous avez des questions, contactez : {email}",
            "good_luck": "Bonne chance dans le tournoi !",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Inscrição aceite",
            "dear": "Olá {player},",
            "accepted": "A sua inscrição para {event_name} foi {status}",
            "status_word": "verificada e aceite!",
            "eligible": "Está agora elegível para participar. Os detalhes das partidas estarão disponíveis no seu painel.",
            "questions": "Se tiver dúvidas, contacte: {email}",
            "good_luck": "Boa sorte no torneio!",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player accepted (to the team owner) ──
    "player_accepted_owner": {
        "en": {
            "heading": "Player Status Update",
            "dear": "Dear {leader} (Team {team_name}),",
            "reviewed": "Player {player} has been reviewed for {event_name}.",
            "status_label": "Status:",
            "status_word": "Accepted",
            "track": "You can track all players in your dashboard.",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "thanks": "Thanks for your participation.",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Mise à jour du statut du joueur",
            "dear": "Bonjour {leader} (équipe {team_name}),",
            "reviewed": "Le joueur {player} a été examiné pour {event_name}.",
            "status_label": "Statut :",
            "status_word": "Accepté",
            "track": "Vous pouvez suivre tous les joueurs dans votre tableau de bord.",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "thanks": "Merci pour votre participation.",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Atualização do estado do jogador",
            "dear": "Olá {leader} (equipa {team_name}),",
            "reviewed": "O jogador {player} foi avaliado para {event_name}.",
            "status_label": "Estado:",
            "status_word": "Aceite",
            "track": "Pode acompanhar todos os jogadores no seu painel.",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "thanks": "Obrigado pela sua participação.",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player rejected (to the player) ──
    "player_rejected": {
        "en": {
            "heading": "Registration Update",
            "dear": "Dear {player},",
            "rejected": "Your application for {event_name} has been {status}.",
            "status_word": "rejected",
            "reason_label": "Reason:",
            "correct": "Please correct the issue and re-submit your registration.",
            "update_btn": "Update Registration",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Mise à jour de l'inscription",
            "dear": "Bonjour {player},",
            "rejected": "Votre candidature pour {event_name} a été {status}.",
            "status_word": "refusée",
            "reason_label": "Raison :",
            "correct": "Veuillez corriger le problème et soumettre à nouveau votre inscription.",
            "update_btn": "Mettre à jour l'inscription",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Atualização da inscrição",
            "dear": "Olá {player},",
            "rejected": "A sua candidatura para {event_name} foi {status}.",
            "status_word": "recusada",
            "reason_label": "Motivo:",
            "correct": "Por favor, corrija o problema e volte a submeter a sua inscrição.",
            "update_btn": "Atualizar inscrição",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_tournament_and_scrims: player rejected (to the team owner) ──
    "player_rejected_owner": {
        "en": {
            "heading": "Player Status Update",
            "dear": "Dear {leader} (Team {team_name}),",
            "reviewed": "Player {player} has been reviewed for {event_name}.",
            "status_label": "Status:",
            "status_word": "Rejected",
            "reason_label": "Reason:",
            "monitor": "You can monitor your team in the dashboard.",
            "need_help": "Need help? {contact}",
            "contact_support": "Contact support",
            "regards": "Best regards,",
            "board": "AFC Management Board",
        },
        "fr": {
            "heading": "Mise à jour du statut du joueur",
            "dear": "Bonjour {leader} (équipe {team_name}),",
            "reviewed": "Le joueur {player} a été examiné pour {event_name}.",
            "status_label": "Statut :",
            "status_word": "Refusé",
            "reason_label": "Raison :",
            "monitor": "Vous pouvez suivre votre équipe dans le tableau de bord.",
            "need_help": "Besoin d'aide ? {contact}",
            "contact_support": "Contactez le support",
            "regards": "Cordialement,",
            "board": "Le conseil de direction AFC",
        },
        "pt": {
            "heading": "Atualização do estado do jogador",
            "dear": "Olá {leader} (equipa {team_name}),",
            "reviewed": "O jogador {player} foi avaliado para {event_name}.",
            "status_label": "Estado:",
            "status_word": "Recusado",
            "reason_label": "Motivo:",
            "monitor": "Pode acompanhar a sua equipa no painel.",
            "need_help": "Precisa de ajuda? {contact}",
            "contact_support": "Contacte o suporte",
            "regards": "Com os melhores cumprimentos,",
            "board": "A direção da AFC",
        },
    },

    # ── afc_player_market: application received (to team staff) ──
    "pm_application_received": {
        "en": {
            "header": "Your Post Is Getting Attention!",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt}, your recruitment post for {team} is attracting players!",
            "total_label": "Total Applications",
            "applied_sub": "players have applied to join your team",
            "message": "Don't let talent slip away, log in to review your applications, shortlist the best candidates, and invite players to trial.",
            "cta": "Review Applications",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Votre annonce attire l'attention !",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt}, votre annonce de recrutement pour {team} attire des joueurs !",
            "total_label": "Total des candidatures",
            "applied_sub": "joueurs ont posé leur candidature pour rejoindre votre équipe",
            "message": "Ne laissez pas filer les talents, connectez-vous pour examiner les candidatures, présélectionner les meilleurs profils et inviter des joueurs à un essai.",
            "cta": "Examiner les candidatures",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "A sua publicação está a chamar a atenção!",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt}, a sua publicação de recrutamento para {team} está a atrair jogadores!",
            "total_label": "Total de candidaturas",
            "applied_sub": "jogadores candidataram-se para entrar na sua equipa",
            "message": "Não deixe o talento escapar, inicie sessão para analisar as candidaturas, selecionar os melhores candidatos e convidar jogadores para um teste.",
            "cta": "Analisar candidaturas",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: application rejected (to the player) ──
    "pm_application_rejected": {
        "en": {
            "header": "Application Update",
            "hi": "Hi {player},",
            "body": "Thank you for your interest in joining {team}. After careful consideration, we regret to inform you that your application was not successful at this time. We encourage you to keep honing your skills and consider applying again in the future.",
            "reason_label": "Reason",
            "keep_going_title": "Keep Going",
            "keep_going_body": "Every great player started somewhere. Keep practicing, stay active in the community, and your next opportunity could be just around the corner.",
            "cta": "Browse Other Teams",
            "footer": "We wish you the best of luck in your esports journey.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Mise à jour de la candidature",
            "hi": "Bonjour {player},",
            "body": "Merci de l'intérêt que vous portez à {team}. Après mûre réflexion, nous avons le regret de vous informer que votre candidature n'a pas été retenue cette fois-ci. Nous vous encourageons à continuer de perfectionner vos compétences et à envisager de postuler à nouveau à l'avenir.",
            "reason_label": "Raison",
            "keep_going_title": "Continuez",
            "keep_going_body": "Tous les grands joueurs ont commencé quelque part. Continuez à vous entraîner, restez actif dans la communauté, et votre prochaine opportunité pourrait être à portée de main.",
            "cta": "Parcourir d'autres équipes",
            "footer": "Nous vous souhaitons bonne chance dans votre parcours esport.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Atualização da candidatura",
            "hi": "Olá {player},",
            "body": "Obrigado pelo seu interesse em juntar-se a {team}. Após uma análise cuidadosa, lamentamos informar que a sua candidatura não foi bem-sucedida desta vez. Encorajamo-lo a continuar a aperfeiçoar as suas competências e a considerar candidatar-se novamente no futuro.",
            "reason_label": "Motivo",
            "keep_going_title": "Continue",
            "keep_going_body": "Todos os grandes jogadores começaram algures. Continue a treinar, mantenha-se ativo na comunidade, e a sua próxima oportunidade pode estar mesmo ao virar da esquina.",
            "cta": "Ver outras equipas",
            "footer": "Desejamos-lhe boa sorte no seu percurso no esport.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: trial started (to the player) ──
    "pm_trial_started_player": {
        "en": {
            "header": "Your Trial Has Begun!",
            "hey": "Hey {player}, {team} has selected you for a trial! A dedicated trial chat has been created where you can communicate directly with the team's management.",
            "team_label": "Team",
            "whatnext_title": "What happens next?",
            "whatnext_body": "Use the trial chat in the AFC app to coordinate with the team. This is your chance to impress, give it your all!",
            "cta": "Open Trial Chat",
            "footer": "This trial was started because you applied to {team} on the AFC Player Market.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Votre essai a commencé !",
            "hey": "Salut {player}, {team} vous a sélectionné pour un essai ! Un chat d'essai dédié a été créé où vous pouvez communiquer directement avec la direction de l'équipe.",
            "team_label": "Équipe",
            "whatnext_title": "Que se passe-t-il ensuite ?",
            "whatnext_body": "Utilisez le chat d'essai dans l'application AFC pour vous coordonner avec l'équipe. C'est votre chance de briller, donnez tout !",
            "cta": "Ouvrir le chat d'essai",
            "footer": "Cet essai a été lancé car vous avez postulé auprès de {team} sur le Player Market AFC.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "O seu teste começou!",
            "hey": "Olá {player}, {team} selecionou-o para um teste! Foi criado um chat de teste dedicado onde pode comunicar diretamente com a direção da equipa.",
            "team_label": "Equipa",
            "whatnext_title": "O que acontece a seguir?",
            "whatnext_body": "Utilize o chat de teste na aplicação AFC para se coordenar com a equipa. Esta é a sua oportunidade de impressionar, dê o seu melhor!",
            "cta": "Abrir chat de teste",
            "footer": "Este teste foi iniciado porque se candidatou a {team} no Player Market da AFC.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: trial started (to the team staff) ──
    "pm_trial_started_team": {
        "en": {
            "header": "Trial Started",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt},",
            "body": "{player} has been added to a trial with your team. A dedicated trial chat is now available to coordinate and evaluate their performance.",
            "player_label": "Player on Trial",
            "cta": "Open Trial Chat",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Essai lancé",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt},",
            "body": "{player} a été ajouté à un essai avec votre équipe. Un chat d'essai dédié est désormais disponible pour vous coordonner et évaluer sa performance.",
            "player_label": "Joueur à l'essai",
            "cta": "Ouvrir le chat d'essai",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Teste iniciado",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt},",
            "body": "{player} foi adicionado a um teste com a sua equipa. Está agora disponível um chat de teste dedicado para coordenar e avaliar o seu desempenho.",
            "player_label": "Jogador em teste",
            "cta": "Abrir chat de teste",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: direct trial invite (to the player) ──
    "pm_trial_invite": {
        "en": {
            "header": "A Team Wants You!",
            "hey": "Hey {player}, {team} saw your availability post and wants you on their roster for a trial!",
            "team_inviting": "Team Inviting You",
            "message_label": "Message",
            "window_title": "72-Hour Window",
            "window_body": "You must accept or decline within {hours}. After that, the invite expires.",
            "hours_text": "72 hours",
            "cta": "View & Respond to Invite",
            "footer": "This invite was sent because you have an active availability post on the AFC Player Market.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Une équipe vous veut !",
            "hey": "Salut {player}, {team} a vu votre annonce de disponibilité et vous veut dans son effectif pour un essai !",
            "team_inviting": "Équipe qui vous invite",
            "message_label": "Message",
            "window_title": "Fenêtre de 72 heures",
            "window_body": "Vous devez accepter ou refuser dans un délai de {hours}. Passé ce délai, l'invitation expire.",
            "hours_text": "72 heures",
            "cta": "Voir et répondre à l'invitation",
            "footer": "Cette invitation a été envoyée car vous avez une annonce de disponibilité active sur le Player Market AFC.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Uma equipa quer-te!",
            "hey": "Olá {player}, {team} viu a sua publicação de disponibilidade e quer-te no plantel para um teste!",
            "team_inviting": "Equipa que o convida",
            "message_label": "Mensagem",
            "window_title": "Janela de 72 horas",
            "window_body": "Deve aceitar ou recusar no prazo de {hours}. Após isso, o convite expira.",
            "hours_text": "72 horas",
            "cta": "Ver e responder ao convite",
            "footer": "Este convite foi enviado porque tem uma publicação de disponibilidade ativa no Player Market da AFC.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },

    # ── afc_player_market: direct trial invite accepted (to the team staff) ──
    "pm_trial_accepted_team": {
        "en": {
            "header": "Trial Accepted!",
            "mgmt": "{team} Management",
            "hi": "Hi {mgmt},",
            "body": "{player} has accepted your trial invite. A dedicated trial chat is now open.",
            "player_label": "Player on Trial",
            "cta": "Open Trial Chat",
            "footer_staff": "You received this because you are a staff member of {team}.",
            "rights": "© 2026 African Free Fire Community. All rights reserved.",
        },
        "fr": {
            "header": "Essai accepté !",
            "mgmt": "la direction de {team}",
            "hi": "Bonjour {mgmt},",
            "body": "{player} a accepté votre invitation à un essai. Un chat d'essai dédié est maintenant ouvert.",
            "player_label": "Joueur à l'essai",
            "cta": "Ouvrir le chat d'essai",
            "footer_staff": "Vous recevez ce message car vous êtes membre du staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Tous droits réservés.",
        },
        "pt": {
            "header": "Teste aceite!",
            "mgmt": "a direção de {team}",
            "hi": "Olá {mgmt},",
            "body": "{player} aceitou o seu convite para teste. Está agora aberto um chat de teste dedicado.",
            "player_label": "Jogador em teste",
            "cta": "Abrir chat de teste",
            "footer_staff": "Recebeu esta mensagem porque é membro do staff de {team}.",
            "rights": "© 2026 African Free Fire Community. Todos os direitos reservados.",
        },
    },
}
