# afc_team/views_transfers.py
#
# The PUBLIC read surface for the automatic transfer feed (backlog item 21, owner 2026-08-08:
# "Public automatic transfer news showing players joining and leaving teams").
#
# Kept in its own module rather than appended to the 3,600-line afc_team/views.py because it is a
# self-contained read-only feature with no overlap with the roster-mutation endpoints, and because
# afc_team/urls.py does `from .views import *` - a separate module is imported explicitly, which is
# clearer about where the handler lives.
#
# How it connects to the rest of the system:
#   - Data      : afc_team.models.TeamTransfer, written automatically by
#                 afc_team.signals.record_team_join / record_team_leave on every roster change.
#   - Rules     : afc_team.transfers.has_competed_subquery (which teams are newsworthy) - see
#                 HAS_COMPETED_RULE there for why "played a match" and not "registered".
#   - Route     : GET /team/transfers/ (afc_team/urls.py).
#   - Consumed  : frontend components/news/TransferFeed.tsx, rendered as the "Transfers" category
#                 on app/(user)/news/page.tsx.
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import TeamTransfer
from .transfers import has_competed_subquery

# Pagination defaults (Best practices §10: every list endpoint takes a limit and reports whether
# more remain, and never loads everything into memory).
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


def _serialize_transfer(request, transfer):
    """One feed entry.

    Deliberately returns the player/team names and the raw `direction` key rather than a finished
    sentence: the sentence is assembled on the frontend from an ICU message
    (messages/<locale>/transfers.json entry.joined / entry.left) so French and Portuguese can put
    the words in their own order. A sentence built here by string concatenation would be English
    word order for everybody. Same reason `management_role` is the stored key, not a label.
    """
    team = transfer.team
    player = transfer.player
    return {
        "transfer_id": transfer.transfer_id,
        "direction": transfer.direction,
        # ── THE NAMES ARE THE LINK, so the LIVE name wins while the row still exists ──────────
        # Both public pages are addressed by NAME, not by id:
        #   /players/<username>  -> POST auth/get-player-profile with that username
        #   /teams/<team_name>   -> POST team/get-team-details with team_name, an EXACT match
        # so a renamed team or player linked by the name they had at the time of the move would
        # 404. (That is exactly how the event-invitation deep link broke for all 24 invitations
        # ever sent - it addressed /teams/<team_id>.) Sending the live name keeps the sentence and
        # the link agreeing with each other and with the rest of the site, which shows current
        # names everywhere.
        #
        # The *_at_move copies are the FALLBACK, for when the account or the team is gone: the
        # entry still reads, it simply carries no link (see player_exists / team_id below).
        "player_username": player.username if player else transfer.player_username_at_move,
        # Deliberately a BOOLEAN and not the user id. The frontend only needs "is there still an
        # account to link to", and this feed is public and unauthenticated - the join-request leak
        # (2026-08-08) is the reminder that an open endpoint should hand out no identifier it does
        # not actually need. The username is the address, so the id would be dead weight.
        "player_exists": player is not None,
        "team_id": team.team_id if team else None,
        "team_name": team.team_name if team else transfer.team_name_at_move,
        "team_tag": team.team_tag if team else None,
        # Absolute URL (API host): a bare `.url` is /media/... which the browser would resolve
        # against the FRONTEND origin, where no media lives. Same pattern as get_all_teams.
        "team_logo": request.build_absolute_uri(team.team_logo.url) if team and team.team_logo else None,
        "management_role": transfer.management_role or None,
        "occurred_at": transfer.occurred_at,
        # True = window open (routine), False = window closed (notable, the feed flags it),
        # null = no active season, so nothing honest to say. See TeamTransfer.in_transfer_window.
        "in_transfer_window": transfer.in_transfer_window,
    }


@api_view(["GET"])
def get_transfer_feed(request):
    """GET /team/transfers/ - the public feed of players joining and leaving teams, newest first.

    AUTH      : none. This is public information, the same as the team pages it links to.

    QUERY     : team_id  optional int, narrows the feed to ONE team ("what happened to my team",
                         which is the view people actually click).
                limit    optional int, 1..50, default 20.
                offset   optional int, >= 0, default 0.

    RESPONSE  : {
                  "results":     [ {transfer_id, direction, player_username, player_exists,
                                    team_id, team_name, team_tag, team_logo, management_role,
                                    occurred_at, in_transfer_window}, ... ],
                  "teams":       [ {team_id, team_name}, ... ]   # every team present in the feed,
                                                                 # for the frontend's team filter
                  "total_count": int,
                  "has_more":    bool,
                  "next_offset": int|null,
                  "limit":       int,
                  "offset":      int
                }

    FILTERING, in order:
      1. HAS COMPETED - only teams with at least one recorded match result appear at all. This is
         the owner's "do not fill the feed with churn from empty teams nobody has heard of"; the
         rule and the reasoning behind the threshold live in afc_team.transfers.HAS_COMPETED_RULE.
      2. team__isnull=False - drop entries whose team no longer exists. TeamTransfer.team is
         SET_NULL, so these are the rows left behind by a DISBAND: the team has no page to link to,
         and a disband is not a transfer. (The rows are kept rather than deleted so the history is
         not silently rewritten; they are simply not this feed's story.)

    CONSUMED BY: frontend components/news/TransferFeed.tsx (the "Transfers" category on /news).
    """
    # ── §1 base queryset: newsworthy teams only ────────────────────────────────────────────────
    # select_related both FKs so the per-row serializer does not lazy-load team + player
    # (the N+1 that get_all_teams / get_all_news already had to be fixed for).
    feed = (
        TeamTransfer.objects
        .select_related("team", "player")
        .filter(team__isnull=False)
        .filter(has_competed_subquery("team_id"))
    )

    # ── §2 optional per-team narrowing ─────────────────────────────────────────────────────────
    team_id_raw = request.GET.get("team_id")
    if team_id_raw not in (None, ""):
        if not str(team_id_raw).strip().isdigit():
            return Response({"message": "team_id must be a number."},
                            status=status.HTTP_400_BAD_REQUEST)
        feed = feed.filter(team_id=int(team_id_raw))

    # ── §3 the team filter's options ───────────────────────────────────────────────────────────
    # Computed from the SAME queryset before the team narrowing would empty it out, so the dropdown
    # only ever offers teams that actually have entries. Built off the unpaginated set on purpose:
    # deriving the options from the current page would hide teams that fall on page two.
    #
    # Names come from the LIVE team (team__team_name), not from team_name_at_move: a team that has
    # been renamed has entries carrying BOTH names, and distinct() over the stored copy would list
    # that one team twice, under an old name and a new one, as if they were rivals.
    filter_options = (
        TeamTransfer.objects
        .filter(team__isnull=False)
        .filter(has_competed_subquery("team_id"))
        .values("team_id", "team__team_name")
        .distinct()
        .order_by("team__team_name")
    )
    teams = [{"team_id": row["team_id"], "team_name": row["team__team_name"]}
             for row in filter_options]

    # ── §4 pagination ──────────────────────────────────────────────────────────────────────────
    try:
        limit = int(request.GET.get("limit", DEFAULT_PAGE_SIZE))
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        return Response({"message": "limit and offset must be numbers."},
                        status=status.HTTP_400_BAD_REQUEST)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    total_count = feed.count()
    page = list(feed[offset:offset + limit])
    has_more = offset + len(page) < total_count

    return Response({
        "results": [_serialize_transfer(request, t) for t in page],
        "teams": teams,
        "total_count": total_count,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "limit": limit,
        "offset": offset,
    }, status=status.HTTP_200_OK)
