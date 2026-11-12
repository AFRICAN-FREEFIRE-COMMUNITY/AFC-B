"""The one function of AFC Capture's roster.py that its reader (tailer.py) needs: the name normaliser. Copied from
afc-capture/afc_capture/roster.py (1.5.4); the rest of that module talks to the website and is not needed here."""
import re
import unicodedata


def _norm_name(value) -> str:
    """Normalize an IGN / team name to a comparable key: NFKD ascii-fold -> lowercase ->
    strip everything but a-z0-9. Mirrors the backend _norm_pname / _norm_tname so the client
    and server agree on what "the same name" means ("龙 | Pro.Gamer" -> "progamer")."""
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)
