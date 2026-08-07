"""The closed error taxonomy.

Paradigm §3.1: an agent cannot branch on prose. Five kinds, five behaviours, one stable
field name (`kind`) that every failing tool result carries.

    auth_expired   stop, surface the login. never retry
    schema_drift   stop, the tool is broken. never retry. flag stale
    rate_limited   back off and retry -- inside the wrapper, not in the agent
    bad_argument   stop, fix the call. never retry unchanged
    empty          NOT an error. it is the answer, and it is returned as a normal result

`empty` deliberately has no exception class. A search that matches nothing returns
`{"results": [], "count": 0}` with no `kind` field at all, because turning "no results"
into an error is how agents learn to retry things that will never succeed.
"""
from __future__ import annotations


class GoogleError(Exception):
    """Base. `kind` is what the agent branches on; the message is for the human."""

    kind: str = "unknown"

    def as_result(self) -> dict:
        return {"kind": self.kind, "error": str(self), "results": [], "count": 0}


class AuthExpired(GoogleError):
    """The dedicated profile has no live Google session.

    Never retried automatically: re-login is interactive by nature, and an agent that
    retries into a login wall burns its budget producing the same failure.
    """

    kind = "auth_expired"


class SchemaDrift(GoogleError):
    """A 200 page that yielded zero organic results.

    Google's SERP markup is obfuscated and rotates, so this is the expected long-run
    failure mode of this tool, not an exotic one. It means the extractor is stale --
    not that the query matched nothing. The distinction matters: `empty` is a result,
    this is a broken instrument, and conflating them makes drift invisible for weeks.
    """

    kind = "schema_drift"


class BadArgument(GoogleError, ValueError):
    """An argument this tool cannot act on -- an unknown vertical, an invalid freshness.

    Distinct from the other three because the fix is in the *call*, not in the tool and not
    in the pacing: retrying unchanged can never succeed, and neither can waiting. It
    subclasses ValueError as well so the pure functions stay usable, and testable, as plain
    Python outside any MCP framing.
    """

    kind = "bad_argument"


class RateLimited(GoogleError):
    """Google served /sorry/ -- the CAPTCHA interstitial.

    Retryable in principle, but the wrapper backs off rather than the agent, and the
    honest advice on repeat is to slow the calling pattern down. Measured trigger
    conditions, 2026-08-07: a VPN exit trips this on every vehicle; a residential exit
    with a warmed profile did not trip it across ~40 queries.
    """

    kind = "rate_limited"
