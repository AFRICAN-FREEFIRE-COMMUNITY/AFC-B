"""
afc_polls.kol_metrics - the KOL nomination round's metrics (inbox #653, owner 27 Nov 2026).

THE ASK
    Owner, on the Metrics tab showing zeros for the round: "the metric page shows no information and i filled
    something. the metrics page should show very very detailed info, who was most searched (how many times), average
    time to nominate, stuff like that a lot of metrics and all very detailed." The mockup the owner approved
    (WEBSITE/mockups/kol-metrics/, "ship it, then build the metrics") is what metrics() returns, section by section.

WHAT IS RECORDED HERE (the saved picks cannot tell these afterwards; model KolBallotEvent)
    - record_open: a ballot opened with a code (kol_nominations.door and the cookie GET of kol_nominations.ballot),
      at most one row per code per OPEN_GAP, so one visit is one row however often the page reloads.
    - record_save: after every save (kol_nominations._save): a CHANGED row with how many picks the save swapped or
      removed, and the first COMPLETE row once the ballot counts.
    - search_log (POST polls/kol-nominations/<edition>/search/, kol_ballot cookie): the ballot page reports a search
      when it ends: what was typed, how many names came up, who was picked from the results (or nobody).
    Recording never breaks the ballot: every write is wrapped, a failure is logged and the creator carries on.

WHAT IS READ (metrics, for GET polls/admin/kol-nominations/<edition>/metrics/)
    The round's own tables: PollResponse (saves, device type, connection country), PollAnswer (picks and when each
    was first made), PollPickSwitch (every change, poll-wide), KolVoterCode (opened, spare, revoked), KolEmail (code
    and picks emails), KolCreator (contact given, write-ins, who added them), plus KolBallotEvent above. Ballots that
    count are counted_response_ids, the same rule as the tally and Garena's live page.

    Ballots finished BEFORE recording started (28 Nov 2026) have no OPENED or COMPLETE row: their finish time is
    rebuilt from the picks (the moment the last award got somebody other than the voter) and their start is their
    first save. `time.from_first_save` says how many were measured that way, and the tab says so.

CONSUMED BY: frontend app/(a)/a/polls/_components/KolNominationsMetrics.tsx (Metrics tab, "Nominations round"), and
the ballot page app/(user)/awards/[edition]/nominate (search_log). Auth for metrics: the same as every KOL admin
address (kol_nominations._admin_edition: an AFC poll admin, or a KOL Admin for an edition with a round).
"""
import logging
import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from .models import (
    KolBallotEvent, KolCreator, KolCreatorOption, KolEmail, KolVoterCode, PollAnswer, PollPickSwitch, PollResponse,
)

log = logging.getLogger(__name__)

OPEN_GAP = timedelta(minutes=30)          # one visit = one OPENED row, however often the page reloads within this
SEARCHES_PER_MINUTE = 30                  # a person types a handful a minute; more is a script (R59)
QUERY_MAX = 60
ONE_VISIT = timedelta(hours=1)            # counted "in one visit" when it counted within an hour of opening
NOTHING_THEN_WINDOW = timedelta(minutes=10)
TIME_BINS = [                             # (upper bound in seconds or None, key the tab translates)
    (120, "under_2m"), (300, "2_to_5m"), (600, "5_to_10m"), (1800, "10_to_30m"), (86400, "30m_to_1d"),
    (None, "over_1d"),
]


# ── recording ─────────────────────────────────────────────────────────────────────────────────────────────────

def _device(request):
    from .vote_tracking import device_type

    try:
        return device_type(request.META.get("HTTP_USER_AGENT")) if request is not None else ""
    except Exception:
        return ""


def record_open(round_, code, request):
    """An OPENED row for `code`, unless it already has one within OPEN_GAP. Called by kol_nominations.door and the
    GET of kol_nominations.ballot."""
    try:
        now = timezone.now()
        if KolBallotEvent.objects.filter(code=code, kind=KolBallotEvent.OPENED, at__gt=now - OPEN_GAP).exists():
            return
        KolBallotEvent.objects.create(edition_id=round_.edition_id, code=code, creator_id=code.creator_id,
                                      kind=KolBallotEvent.OPENED, device_type=_device(request), at=now)
    except Exception:
        log.exception("KOL metrics: could not record an open")


