"""
afc_support/notify.py - telling people something happened on their ticket.

THREE CHANNELS, AND WHY EACH IS HERE (owner 2026-09-14)
  1. The ACKNOWLEDGEMENT email, the moment a ticket is created: "let them get a reply in their email
     saying we have received it and will get back to them ... and a ticket number".
  2. The DISCORD DM, when we can match the writer to an AFC account with Discord connected: "if the
     person has discord, our discord bot should also send them a message". Email is exactly the
     channel that fails somebody locked out of the account tied to that address, which is why the
     acknowledgement also tells them to connect Discord and join the server.
  3. The REPLY email, when a human at AFC answers.

EVERYTHING HERE IS FAILURE-SAFE. A ticket is saved before any of this runs, and a dead SMTP server
or a Discord outage must never lose somebody's message or 500 the form. Every function returns a
bool and swallows its own errors; the caller records what was attempted on the ticket either way.

CONNECTS TO
  afc_auth.views.send_email (the single mail chokepoint) + afc_auth.email_i18n (the hand-written
  en/fr/pt copy, templates "support_received" and "support_reply"), afc_support.views (the callers),
  and settings.DISCORD_BOT_TOKEN for the DM.
"""
import os

import requests
from django.conf import settings

from afc_auth import outbox
from afc_auth.email_i18n import copy_for, subject_for
from afc_auth.views import SITE_URL, _email_shell, send_email

# How long we wait on Discord before giving up. A support form must answer the person quickly; the
# DM is a bonus channel, not a reason to hold the request open.
_DISCORD_TIMEOUT = 8


def ticket_url(ticket) -> str:
    """The address of a ticket's own page, which is the link in every email we send about it.

    Addressed by the opaque token, never by the row id (R22), because this link IS the credential
    for a requester who has no AFC account.
    """
    return f"{SITE_URL}/support/t/{ticket.public_token}"


def _button(href, label):
    """The one button style the AFC emails use, inline so mail clients keep it."""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" align="center"><tr>'
        f'<td align="center" bgcolor="#2fa35f" style="border-radius:8px;">'
        f'<a href="{href}" style="display:inline-block;padding:13px 26px;font-size:15px;'
        f'font-weight:600;color:#ffffff;text-decoration:none;">{label}</a>'
        f"</td></tr></table>"
    )


