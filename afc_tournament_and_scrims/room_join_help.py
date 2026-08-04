# ── afc_tournament_and_scrims/room_join_help.py ──────────────────────────────────────────────
# The joining steps players are shown for a 3D CUSTOM ROOM (owner 2026-08-04), and the one place
# that text lives on the backend.
#
# WHY IT EXISTS. A 3D room is not joined the way an ordinary custom room is: the squad has to be a
# complete group first, and the leader goes in through Customs and League rather than typing a room
# id on the lobby screen. Players who did not know that simply failed to join, so the steps now
# travel WITH the room id and password whenever Match.room_is_3d is on.
#
# HOW IT CONNECTS. Two kinds of consumer:
#   * The EVENT PAGE renders its own translated copy from the frontend message files
#     (frontend/messages/{en,fr,pt}/tournaments.json, key `roomJoin3d.*`), because that surface is
#     internationalised and a player reads it in their own language. This module is NOT the source
#     for that copy; the two are kept in step by hand and both trace back to tasks/todo.md.
#   * The BROADCAST MESSAGE BUILDERS in views.py (_group_room_details_text,
#     broadcast_match_room_details, broadcast_to_stage) and views_room_release.py paste this text
#     onto the end of the notification and email body. Those bodies are ONE string built for a
#     whole group, not per recipient, so they are English today for every recipient, exactly like
#     the "Room ID: / Room Name: / Password:" lines they follow. Making them per-recipient is a
#     separate change to how broadcasts are composed, not something to smuggle in here.
#
# NOT COVERED, and deliberately: the WhatsApp room-details message
# (afc_tournament_and_scrims/whatsapp_room_details.py). It sends a Meta-APPROVED TEMPLATE whose
# body and variable slots are fixed on Meta's side, so extra text cannot be added without
# submitting a new template for approval. Flagged to the owner rather than silently dropped.

# The owner's own wording, typos corrected on their instruction (2026-08-04). Kept as ONE constant
# rather than a list so a caller cannot reorder or partially render it: these are numbered steps and
# step 3 makes no sense before step 2.
ROOM_JOIN_3D_HELP = """How to join the 3D room
1. Log in and create a group.
2. Add all of your team members to the group. You must be a complete squad of 4. It is the same way you add your friends to a group when you want to play any mode.
3. Whoever is the group leader then goes to Customs.
4. Go to League.
5. Or you can search for the room ID.
6. Tap the join icon when you see the room.
7. Enter your team name, team tag and then the password.
8. Make sure you are using the correct account registered on the AFC website. If you are not, your results will not count and your team could be penalized or even banned."""


def append_3d_help(body, matches):
    """Append the 3D joining steps to a room-details message body, once, when they are wanted.

    Args:
        body:    the message built so far, or None/"" when there was nothing to send.
        matches: the Match rows this message is about. An iterable, because a group broadcast
                 covers every map in the group and a single-map broadcast covers one.

    Returns the body unchanged unless at least one of those maps is a 3D room.

    ONCE, not once per map, and that is the point of taking an iterable. A group can have five
    maps; five copies of the same eight steps would bury the room ids they are attached to. If any
    map in the message is a 3D room the steps are relevant to that message, and a player reading
    them for a map that is not 3D loses nothing.
    """
    if not body:
        return body
    if not any(getattr(m, "room_is_3d", False) for m in matches):
        return body
    return f"{body}\n\n{ROOM_JOIN_3D_HELP}"