def record_save(round_, code, changed, complete, request):
    """After a save: a CHANGED row carrying how many picks it swapped or removed (`changed`, from
    kol_nominations._write_ballot), and the creator's first COMPLETE row once the ballot counts (`complete`, the
    ballot payload's own flag)."""
    try:
        now = timezone.now()
        if changed:
            KolBallotEvent.objects.create(edition_id=round_.edition_id, code=code, creator_id=code.creator_id,
                                          kind=KolBallotEvent.CHANGED, result_count=changed,
                                          device_type=_device(request), at=now)
        if complete and not KolBallotEvent.objects.filter(edition_id=round_.edition_id, creator_id=code.creator_id,
                                                          kind=KolBallotEvent.COMPLETE).exists():
            KolBallotEvent.objects.create(edition_id=round_.edition_id, code=code, creator_id=code.creator_id,
                                          kind=KolBallotEvent.COMPLETE, device_type=_device(request), at=now)
    except Exception:
        log.exception("KOL metrics: could not record a save")


@api_view(["POST"])
@authentication_classes([])
def search_log(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/search/  (kol_ballot cookie)
    {question_id, query, result_count, picked_id | null}

    The ballot page reports one search when it ends: picked from the results (picked_id) or left without a pick
    (picked_id null). 200 {logged: true}; 400 kol_bad_search; 401 kol_code_needed / kol_code_replaced; 404
    kol_round_not_found; 429 kol_search_too_many (SEARCHES_PER_MINUTE a code). Consumed by the nominate page."""
    from .kol_nominations import _minute_limited, _no_round, _refuse, code_from_cookie, round_for_edition_slug

    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    if _minute_limited("kol_search", code, SEARCHES_PER_MINUTE):
        return _refuse("Searching too fast. Wait a moment.", "kol_search_too_many", status.HTTP_429_TOO_MANY_REQUESTS)
    data = request.data if isinstance(request.data, dict) else {}
    query = data.get("query")
    question_id = data.get("question_id")
    result_count = data.get("result_count")
    picked_id = data.get("picked_id")
    bad = _refuse("That search could not be read.", "kol_bad_search")
    # Types first (R69): a bool is an int in Python, so it is refused by name.
    if not isinstance(query, str) or isinstance(question_id, bool) or not isinstance(question_id, int):
        return bad
    if isinstance(result_count, bool) or not isinstance(result_count, int) or not 0 <= result_count <= 10000:
        return bad
    if picked_id is not None and (isinstance(picked_id, bool) or not isinstance(picked_id, int)):
        return bad
    query = " ".join(query.split()).lower()[:QUERY_MAX]
    if not query:
        return bad
    if not round_.poll.questions.filter(question_id=question_id).exists():
        return bad
    if picked_id is not None and not KolCreator.objects.filter(pk=picked_id, edition_id=round_.edition_id).exists():
        return bad
    try:
        KolBallotEvent.objects.create(edition_id=round_.edition_id, code=code, creator_id=code.creator_id,
                                      kind=KolBallotEvent.SEARCH, question_id=question_id, query=query,
                                      result_count=result_count, picked_id=picked_id, device_type=_device(request))
    except Exception:
        log.exception("KOL metrics: could not record a search")
    return Response({"logged": True}, status=status.HTTP_200_OK)


# ── reading ───────────────────────────────────────────────────────────────────────────────────────────────────

def _seconds(delta):
    return max(0, int(delta.total_seconds()))


def _median(values):
    return int(statistics.median(values)) if values else None


def _mean(values):
    return int(sum(values) / len(values)) if values else None


def _bin(seconds):
    for upper, key in TIME_BINS:
        if upper is None or seconds < upper:
            return key
    return TIME_BINS[-1][1]


# The copied reminder for a creator whose ballot does not count only because an award holds nobody but themselves
# (#652). Same rules as the KOL messages (afc_polls.kol_messages): names the edition, never AFC, the creator's
# language from the sheet's country, hand-written.
REMINDER = {
    "en": ("Hi {name}, in {n} of the awards of the {title} nominations you picked only yourself. An award with only "
           "yourself counts as empty, so your nominations do not count yet. Open your ballot with your personal link "
           "or code and add at least one other creator there."),
    "fr": ("Bonjour {name}, dans {n} des prix des nominations des {title}, vous n'avez choisi que vous-même. Un prix "
           "avec seulement vous-même compte comme vide : vos nominations ne comptent donc pas encore. Ouvrez votre "
           "bulletin avec votre lien ou votre code personnel et ajoutez-y au moins un autre créateur."),
    "pt": ("Olá {name}, em {n} dos prémios das nomeações dos {title} escolheu apenas a si próprio. Um prémio só "
           "consigo próprio conta como vazio, por isso as suas nomeações ainda não contam. Abra o seu boletim com o "
           "seu link ou código pessoal e acrescente lá pelo menos outro criador."),
}


def reminder_for(creator, awards_only_self, edition_title):
    from .kol_messages import event_name, language_for

    return REMINDER[language_for(creator.country)].format(name=creator.name, n=awards_only_self,
                                                          title=event_name(edition_title))


def metrics(round_):
    """Everything the Metrics tab shows for the round, read now. See the module docstring for the sources."""
    from afc_auth.country_grouping import country_code
    from .kol_nominations import counted_response_ids, mask_email, respondent_key_for
    from .vote_tracking import COUNTED_ANSWER

    edition = round_.edition
    poll = round_.poll
    now = timezone.now()
    questions = list(poll.questions.order_by("order"))
    question_ids = [q.question_id for q in questions]

    everyone = {c.pk: c for c in KolCreator.objects.filter(edition=edition).select_related("added_by")}
    voters = {pk: c for pk, c in everyone.items() if c.origin == KolCreator.LIST and c.active}
    voter_by_key = {respondent_key_for(round_, c): c for c in voters.values()}

    # ── ballots and picks ──
    responses = {r.response_id: r for r in PollResponse.objects.filter(poll=poll, status=PollResponse.SUBMITTED)}
    voter_of_response = {rid: voter_by_key[r.respondent_key] for rid, r in responses.items()
                         if r.respondent_key in voter_by_key}
    picks = defaultdict(lambda: defaultdict(list))        # response_id -> question_id -> [(creator_id, created_at)]
    for rid, qid, cid, at in (PollAnswer.objects.filter(response__poll=poll, response__status=PollResponse.SUBMITTED,
                                                        option__kol_creator_link__creator__active=True)
                              .filter(COUNTED_ANSWER)
                              .values_list("response_id", "question_id", "option__kol_creator_link__creator_id",
                                           "created_at")):
        if rid in voter_of_response:
            picks[rid][qid].append((cid, at))
    started = [rid for rid in voter_of_response if any(picks[rid].values())]
    counted = counted_response_ids(round_) & set(voter_of_response)
    response_of_voter = {voter_of_response[rid].pk: rid for rid in voter_of_response}

    # ── events ──
    events = list(KolBallotEvent.objects.filter(edition=edition).select_related("code").order_by("at", "event_id"))

    def who(event):
        return event.creator_id or (event.code.creator_id if event.code_id and event.code else None)

    opens = defaultdict(list)
    complete_event = {}
    changes = defaultdict(int)
    searches = []
    for e in events:
        person = who(e)
        if e.kind == KolBallotEvent.OPENED and person:
            opens[person].append(e.at)
        elif e.kind == KolBallotEvent.COMPLETE and person and person not in complete_event:
            complete_event[person] = e.at
        elif e.kind == KolBallotEvent.CHANGED and person:
            changes[person] += e.result_count or 0
        elif e.kind == KolBallotEvent.SEARCH:
            searches.append((e, person))

    # ── codes ──
    codes = list(KolVoterCode.objects.filter(edition=edition))
    opened_voters = {c.creator_id for c in codes if c.creator_id in voters and c.last_used_at} | (
        set(opens) & set(voters))
    spare = [c for c in codes if c.spare]

    # ── emails ──
    emails = list(KolEmail.objects.filter(creator__edition=edition).order_by("queued_at", "email_id"))
    code_emails = [m for m in emails if m.kind == KolEmail.CODE]
    latest_code_email = {}
    for m in code_emails:
        latest_code_email[m.creator_id] = m
    emailed = {m.creator_id for m in code_emails if m.status == KolEmail.SENT}

    # ── time to nominate ──
    per_creator_time = {}
    rows_time = []
    for rid in counted:
        voter = voter_of_response[rid]
        complete_at = complete_event.get(voter.pk)
        if complete_at is None:
            # Rebuilt from the picks: the moment the last award got somebody other than the voter.
            firsts = []
            for qid in question_ids:
                others = [at for cid, at in picks[rid].get(qid, []) if cid != voter.pk]
                if others:
                    firsts.append(min(others))
            complete_at = max(firsts) if len(firsts) == len(question_ids) else responses[rid].submitted_at
        first_opens = [at for at in opens.get(voter.pk, []) if at <= complete_at]
        from_open = bool(first_opens)
        start = min(first_opens) if first_opens else responses[rid].started_at
        all_pick_times = [at for rows in picks[rid].values() for _cid, at in rows]
        first_pick = min(all_pick_times) if all_pick_times else start
        seconds = _seconds(complete_at - start)
        per_creator_time[voter.pk] = {"start": start, "complete": complete_at, "seconds": seconds,
                                      "from_open": from_open, "first_pick": first_pick}
        rows_time.append(per_creator_time[voter.pk])
    durations = [r["seconds"] for r in rows_time]
    open_to_pick = [_seconds(r["first_pick"] - r["start"]) for r in rows_time if r["from_open"]]
    pick_to_count = [_seconds(r["complete"] - r["first_pick"]) for r in rows_time]
    bins = Counter(_bin(s) for s in durations)
    visits = [len(v) for pk, v in opens.items() if pk in voters]

    # ── picks by creator, in all saved ballots and in the ones that count ──
    picks_all = Counter()
    picks_counted = defaultdict(Counter)                  # question_id -> creator_id -> picks in counting ballots
    for rid in started:
        for qid, rows in picks[rid].items():
            for cid, _at in rows:
                picks_all[cid] += 1
                if rid in counted:
                    picks_counted[qid][cid] += 1

    def person(pk):
        c = everyone.get(pk)
        return {"creator_id": pk, "name": c.name if c else "", "handle": c.tiktok_handle if c else ""}

    # ── searches ──
    def searcher(entry):
        e, p = entry
        return p or f"code:{e.code_id}"

    ended_on = defaultdict(list)
    for entry in searches:
        if entry[0].picked_id:
            ended_on[entry[0].picked_id].append(entry)
    most_searched = sorted(
        ({**person(pk), "searches": len(rows), "by": len({searcher(r) for r in rows}), "picks": picks_all[pk]}
         for pk, rows in ended_on.items()),
        key=lambda row: (-row["searches"], -row["by"], row["name"].lower()))
    write_ins_by = defaultdict(list)
    for c in everyone.values():
        if c.origin == KolCreator.WRITE_IN and c.added_by_id:
            write_ins_by[c.added_by_id].append(c)
    nothing = defaultdict(lambda: {"times": 0, "by": set(), "then": Counter()})
    for i, entry in enumerate(searches):
        e, p = entry
        if e.result_count != 0:
            continue
        row = nothing[e.query]
        row["times"] += 1
        row["by"].add(searcher(entry))
        then = "nothing"
        for later, later_p in searches[i + 1:]:
            if later.at - e.at > NOTHING_THEN_WINDOW:
                break
            if searcher((later, later_p)) == searcher(entry) and later.picked_id:
                then = f"picked:{everyone[later.picked_id].name}" if later.picked_id in everyone else "picked:"
                break
        if then == "nothing" and p:
            added = [c for c in write_ins_by.get(p, []) if timedelta(0) <= c.created_at - e.at <= NOTHING_THEN_WINDOW]
            if added:
                then = f"added:{added[0].name}"
        row["then"][then] += 1
    found_nothing = sorted(
        ({"query": q, "times": r["times"], "by": len(r["by"]), "then": r["then"].most_common(1)[0][0]}
         for q, r in nothing.items()),
        key=lambda row: (-row["times"], row["query"]))[:50]
    typed = defaultdict(Counter)
    for e, _p in searches:
        if e.picked_id:
            typed[e.query][e.picked_id] += 1
    most_typed = sorted(
        ({"query": q, "times": sum(c.values()), "top": {**person(c.most_common(1)[0][0]),
                                                       "times": c.most_common(1)[0][1]}}
         for q, c in typed.items()),
        key=lambda row: (-row["times"], row["query"]))[:30]

    # ── by award ──
    awards = []
    for q in questions:
        qid = q.question_id
        counts, dist = [], [0] * 6
        with_pick = only_self = picked_self = write_in_picks = 0
        nominees = set()
        for rid in started:
            voter = voter_of_response[rid]
            ids = [cid for cid, _at in picks[rid].get(qid, [])]
            if not ids:
                continue
            with_pick += 1
            counts.append(len(ids))
            dist[min(len(ids), 6) - 1] += 1
            nominees.update(ids)
            if voter.pk in ids:
                picked_self += 1
                if all(cid == voter.pk for cid in ids):
                    only_self += 1
            write_in_picks += sum(1 for cid in ids if cid in everyone and everyone[cid].origin == KolCreator.WRITE_IN)
        top = picks_counted[qid].most_common(1)
        awards.append({
            "question_id": qid, "name": q.prompt, "with_pick": with_pick, "empty": len(started) - with_pick,
            "only_self": only_self, "avg_picks": round(sum(counts) / len(counts), 1) if counts else 0,
            "distribution": dist, "picked_self": picked_self, "write_in_picks": write_in_picks,
            "nominees": len(nominees), "top": {**person(top[0][0]), "picks": top[0][1]} if top else None,
        })

    # ── activity ──
    by_day = Counter(r["complete"].date().isoformat() for r in rows_time)
    first_day = (round_.opens_at or (min((r["complete"] for r in rows_time), default=now))).date()
    last_day = max(first_day, min(round_.closes_at or now, now).date())
    days = []
    day = first_day
    while day <= last_day and len(days) < 60:
        days.append({"day": day.isoformat(), "counted": by_day.get(day.isoformat(), 0)})
        day += timedelta(days=1)
    by_hour = [0] * 24
    for rid in started:
        for rows in picks[rid].values():
            for _cid, at in rows:
                by_hour[at.hour] += 1

    # ── changes ──
    switches = list(PollPickSwitch.objects.filter(poll=poll).values_list("question_id", "from_option_id", "at"))
    option_creator = dict(KolCreatorOption.objects.filter(option__question__poll=poll).values_list("option_id",
                                                                                                  "creator_id"))
    removed = Counter(option_creator.get(from_id) for _qid, from_id, _at in switches if from_id)
    removed.pop(None, None)
    by_award_changes = Counter(qid for qid, _f, _at in switches)
    most_changed = by_award_changes.most_common(1)
    changed_after = sum(1 for pk, r in per_creator_time.items()
                        if any(e.kind == KolBallotEvent.CHANGED and who(e) == pk and e.at > r["complete"]
                               for e in events))
    picks_emails = [m for m in emails if m.kind == KolEmail.PICKS]

    # ── self-nominations ──
    self_pickers = set()
    only_self_rows = []
    for rid in started:
        voter = voter_of_response[rid]
        n_only = 0
        for qid in question_ids:
            ids = [cid for cid, _at in picks[rid].get(qid, [])]
            if voter.pk in ids:
                self_pickers.add(voter.pk)
                if all(cid == voter.pk for cid in ids):
                    n_only += 1
        if n_only and rid not in counted:
            only_self_rows.append({**person(voter.pk), "awards_only_self": n_only,
                                   "saved_at": responses[rid].submitted_at,
                                   "reminder": reminder_for(voter, n_only, edition.title)})
    only_self_rows.sort(key=lambda row: (-row["awards_only_self"], row["name"].lower()))
    write_ins = sorted(
        ({**person(c.pk), "added_by": person(c.added_by_id) if c.added_by_id else None, "picks": picks_all[c.pk]}
         for c in everyone.values() if c.origin == KolCreator.WRITE_IN and c.active),
        key=lambda row: (-row["picks"], row["name"].lower()))

    # ── devices and countries ──
    devices = Counter((responses[rid].device_type or "unknown") for rid in started)
    by_country = defaultdict(lambda: {"ballots": 0, "counting": 0})
    mismatch = 0
    for rid in started:
        r = responses[rid]
        key = r.ip_country or ""
        by_country[key]["ballots"] += 1
        by_country[key]["counting"] += rid in counted
        sheet = country_code(voter_of_response[rid].country) if voter_of_response[rid].country else ""
        if r.ip_country and sheet and sheet != r.ip_country:
            mismatch += 1
    on_list = Counter(country_code(c.country) or "" for c in voters.values())
    countries = sorted(
        ({"country": code, "ballots": v["ballots"], "counting": v["counting"], "on_list": on_list.get(code, 0)}
         for code, v in by_country.items()),
        key=lambda row: (-row["ballots"], row["country"]))

    # ── codes never opened, emails ──
    never_opened = []
    for c in sorted(voters.values(), key=lambda c: c.name.lower()):
        if c.pk in opened_voters:
            continue
        m = latest_code_email.get(c.pk)
        never_opened.append({**person(c.pk), "has_email": bool(c.email), "contact_given": bool(c.contact_given_at),
                             "email_status": m.status if m else ("" if c.email else "no_address"),
                             "emailed_at": (m.finished_at or m.queued_at) if m else None})
    email_to_open = []
    opened_after_email = 0
    for pk in emailed:
        sent_at = max((m.finished_at or m.queued_at) for m in code_emails
                      if m.creator_id == pk and m.status == KolEmail.SENT)
        after = [at for at in opens.get(pk, []) if at >= sent_at]
        code_last = max((c.last_used_at for c in codes if c.creator_id == pk and c.last_used_at), default=None)
        if after or (code_last and code_last >= sent_at):
            opened_after_email += 1
        if after:
            email_to_open.append(_seconds(min(after) - sent_at))
    failed = [{**person(pk), "address": mask_email(m.to_address), "error": m.error, "at": m.finished_at or m.queued_at}
              for pk, m in latest_code_email.items() if m.status == KolEmail.FAILED]

    # ── one row per creator (the CSV) ──
    per_creator = []
    for c in sorted(voters.values(), key=lambda c: c.name.lower()):
        rid = response_of_voter.get(c.pk)
        t = per_creator_time.get(c.pk)
        per_creator.append({
            **person(c.pk), "country": c.country, "has_email": bool(c.email),
            "contact_given": bool(c.contact_given_at), "emailed": c.pk in emailed,
            "opened": c.pk in opened_voters, "first_open": min(opens[c.pk]) if opens.get(c.pk) else None,
            "visits": len(opens.get(c.pk, [])), "searches": sum(1 for e, p in searches if p == c.pk),
            "awards_with_pick": sum(1 for qid in question_ids if picks[rid].get(qid)) if rid else 0,
            "counts": rid in counted if rid else False, "counted_at": t["complete"] if t else None,
            "seconds_to_count": t["seconds"] if t else None, "changes": changes.get(c.pk, 0),
            "ballot_counts_switch": c.ballot_counts,
        })

    return {
        "round": {"opens_at": round_.opens_at, "closes_at": round_.closes_at, "state": round_.state(),
                  "recording_since": min((e.at for e in events), default=None)},
        "headline": {
            "voters": len(voters), "counting": len(counted), "started_not_counting": len(started) - len(counted),
            "codes_opened": len(opened_voters), "never_opened": len(voters) - len(opened_voters),
            "median_seconds": _median(durations), "average_seconds": _mean(durations),
            "searches": len(searches), "searchers": len({searcher(s) for s in searches}),
            "searches_nothing": sum(1 for e, _p in searches if e.result_count == 0),
            "code_emails_sent": sum(1 for m in code_emails if m.status == KolEmail.SENT),
            "code_emails_failed": len(failed),
            "no_address": sum(1 for c in voters.values() if not c.email),
        },
        "funnel": [
            {"key": "in_round", "count": len(voters)},
            {"key": "emailed", "count": len(emailed & set(voters))},
            {"key": "opened", "count": len(opened_voters)},
            {"key": "contact", "count": sum(1 for c in voters.values() if c.contact_given_at)},
            {"key": "picked", "count": len(started)},
            {"key": "counting", "count": len(counted)},
        ],
        "time": {
            "ballots": len(durations), "from_first_save": sum(1 for r in rows_time if not r["from_open"]),
            "bins": [{"key": key, "count": bins.get(key, 0)} for _upper, key in TIME_BINS],
            "median_seconds": _median(durations), "average_seconds": _mean(durations),
            "fastest_seconds": min(durations) if durations else None,
            "longest_seconds": max(durations) if durations else None,
            "open_to_first_pick_median": _median(open_to_pick),
            "first_pick_to_counting_median": _median(pick_to_count),
            "one_visit": sum(1 for s in durations if s <= ONE_VISIT.total_seconds()),
            "came_back": sum(1 for s in durations if s > ONE_VISIT.total_seconds()),
            "visits_average": round(sum(visits) / len(visits), 1) if visits else None,
        },
        "searches": {"most_searched": most_searched, "found_nothing": found_nothing, "most_typed": most_typed},
        "awards": awards,
        "activity": {"days": days, "by_hour": by_hour,
                     "peak_hour": max(range(24), key=lambda h: by_hour[h]) if any(by_hour) else None},
        "changes": {
            "total": len(switches), "after_counting": changed_after,
            "per_ballot_average": round(sum(changes.values()) / len(changes), 1) if changes else None,
            "most_changed_award": ({"name": next(q.prompt for q in questions if q.question_id == most_changed[0][0]),
                                    "count": most_changed[0][1]} if most_changed else None),
            "most_removed": [{**person(pk), "removed": n, "picks": picks_all[pk]} for pk, n in removed.most_common(10)],
            "picks_emails": sum(1 for m in picks_emails if m.status == KolEmail.SENT),
            "picks_emails_by": len({m.creator_id for m in picks_emails if m.status == KolEmail.SENT}),
            "contact_given": sum(1 for c in voters.values() if c.contact_given_at),
        },
        "self": {"picked_self": len(self_pickers), "only_self": only_self_rows, "write_ins": write_ins},
        "devices": [{"key": k, "count": n} for k, n in devices.most_common()],
        "countries": countries, "country_mismatch": mismatch,
        "codes": {
            "creator_codes": len({c.creator_id for c in codes if not c.spare and c.creator_id}),
            "opened": len(opened_voters), "never_opened_count": len(never_opened), "never_opened": never_opened,
            "spare_made": len(spare), "spare_claimed": sum(1 for c in spare if c.creator_id),
            "revoked": sum(1 for c in codes if c.revoked_at),
        },
        "emails": {
            "code_sent": sum(1 for m in code_emails if m.status == KolEmail.SENT), "code_failed": len(failed),
            "no_address": sum(1 for c in voters.values() if not c.email), "emailed_creators": len(emailed),
            "opened_after_email": opened_after_email, "email_to_open_median": _median(email_to_open),
            "picks_sent": sum(1 for m in picks_emails if m.status == KolEmail.SENT), "failed": failed,
        },
        "creators": per_creator,
        "generated_at": now,
    }


@api_view(["GET"])
def admin_metrics(request, edition_slug):
    """GET polls/admin/kol-nominations/<edition_slug>/metrics/ -> metrics(round_) (see above). Auth as every KOL
    admin address: 401 signed out, 403 kol_admin_only, 404 awards_edition_not_found / kol_round_not_found.
    Consumed by frontend KolNominationsMetrics.tsx (the Metrics tab's "Nominations round")."""
    from .kol_nominations import _admin_edition, _no_round, round_for_edition_slug

    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    round_ = round_for_edition_slug(edition.slug)
    if round_ is None:
        return _no_round()
    return Response(metrics(round_), status=status.HTTP_200_OK)
