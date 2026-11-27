"""
afc_polls.kol_emails - emails to the KOL creators of a nomination round (owner, 27 Nov 2026).

TWO KINDS (afc_polls.models.KolEmail.kind)
    CODE  (#646: "add a mechanism to automatically send an email to the KOLs which we already have their mails ... send
          individually or ... selected ... or to send to all"): a creator's personal code, link and ready message,
          queued from the admin tab (kol_nominations.admin_emails) and sent on a background thread.
    PICKS (#648: "They should also be able to request to send their own votes to their mails"): a creator's own picks
          to the email they gave, sent at once from the ballot (kol_nominations.email_me).

HOW CODE EMAILS GO OUT
    queue_code_emails writes one QUEUED KolEmail per creator and starts ONE daemon thread for the batch, exactly as
    afc_auth.views.deliver_broadcast sends broadcast emails (SMTP is slow; an admin pressing "Email everyone" must not
    hold a request open for minutes). The thread sends one message every BROADCAST_EMAIL_PACE_SECONDS and tries a
    refused one again after each of BROADCAST_EMAIL_RETRY_WAITS: the broadcast sender's OWN constants, imported, not
    copied, because Microsoft 365 cut 31 of 55 room emails on 12 Nov 2026 when nothing waited (afc_auth.views, inbox
    #557). Not Celery: the deploy restarts the API, not the Celery worker (23 Nov 2026), so a new task would wait for a
    worker that does not know it. A thread cut short by a restart leaves rows QUEUED; the tab shows a queued row older
    than STALE_AFTER as not sent, and sending again is always safe (a creator already queued is never queued twice).

    The code itself is never stored here: the thread reads the creator's live code back from its sealed copy
    (kol_nominations.unseal_code) when the message goes out, so a code replaced in between is never sent.

    From info@ (the one mailbox the server sends from) as "African Freefire Community" (owner's answer, 27 Nov), in the
    creator's language from the hand-written catalog (kol_messages, prelocalized=True: never machine-translated).

CONNECTS TO: afc_auth.views.send_email (the one email chokepoint; under tests and on the scratch server it writes to
afc_auth.outbox instead of sending), afc_polls.kol_messages (subjects and bodies), afc_polls.kol_nominations (codes,
links, ballots), frontend app/(a)/a/polls/_components/KolNominationsAdmin.tsx and app/(user)/awards/_kol/KolNominate.tsx.
"""
import threading
import time
from datetime import timedelta

from django.db import close_old_connections, connection
from django.utils import timezone

from .models import KolCreator, KolEmail, KolVoterCode

FROM_NAME = "African Freefire Community"
# A row still QUEUED after this long was cut short (a restart mid-batch): the tab shows it as not sent.
STALE_AFTER = timedelta(minutes=30)
# #648: a creator may email themselves their picks this many times in PICKS_WINDOW (R59; it is a real email each).
PICKS_PER_WINDOW = 3
PICKS_WINDOW = timedelta(minutes=10)


def _pace():
    """(pace seconds, retry waits): the broadcast sender's own numbers."""
    from afc_auth.views import BROADCAST_EMAIL_PACE_SECONDS, BROADCAST_EMAIL_RETRY_WAITS

    return BROADCAST_EMAIL_PACE_SECONDS, tuple(BROADCAST_EMAIL_RETRY_WAITS)


def _send(to_address, subject, html_body, language):
    from afc_auth.views import send_email

    return bool(send_email(to_address, subject, html_body, language=language, prelocalized=True,
                           from_name=FROM_NAME))


def queued_now(creator_ids):
    """The creators among `creator_ids` with a CODE email still on its way (queued, not stale)."""
    return set(
        KolEmail.objects.filter(creator_id__in=list(creator_ids), kind=KolEmail.CODE, status=KolEmail.QUEUED,
                                queued_at__gt=timezone.now() - STALE_AFTER)
        .values_list("creator_id", flat=True)
    )


def queue_code_emails(creators, admin_user):
    """Queue a CODE email to each of `creators` (they all have an address and a live readable code; the caller,
    kol_nominations.admin_emails, saw to that) and start the sender. Returns the KolEmail rows."""
    rows = [KolEmail.objects.create(creator=creator, kind=KolEmail.CODE, to_address=creator.email,
                                    sent_by=admin_user) for creator in creators]
    ids = [row.pk for row in rows]
    if ids:
        start_sender(ids)
    return rows


def start_sender(email_ids):
    """Send `email_ids` on a daemon thread. The tests replace this to send inline with no waits."""
    threading.Thread(target=_send_in_thread, args=(email_ids,), daemon=True).start()


