"""The game's replay file, read while the game writes it: one reader for AFC Capture and AFC LIVE (owner rule R24).

THE FILE
--------
``Free Fire_64_Data\\Replays\\ReplayInfo_<match id>_<time>.bin``. The game opens it when the map STARTS
(ReplayManager.OnRecordStarted, FileStream(Create, Write, FileShare.ReadWrite)) and appends every network packet as it
arrives, so another program may read it while it grows (proven 4 Nov 2026, AFC inbox #482 #485). A client restart
mid-map starts a second file for the same match. Records, little endian:

    u8 kill cam | f32 game seconds | u8 send option | u16 cmd | u16 length | length bytes

Payloads are 7-bit varints (signed ones zigzag). Research and proofs: WEBSITE/tasks/weapon-ids-and-replay-2026-11-03/
README.md sections 7 and 8.

THE GUN OF EVERY KILL AND KNOCK (inbox #506, owner "WHY DOES WEAPONS ONLY SHOW FOR SOME AND NOT ALL?", 6 Nov 2026)
--------------------------------------------------------------------------------------------------------------
The debugger log names the gun on about 6 kills in 10 (OnGameAchievementNotify) and never on a knock. The replay
has every hit: 106 RUDP_TAKE_DAMAGE [attacker, victim, damage, 0, weapon (zigzag), ...], 139 RUDP_KNOCK_DOWN
[victim, knocker, ...], 107 RUDP_DEAD [victim, killer, ...]; their ids are the log's entity ids (43 of 43 kills and
47 of 47 knocks of the 6 Nov map found their record by killer and victim). The game's own kill gun is the gun of the
killer's LAST HIT BEFORE THE KNOCK, or before the death when there was no knock by him: 26 of 26 kills where the log
names a gun agree (the plain last hit before the death: 21 of 26; a knocked player is often finished with something
else). A knock's gun is the knocker's last hit before it.

EVERY PLAYER'S DAMAGE (inbox #512, owner "The site still does not store damage", 9 Nov 2026)
------------------------------------------------------------------------------------------
The server keeps each player's own game variables in sync: 118 RUDP_BIND_PRI ties a replication id to a player's
entity id, 1900 RUDP_VAR_SYNC_PRI_DATA carries blocks of (variable, value) per replication id (client enum
EPRI_PLAYER_*; WEBSITE/tasks/weapon-ids-and-replay-2026-11-03/README.md section 8). Var 31 TOTAL_DAMAGE is the game's own
damage figure. On the 6 Nov map it went on across the client crash (23 of 23 players resumed at or above their figure
before it) and ran 0 to 24% above the hits the observer was sent (median 0 to 7%): the game counts hits it did not send
the observer, so its figure is the one to store. One replication id belongs to one client session: bind per file.

CONSUMERS
---------
tailer.LiveTailer._poll_replay (tails the match's files, LiveMatchState.apply_replay_guns fills the kill log,
apply_replay_damage the damage the upload carries), AFC LIVE tournament.py (ReplayReader, varint, varints,
bound_players, var_blocks: the overlays' health, armour, held gun, win chance). stdlib only.
"""

from __future__ import annotations

import struct

CMD_HIT, CMD_DEAD, CMD_KNOCK = 106, 107, 139
CMD_BIND, CMD_VARS = 118, 1900
V_DAMAGE = 31                     # EPRI_PLAYER_TOTAL_DAMAGE


def varint(b: bytes, i: int) -> tuple:
    v = s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return v, i


def varints(b: bytes) -> list:
    out, i = [], 0
    while i < len(b):
        v, i = varint(b, i)
        out.append(v)
    return out


def zigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def bound_players(data: bytes) -> list:
    """118 RUDP_BIND_PRI: [varint 2 x n] then n times [replication id, type, entity id]; the (replication id, entity id)
    of each one of type 1, a player."""
    v = varints(data)
    return [(v[i], v[i + 2]) for i in range(1, 1 + 3 * (v[0] // 2), 3) if v[i + 1] == 1]


def var_blocks(data: bytes):
    """1900 RUDP_VAR_SYNC_PRI_DATA: blocks of [varint replication id][u16 LE length][(varint var, varint value) ...];
    yields (replication id, var, value) in order. A cut record raises IndexError or struct.error part way."""
    i = 0
    while i < len(data):
        rep, i = varint(data, i)
        ln = struct.unpack_from("<H", data, i)[0]
        i += 2
        end = i + ln
        while i < end:
            var, i = varint(data, i)
            val, i = varint(data, i)
            yield rep, var, val
        i = end


class GameDamage:
    """The game's own damage figure (var 31) of every player in ONE replay file, by entity id. Replication ids are
    bound per client session, so a match with two files (a crash) gets one GameDamage per file; the later file's
    figure is the newer one."""

    def __init__(self) -> None:
        self.bind: dict = {}           # replication id -> entity id
        self.damage: dict = {}         # entity id -> TOTAL_DAMAGE

    def feed(self, records) -> bool:
        """Records from ReplayReader. True when a figure changed."""
        changed = False
        for _t, cmd, data in records:
            try:
                if cmd == CMD_BIND:
                    self.bind.update(bound_players(data))
                elif cmd == CMD_VARS:
                    for rep, var, val in var_blocks(data):
                        eid = self.bind.get(rep)
                        if var == V_DAMAGE and eid is not None and self.damage.get(eid) != val:
                            self.damage[eid] = val
                            changed = True
            except (IndexError, struct.error):
                continue        # a cut record: skip it, never stop reading
        return changed


class ReplayReader:
    """Reads the records the game appended since the last call. The game writes in 4 KB pieces, so the end of
    the file is often half a record: that tail is kept and finished on the next read, never parsed early."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.pos = 0
        self.rest = b""

    def read_new(self) -> list:
        try:
            # Python opens with FILE_SHARE_READ | FILE_SHARE_WRITE, which the game's FileShare.ReadWrite allows
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read()
        except OSError:
            return []
        if not chunk:
            return []
        self.pos += len(chunk)
        return self.feed(chunk)

    def feed(self, chunk: bytes) -> list:
        """Complete records in rest + chunk; whatever is left over waits for the next piece."""
        buf = self.rest + chunk
        out = []
        off = 0
        while off + 10 <= len(buf):
            kc, t, _so, cmd, ln = struct.unpack_from("<BfBHH", buf, off)
            if kc > 1:
                # not a record boundary: the file is not a replay (or was cut); drop what is held
                buf = b""
                off = 0
                break
            if off + 10 + ln > len(buf):
                break
            out.append((t, cmd, buf[off + 10: off + 10 + ln]))
            off += 10 + ln
        self.rest = buf[off:]
        return out


class KillGuns:
    """The gun of every kill and knock of one match, in replay order (see the module docstring for the rule).

    ``deaths`` and ``knocks`` are lists of (killer, victim, gun or None) in the order the game sent them; a gun is a
    weapon id above 1 (1 is bare hands; 0 and negatives are the zone, a fall, bleeding)."""

    def __init__(self) -> None:
        self.deaths: list = []
        self.knocks: list = []
        self._last_hit: dict = {}      # (attacker, victim) -> gun of the latest hit
        self._knocked: dict = {}       # victim -> (knocker, gun) of the victim's latest knock

    def feed(self, records) -> bool:
        """Records from ReplayReader. True when a kill or a knock was added."""
        added = False
        for _t, cmd, data in records:
            try:
                if cmd == CMD_HIT:
                    v = varints(data)
                    if len(v) >= 5 and v[0] != v[1]:
                        self._last_hit[(v[0], v[1])] = zigzag(v[4])
                elif cmd == CMD_KNOCK:
                    victim, i = varint(data, 0)
                    knocker, _ = varint(data, i)
                    gun = self._gun(self._last_hit.get((knocker, victim)))
                    self._knocked[victim] = (knocker, gun)
                    self.knocks.append((knocker, victim, gun))
                    added = True
                elif cmd == CMD_DEAD:
                    victim, i = varint(data, 0)
                    killer, _ = varint(data, i)
                    knock = self._knocked.pop(victim, None)
                    if knock is not None and knock[0] == killer and knock[1] is not None:
                        gun = knock[1]
                    else:
                        gun = self._gun(self._last_hit.get((killer, victim)))
                    self.deaths.append((killer, victim, gun))
                    added = True
            except IndexError:
                continue        # a cut record: skip it, never stop reading
        return added

    @staticmethod
    def _gun(gun):
        return gun if gun is not None and gun > 1 else None