def _shell_rows(heading, paragraphs, href, cta, disclaimer):
    """The rows every support email shares: heading, some paragraphs, a button, a small note."""
    body = "".join(
        f'<div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">{p}</div>'
        for p in paragraphs
    )
    return f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{heading}</div>
    {body}
  </td></tr>
  <tr><td style="padding:24px 44px 8px;" align="center">{_button(href, cta)}</td></tr>
  <tr><td style="padding:18px 44px 8px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">{disclaimer}</div>
  </td></tr>"""


def email_ticket_received(ticket, lang="en") -> bool:
    """"We have your message." Sent once, when the ticket is created."""
    c = copy_for("support_received", lang)
    number = f'<span style="color:#e8efe9;font-weight:600;">{ticket.ticket_number}</span>'
    html = _email_shell(
        _shell_rows(
            c["heading"],
            [c["intro"].format(ticket=number), c["thread"], c["discord"]],
            ticket_url(ticket),
            c["cta"],
            c["disclaimer"],
        ),
        "green",
    )
    return send_email(
        ticket.email,
        subject_for("support_received", lang, ticket=ticket.ticket_number),
        html,
        language=lang,
        prelocalized=True,
    )


def email_ticket_reply(ticket, message, lang="en") -> bool:
    """"AFC replied." Sent when a human answers, with the reply quoted under the heading."""
    c = copy_for("support_reply", lang)
    number = f'<span style="color:#e8efe9;font-weight:600;">{ticket.ticket_number}</span>'
    # The reply itself, escaped by the caller's serializer path? No: escape HERE, because this is
    # staff-typed text going into an HTML email and the shell does not escape anything.
    from django.utils.html import escape as html_escape

    quoted = html_escape(message.body).replace("\n", "<br>")
    paragraphs = [
        c["intro"].format(ticket=number),
        f'<div style="margin-top:14px;color:#cfd8d2;">{quoted}</div>',
        c["thread"],
    ]
    html = _email_shell(
        _shell_rows(c["heading"], paragraphs, ticket_url(ticket), c["cta"], c["disclaimer"]),
        "green",
    )
    return send_email(
        ticket.email,
        subject_for("support_reply", lang, ticket=ticket.ticket_number),
        html,
        language=lang,
        prelocalized=True,
    )


def email_staff_new_ticket(ticket, message, attachment_count=0) -> bool:
    """Tell the support inbox a ticket landed, with the text and a link into the dashboard.

    The dashboard is the system of record now, but the inbox is where the team already looks, and a
    queue nobody opens is the failure this whole app exists to end. Reply-To is the person who
    wrote, so hitting Reply in the mail client still reaches them (it just will not be recorded on
    the ticket, which is why the mail says so).
    """
    from django.utils.html import escape as html_escape

    support_email = os.getenv("SUPPORT_EMAIL", "info@africanfreefirecommunity.com")
    dash = f"{SITE_URL}/a/support?ticket={ticket.ticket_number}"
    files_line = (f"<p><b>Attachments:</b> {attachment_count}</p>" if attachment_count else "")
    inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">New support ticket {ticket.ticket_number}</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">
      <p><b>From:</b> {html_escape(ticket.name)} ({html_escape(ticket.email)})</p>
      {files_line}
      <p>{html_escape(message.body).replace(chr(10), "<br>")}</p>
    </div>
  </td></tr>
  <tr><td style="padding:24px 44px 8px;" align="center">{_button(dash, "Open it in the dashboard")}</td></tr>
  <tr><td style="padding:18px 44px 8px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">Replying to this email reaches the
      person directly, but it is not recorded on the ticket. Answer in the dashboard so the whole
      conversation stays in one place.</div>
  </td></tr>"""
    return send_email(
        support_email,
        f"[{ticket.ticket_number}] {ticket.name}",
        _email_shell(inner, "green"),
        prelocalized=True,
        reply_to=ticket.email,
        from_name=f"{ticket.name} via AFC Support",
    )


def send_discord_dm(discord_id: str, content: str) -> bool:
    """Open a DM channel with `discord_id` and post one message. False on anything going wrong.

    Two calls, which is how Discord works: POST /users/@me/channels creates (or returns) the DM
    channel, then POST /channels/<id>/messages writes in it. A person who has DMs closed to
    non-friends makes the second call fail with 403, which is a normal outcome and not an error
    worth raising: they still have the email.
    """
    token = getattr(settings, "DISCORD_BOT_TOKEN", None)
    if not token or not discord_id:
        return False
    if not outbox.is_live():
        # The test runner and the scratch server: recorded, never posted (afc_auth.outbox says why).
        return outbox.record("discord_dm", str(discord_id), None, content)
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        channel = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": str(discord_id)},
            timeout=_DISCORD_TIMEOUT,
        )
        if channel.status_code not in (200, 201):
            return False
        channel_id = channel.json().get("id")
        if not channel_id:
            return False
        sent = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": content[:1900]},  # Discord caps a message at 2000 characters
            timeout=_DISCORD_TIMEOUT,
        )
        return sent.status_code in (200, 201)
    except requests.RequestException:
        return False
    except Exception:
        # Anything else (a JSON shape we did not expect) is still just "the DM did not go".
        return False


def dm_ticket_received(ticket) -> bool:
    """The Discord version of the acknowledgement. Plain text: a DM is not an HTML email."""
    if not ticket.discord_id:
        return False
    return send_discord_dm(
        ticket.discord_id,
        f"Hi {ticket.name}, AFC support has your message. Your ticket number is "
        f"{ticket.ticket_number} and somebody on the team will get back to you.\n"
        f"You can read the conversation and add anything you forgot here: {ticket_url(ticket)}",
    )


def dm_ticket_reply(ticket, message) -> bool:
    """The Discord version of "AFC replied"."""
    if not ticket.discord_id:
        return False
    return send_discord_dm(
        ticket.discord_id,
        f"AFC support replied to your message ({ticket.ticket_number}).\n\n"
        f"{message.body[:1200]}\n\n"
        f"Answer here: {ticket_url(ticket)}",
    )
