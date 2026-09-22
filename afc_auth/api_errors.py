"""The one way to answer a request that blew up (owner rule R79, applied 2026-09-22).

WHY
---
A handler whose last resort is a bare `except Exception` that puts the exception's own text into the
response body (an "error" key beside a polite "An error occurred.") hands the browser whatever the
exception said. That text is written by MySQL, by Django or by a library, not by us: one of the nine
could answer with error 1054 naming a table and a column that do not match, a file path names the
deploy layout, and a driver error can carry a host name. It is also useless to the person reading
it, who cannot act on any of it.

The rule: the CLIENT gets one generic sentence and a code it can translate; the LOG gets the
exception with its traceback, where it is searchable and nobody outside can read it.

USE
---
    from afc_auth.api_errors import internal_error
    ...
    except Exception as exc:
        return internal_error(exc, where="join_team", code="team_join_failed")

`where` is a short label that goes in the log line so the entry can be found; `code` is what the
frontend switches on (lib/apiMessage). Both are optional: the default code is "internal_error" and
the default label is the calling function's name.

WHAT IT IS NOT FOR
------------------
Refusals the code MEANT to send. A sentence this codebase wrote on its own exception class
(BracketError, GroupCapacityError, ProviderError, DrawError, a ValueError raised by our own parser
with a written message) is a real answer to the user and belongs in the response exactly as it is.
Those sites are declared in security-rules.json with that reason, and this helper must not be used
to flatten them into "something went wrong", which would take away the one sentence that tells the
organizer what to change.
"""

import logging

from rest_framework import status as http_status
from rest_framework.response import Response

log = logging.getLogger(__name__)

# One sentence, no detail, translatable by its code on the frontend.
GENERIC_MESSAGE = (
    "Something went wrong on our side. Please try again in a moment. If it keeps happening, "
    "contact support and we will look at the log."
)
GENERIC_CODE = "internal_error"


def internal_error(exc, *, where="", code=GENERIC_CODE, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                   message=GENERIC_MESSAGE):
    """Log `exc` with its traceback and return the generic coded 500 the client sees."""
    label = where or "api"
    # log.exception keeps the traceback, which is the whole point of not sending it to the browser.
    log.exception("%s failed: %s", label, exc.__class__.__name__)
    return Response({"message": message, "code": code}, status=status)