def _send_in_thread(email_ids):
    """The thread's body: its own database connection, closed at the end (a thread's connection is never closed by
    Django's request cycle)."""
    try:
        close_old_connections()
        send_code_emails(email_ids)
    except Exception as error:                     # a thread's exception is otherwise lost without a trace
        print(f"KOL CODE EMAILS stopped: {type(error).__name__}", flush=True)
    finally:
        connection.close()


def _finish(row, ok, error=""):
    row.status = KolEmail.SENT if ok else KolEmail.FAILED
    row.error = "" if ok else error
    row.finished_at = timezone.now()
    row.save(update_fields=["status", "error", "finished_at"])


def send_code_emails(email_ids, *, sleep=None, send=None):
    """Send the QUEUED code emails `email_ids`, paced and retried; each row ends SENT or FAILED (with a code).
    Returns (sent, failed). `sleep` and `send` are for the tests."""
    from .kol_messages import code_email
    from .kol_nominations import ballot_link, unseal_code
    from .models import KolNominationRound

    sleep = sleep or time.sleep
    send = send or _send
    pace, waits = _pace()
    sent = failed = 0
    rows = list(KolEmail.objects.filter(pk__in=list(email_ids), status=KolEmail.QUEUED)
                .select_related("creator", "creator__edition").order_by("email_id"))
    for i, row in enumerate(rows):
        if i:
            sleep(pace)
        creator = row.creator
        code = (KolVoterCode.objects.filter(creator=creator, revoked_at__isnull=True)
                .order_by("-created_at", "-code_id").first())
        plain = unseal_code(code.code_sealed) if code is not None else ""
        if not creator.active or not plain:
            _finish(row, False, "kol_code_gone")    # switched off, or the code was revoked since it was queued
            failed += 1
            continue
        round_ = KolNominationRound.objects.filter(edition=creator.edition).first()
        try:
            subject, body, language = code_email(creator, plain, ballot_link(creator.edition, plain),
                                                 round_.closes_at if round_ is not None else None,
                                                 creator.edition.title)
        except Exception:
            _finish(row, False, "kol_email_build_failed")
            failed += 1
            continue
        ok = False
        for wait in (0,) + waits:
            if wait:
                sleep(wait)
            try:
                ok = bool(send(row.to_address, subject, body, language))
            except Exception:
                ok = False
            if ok:
                break
        _finish(row, ok, "kol_email_send_failed")
        sent += ok
        failed += (not ok)
    print(f"KOL CODE EMAILS: {sent} sent, {failed} failed of {len(rows)}", flush=True)
    return sent, failed


def picks_limited(creator):
    """True when `creator` has asked for their picks PICKS_PER_WINDOW times in the last PICKS_WINDOW."""
    return KolEmail.objects.filter(creator=creator, kind=KolEmail.PICKS,
                                   queued_at__gt=timezone.now() - PICKS_WINDOW).count() >= PICKS_PER_WINDOW


def email_picks(round_, creator, picks, *, send=None):
    """Email `creator` their own `picks` ({question_id: [creator_id]}, as ballot_payload answers them) to the address
    they gave, now (one message: no thread). Returns the KolEmail row, SENT or FAILED."""
    from .kol_messages import picks_email

    send = send or _send
    names = {c.pk: (c.name, c.tiktok_handle) for c in KolCreator.objects.filter(edition_id=round_.edition_id)}
    awards = [
        (question.prompt, [names[cid] for cid in picks.get(str(question.question_id), []) if cid in names])
        for question in round_.poll.questions.order_by("order")
    ]
    row = KolEmail.objects.create(creator=creator, kind=KolEmail.PICKS, to_address=creator.email)
    subject, body, language = picks_email(creator, round_.edition.title, awards)
    try:
        ok = bool(send(creator.email, subject, body, language))
    except Exception:
        ok = False
    _finish(row, ok, "kol_email_send_failed")
    return row


def last_code_emails(creator_ids):
    """{creator_id: {status, at, error}} of each creator's latest CODE email, for the admin tab. A QUEUED one older
    than STALE_AFTER reads as failed (kol_email_interrupted): its thread was cut short."""
    latest = {}
    for row in (KolEmail.objects.filter(creator_id__in=list(creator_ids), kind=KolEmail.CODE)
                .order_by("creator_id", "-queued_at", "-email_id")):
        if row.creator_id in latest:
            continue
        status, error = row.status, row.error
        if status == KolEmail.QUEUED and row.queued_at <= timezone.now() - STALE_AFTER:
            status, error = KolEmail.FAILED, "kol_email_interrupted"
        latest[row.creator_id] = {"status": status, "at": row.finished_at or row.queued_at, "error": error,
                                  "to": row.to_address}
    return latest
