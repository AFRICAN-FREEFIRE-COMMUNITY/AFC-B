"""Capture's in-process event bus - the one place the dashboard reads "what is happening" from.

WHAT IT DOES
------------
Every part of Capture that does something worth showing (the watcher sees a file, the uploader tries and
retries, the tailer reads a kill, the updater finds a release, the controller changes status, the logger
writes a line) calls ``emit(kind, **data)``. The bus keeps the last ``MAX_EVENTS`` events in a ring with an
ever-growing sequence number, and wakes anyone waiting in ``wait_for()``. The dashboard server
(dashboard.py) turns that into a live stream; ``DashboardState`` (also dashboard.py) folds the same events
into the "current picture" (folder, match, queue, version) so a freshly opened dashboard starts complete.

HOW IT CONNECTS
---------------
    watcher.py / uploader.py / tailer.py / updater.py / app.py   --emit-->   events.bus
    events.bus   --wait_for()-->   dashboard.py (SSE stream + /api/state)   -->   assets/dashboard.html

stdlib only, so every module may import it without pulling in a third-party dependency. Emitting never
raises and never blocks: a dashboard that is closed or broken must not be able to slow capture down
(the same rule as the tray notifications: decoration, never load-bearing). Nothing secret goes through
here: the upload token is never an event field.

OBSERVE ONLY (owner 2026-09-22): events describe what the client saw. No points, positions or rankings are
computed for the dashboard either; the server's models calculate those.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

MAX_EVENTS = 2000     # ring size; a map is ~300 events (kills, knocks, joins, respawns), this is an evening
# Heartbeats (inbox #510, the 9 Nov walk): what every live push sends. They wake the stream and fold into the
# dashboard's state like any event, but only the last TICK_KEEP are kept, beside the ring: three a push filled all
# 2000 slots within a map, and a page opened after it was handed 400 heartbeats and no kill.
TICK_KINDS = frozenset({"push", "board", "live_status"})
TICK_KEEP = 20


class EventBus:
    """A thread-safe ring of events with a sequence number and a wait/notify."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._ring: deque = deque(maxlen=max_events)
        self._ticks: deque = deque(maxlen=TICK_KEEP)      # the last heartbeats, kept apart (TICK_KINDS)
        self._cond = threading.Condition()
        self._seq = 0
        self._listeners: list = []

    def emit(self, kind: str, **data) -> int:
        """Append one event; returns its sequence number. Never raises."""
        try:
            with self._cond:
                self._seq += 1
                ev = {"seq": self._seq, "ts": time.time(), "kind": kind, "data": data}
                (self._ticks if kind in TICK_KINDS else self._ring).append(ev)
                # Listeners (the dashboard's state folder) run under the lock so they see events in order.
                for fn in list(self._listeners):
                    try:
                        fn(ev)
                    except Exception:  # pragma: no cover - a broken listener must not break capture
                        pass
                self._cond.notify_all()
                return self._seq
        except Exception:  # pragma: no cover - the bus must never break capture
            return 0

    def add_listener(self, fn) -> None:
        """Call ``fn(event)`` for every future event, in order, on the emitting thread. Keep it fast."""
        with self._cond:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._cond:
            if fn in self._listeners:
                self._listeners.remove(fn)

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def since(self, seq: int, limit: int = 500) -> list:
        """Events with a sequence number above ``seq`` (oldest first), at most ``limit``."""
        with self._cond:
            out = self._after(seq)
        return out[:limit]

    def last(self, n: int) -> list:
        """The newest ``n`` events kept (oldest first). Not "the last n sequence numbers": heartbeats use those up
        without staying in the ring (TICK_KINDS)."""
        with self._cond:
            return self._after(0)[-n:] if n > 0 else []

    def wait_for(self, seq: int, timeout: float = 15.0) -> list:
        """Block until there is an event above ``seq`` or ``timeout`` passes; returns what is there."""
        with self._cond:
            if self._seq <= seq:
                self._cond.wait(timeout)
            return self._after(seq)[:500]

    def _after(self, seq: int) -> list:
        """Ring and heartbeats above ``seq``, in emit order. Call with the lock held."""
        out = [e for e in self._ring if e["seq"] > seq] + [e for e in self._ticks if e["seq"] > seq]
        out.sort(key=lambda e: e["seq"])
        return out

    def clear(self) -> None:
        with self._cond:
            self._ring.clear()
            self._ticks.clear()


# The one bus of the process. Tests that need isolation call bus.clear() or build their own EventBus.
bus = EventBus()


def emit(kind: str, **data) -> int:
    """Module-level shortcut used by watcher / uploader / tailer / updater / app."""
    return bus.emit(kind, **data)


class BusLogHandler(logging.Handler):
    """Copies Capture's log records onto the bus as ``log`` events (the dashboard's log panel)."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - logging API
        try:
            bus.emit("log", level=record.levelname, msg=record.getMessage()[:600], logger=record.name)
        except Exception:  # pragma: no cover
            pass


def attach_log_handler(logger_name: str = "afc_capture", level: int = logging.INFO) -> Optional[logging.Handler]:
    """Attach the bus handler to Capture's logger once; returns the handler (or None if already there)."""
    lg = logging.getLogger(logger_name)
    for h in lg.handlers:
        if isinstance(h, BusLogHandler):
            return None
    h = BusLogHandler(level)
    lg.addHandler(h)
    return h
