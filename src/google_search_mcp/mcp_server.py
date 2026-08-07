"""MCP server over Google Search, through a dedicated logged-in browser profile.

Run:  python -m google_search_mcp.mcp_server   (stdio)

Everything here reads. There is no write path and no binding action, so unlike doctolib
this needs no confirm-gate and no safety.md.

What this gives an agent that a keyword search API does not:

- **The advanced-operator surface.** site:, filetype:, exact phrase, exclusions,
  before:/after: date bounds, freshness windows down to the past hour, verbatim, and every
  Google vertical. All free, all server-side, all reachable once the profile is warmed.
- **Better sources on technical queries** than either alternative on this box. Measured
  against the harness WebSearch and the Brave API: primary sources (vendor engineering
  blogs, the spec's own issue tracker, official SDK docs) where both alternatives returned
  SEO blogspam paraphrasing the same announcement. See docs/ROADMAP.md.
- **Personalized ranking.** Measured 2026-08-07 at ~3.4x the run-to-run noise floor:
  disabling it moved roughly a third of the top-10. Qualitatively it resolves ambiguous
  technical queries toward the domain sense -- "spring" returns Spring Framework rather
  than the film. Set personalized=False to get the neutral view.
- **Ads stripped** before the agent ever sees them.

Cost model worth knowing before calling: the first call in a process launches a browser
and warms the profile, ~10s. Subsequent calls are one throttled page load each (4-7s).
Pages are 10 results and depth costs a round trip, because Google stopped honouring num=
on 11 Sept 2025. Prefer `google_multi_search` over several `google_search` calls -- it
amortises the launch across queries.

Built on `MCPServer`, the high-level API the `mcp` package exposes since 2.0.0 -- it
replaced the bundled `mcp.server.fastmcp.FastMCP`, which 2.0.0 removed.
"""
from __future__ import annotations

import logging
from typing import Optional

from mcp.server import MCPServer

from . import client, session
from .errors import GoogleError

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("google-search")


@mcp.tool()
def google_session_status() -> dict:
    """Whether the dedicated profile has a live Google session, and which account it is.

    Worth calling once before relying on personalization: signed out still works, it just
    returns the neutral (unpersonalized) view. The account is read off the page rather
    than assumed, because "whoever was signed in" is not a safe default on a box with
    more than one Google account.
    """
    return session.status()


@mcp.tool()
def google_search(
    query: str,
    pages: int = 1,
    site: Optional[str] = None,
    filetype: Optional[str] = None,
    exact: Optional[str] = None,
    exclude: Optional[list[str]] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    lang: Optional[str] = None,
    country: Optional[str] = None,
    freshness: Optional[str] = None,
    vertical: str = "web",
    personalized: bool = True,
    verbatim: bool = False,
    strict_dates: bool = False,
) -> dict:
    """Search Google. Ads stripped; results are {rank, title, url, host, snippet, date}.

    Use the structured arguments rather than typing operators into `query` -- they
    assemble the correct syntax for you:

      site='arxiv.org'          restrict to one domain
      filetype='pdf'            only PDFs
      exact='model context protocol'   quoted phrase, must appear verbatim
      exclude=['tutorial']      drop results containing a term
      after='2026-01-01'        published after a date (before= for the other bound)
      freshness='week'          hour | day | week | month | year
      verbatim=True             no synonyms or stemming; the words as typed
      strict_dates=True         apply before/after as Tools > Custom range instead of
                                as query operators (index date rather than document date)
      country='de', lang='de'   region and language bias
      personalized=False        the neutral view, no account history applied

    `vertical` selects which Google tab to read:

      web           the default SERP, including rich blocks
      web_only      the "Web" tab -- plain links, no rich blocks. Cleanest for research;
                    measured 17 external anchors against 56 on the default SERP.
      news          news, with dates
      videos        video results
      short_videos  the shorts feed
      books         Google Books (results are google-hosted by nature)
      images        the image grid; single page, `title` comes from alt text

    `date` is populated where Google shows one ("2 days ago", "28 Jul 2026") and is null
    otherwise. `total_matches` is Google's own estimate for the whole query, not the
    number returned.

    `pages` is 10 results each, max 5, and each page is a separate round trip -- ask for
    depth only when you actually need it. Ignored for `images`.

    On failure the result carries a `kind` field: auth_expired (sign in), schema_drift
    (the extractor is stale, do not retry), rate_limited (back off). An empty result set
    is NOT an error -- it returns count=0 with no `kind`, and means Google matched nothing.
    """
    try:
        return client.search(
            query, pages=pages, site=site, filetype=filetype, exact=exact,
            exclude=exclude, before=before, after=after, lang=lang,
            country=country, freshness=freshness, vertical=vertical,
            personalized=personalized, verbatim=verbatim, strict_dates=strict_dates,
        )
    except GoogleError as exc:
        return exc.as_result()


@mcp.tool()
def google_ai_mode(
    query: str,
    lang: Optional[str] = None,
    country: Optional[str] = None,
) -> dict:
    """Google's AI Mode answer for a query, with the sources it cites.

    **The answer is unreliable. Treat it as a lead, never as a fact.** It is hit or miss,
    it is confidently wrong at the same tone it is right, and it is not authoritative even
    about Google's own products -- which is the trap, because those are exactly the queries
    where it reads most credible. Nothing from here should reach a user, a document or a
    decision without being confirmed against a real source.

    The `citations` are the valuable part; the prose is a map to them. Normal use is: read
    this for orientation on an unfamiliar topic, then `google_search` for the primary
    sources and believe those instead. For anything load-bearing, skip this tool.

    **Absence is normal.** AI Mode is not offered for every query, region or account. When
    it is not there you get `available: False` and a reason, NOT an error and NOT
    schema_drift -- so do not retry the same query hoping for a different shape.

    Slower than a search: the answer streams, and this polls until it stops growing
    (typically ~4s, capped at 20s).
    """
    try:
        return client.ai_mode(query, lang=lang, country=country)
    except GoogleError as exc:
        return exc.as_result()


@mcp.tool()
def google_multi_search(
    queries: list[str],
    pages: int = 1,
    personalized: bool = True,
) -> dict:
    """Run several related queries in one call. Prefer this when researching a topic.

    Google makes you search, read ten results, then search again -- and every separate
    tool call otherwise pays the browser launch and warm-up again. This amortises that
    across the whole set.

    Runs sequentially on purpose: concurrent requests are exactly what Google's anti-bot
    watches for, so the win here is the shared warm browser, not parallelism. Budget
    roughly 5-7 seconds per query after the first.

    One failing query does not sink the call -- failures land in `errors` keyed by query,
    and the rest still return. A rate_limited stops the run early rather than hammering.
    """
    return client.multi_search(list(queries), pages=pages, personalized=personalized)


if __name__ == "__main__":
    mcp.run()
