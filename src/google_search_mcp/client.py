"""All Google knowledge lives here. No I/O framing, no MCP, no CLI.

Three jobs, in the paradigm's order:

- **Build the query.** The advanced-operator surface is the reason this tool beats a
  keyword search API, and operators are fiddly and easy to get subtly wrong. Callers pass
  structured arguments (`site`, `filetype`, `before`, `after`, `exact`, `exclude`) and this
  assembles them, so the agent never has to remember whether it is `before:` or `daterange:`.
- **Fetch.** Throttled, warmed, ad-aware.
- **Normalise at the boundary.** Raw DOM never reaches the agent. Results are projected to
  {rank, title, url, host, snippet, date}, deduped by origin+path, ads dropped.

Deliberate deviation from paradigm §1.6: the compound tool does **not** fan out
concurrently. apthunt and doctolib both do, and both are right to -- but concurrency is
precisely the signature Google's anti-bot watches for. `multi_search` therefore walks its
queries sequentially through one warmed browser, and its win is amortising the browser
launch and warm-up (~10s) across N queries rather than parallelism. Recorded here because
a future reader will otherwise "fix" it back to a ThreadPoolExecutor.

## Verticals are not one extractor (measured 2026-08-07)

`tbm` is dead as a scheme -- Google redirects `tbm=isch` to `udm=2` itself -- and each
`udm` vertical renders its results differently. Anchor counts on the same query:

    vertical        a h3   a [role=heading]   note
    web (default)      9         18 (2 ads)
    web_only udm=web  10          1 (1 ad)    strips rich blocks; 17 external anchors vs 56
    videos   udm=7    10          0
    books    udm=36   10          0           every link is google-internal
    news     udm=12    0         10
    shorts   udm=39    0         12
    images   udm=2      0          0          neither; the grid is anchors wrapping <img>
    shopping udm=28     0          0          product cards, no external links at all

**The trap that shaped this design.** The obvious fix for news was to broaden the anchor to
`[role="heading"]`. That destroys the ad guarantee. On `hotel berlin buchen`:

    a h3               10 anchors,  0 inside an ad container
    a [role=heading]    2 anchors,  2 inside an ad container

Both were ads. And it fails *invisibly*: on a technical query `[role=heading]` returns 7
with 0 ads, so a union selector tests clean on exactly the queries a developer would try
and starts leaking ads only on commercial ones. So each vertical names its own anchor, web
keeps `a h3` and keeps the structural guarantee, and on role-anchored verticals the
`inAdContainer` filter is **load-bearing rather than defence-in-depth**.
"""
from __future__ import annotations

import random
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

from . import session as _session
from .errors import BadArgument, RateLimited, SchemaDrift

RESULTS_PER_PAGE = 10  # Google stopped honouring num= on 11 Sept 2025. Depth costs round trips.
MAX_PAGES = 5
MAX_SNIPPET = 400

# Page-content budgets. Up here rather than beside the fetch code because `search` takes
# them as default arguments, and defaults are bound at definition time.
CONTENT_DEFAULT_CHARS = 2000
CONTENT_MAX_CHARS = 20000
CONTENT_DEFAULT_TOP_N = 3
CONTENT_MAX_TOP_N = 5

FRESHNESS = {
    "hour": "qdr:h",
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}

# Anchor per vertical -- see the module docstring for the measurements and the ad trap.
#   udm     None means "send nothing", which is the default rich SERP
#   anchor  h3 | role | image
#   google_hosts  books links all point at books.google.com, so the usual
#                 drop-google-hostnames rule would zero the vertical out
VERTICALS: dict[str, dict[str, Any]] = {
    "web": {"udm": None, "anchor": "h3"},
    "web_only": {"udm": "web", "anchor": "h3"},
    "news": {"udm": "12", "anchor": "role"},
    "videos": {"udm": "7", "anchor": "h3"},
    "short_videos": {"udm": "39", "anchor": "role"},
    "books": {"udm": "36", "anchor": "h3", "google_hosts": True},
    "images": {"udm": "2", "anchor": "image"},
}

# `shopping` (udm=28) is deliberately absent. It renders product cards with zero external
# anchors and zero headings -- there is nothing for a link-and-snippet projection to return,
# and shipping it as a vertical that always yields 0 would repeat exactly the bug this
# release fixes. Left unmapped and documented rather than half-supported.

# Verticals whose result grid is one page. `start=` paginates the ten-blue-links shape;
# the image grid is infinite-scroll and does not honour it.
SINGLE_PAGE = {"images"}


class _Throttle:
    """Per-host minimum interval with jitter (paradigm §1.3).

    Non-negotiable, and it lives here rather than in the agent on purpose: if pacing is
    left to the caller, the caller has to reason about pacing, and a reasoning agent under
    load is a flailing agent.
    """

    def __init__(self, min_interval: float = 4.0, jitter: float = 3.0):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            gap = self.min_interval + random.uniform(0, self.jitter)
            sleep_for = last + gap - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last[host] = time.monotonic()


_throttle = _Throttle()


# Extraction. Deliberately loose: an extractor over-fitted to Google's current obfuscated
# class names measures the parser, not the page, and those class names rotate by design.
# Anchored on structure that has to exist for the page to work.
#
# Takes a config so one function serves every vertical -- see VERTICALS above.
EXTRACT_JS = r"""
(cfg) => {
  const AD_ROOTS = ['#tads', '#tadsb', '#bottomads', '[data-text-ad]', '[aria-label="Ads"]'];
  const AD_WORDS = ['sponsored', 'gesponsert', 'anzeige'];

  const inAdContainer = (el) => AD_ROOTS.some(sel => el.closest(sel));
  const isGoogle = (h) => /(^|\.)google\.[a-z.]+$/.test(h);

  // Walk up from the anchor to the smallest block that ADDS a description.
  //
  // A plain length threshold does not work and produced empty snippets on the first
  // build: the anchor's own subtree already carries title + URL breadcrumb (~70-105
  // chars), so any fixed cutoff trips at level 0 and never reaches the description,
  // which sits ~4 levels up. Measured against live SERPs 2026-08-07. So the test is
  // relative -- keep climbing until the text grows meaningfully beyond the anchor's own.
  // ...but never climb into a container holding a SECOND result. That is what produced
  // the date leak (one shared date stamped onto every row) and it would equally merge a
  // neighbouring tile's text into this result's snippet. So the climb stops one level
  // short of the first ancestor that holds two results, and returns the largest block
  // that still describes exactly one.
  const resultBlock = (a, sel) => {
    const baseline = (a.innerText || '').length;
    let node = a, best = a;
    for (let i = 0; i < 6 && node.parentElement; i++) {
      node = node.parentElement;
      if (node.querySelectorAll(sel).length > 1) break;
      best = node;
      if ((node.innerText || '').length > baseline + 60) return node;
    }
    return best;
  };

  // Dates are discrete, class-less elements -- "2 days ago", "28 Jul 2026", "20.01.2025".
  // Promoting them out of the snippet is what makes recency sortable instead of prose.
  const DATE_RE = [
    /^\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago$/i,
    /^vor\s+\d+\s+\w+$/i,
    /^\d{1,2}\s+\w{3,}\.?\s+\d{4}$/,
    /^\d{1,2}\.\d{1,2}\.\d{4}$/,
    /^\w{3,}\.?\s+\d{1,2},\s+\d{4}$/,
  ];
  // Bare forms of the anchors below, for counting results inside a candidate block.
  const BARE = { h3: 'a h3', role: 'a [role="heading"]', image: 'a img' };

  const findDate = (block, anchorKind) => {
    // A date belongs to THIS result only if the block that holds it holds exactly one
    // result. The block climb overshoots on verticals whose tiles are shallow siblings,
    // landing on the shared grid container -- which holds ONE date element that then gets
    // stamped onto every row. Measured 2026-08-07 on `berlin climate policy`:
    //
    //     images        96 results, 96 dated, 1 distinct value
    //     news          10 results, 10 dated, 1 distinct ("3 weeks ago")
    //     short_videos  11 results, 11 dated, 1 distinct ("29 Jun 2026")
    //     videos         6 results,  6 dated, 4 distinct  <- genuine, and the control
    //
    // Identical dates across every row is the tell, and a confident wrong date is worse
    // than an absent one because nothing downstream can detect it.
    if (block.querySelectorAll(BARE[anchorKind] || BARE.h3).length > 1) return null;
    let found = null;
    block.querySelectorAll('span,div,cite').forEach(e => {
      if (found || e.children.length) return;
      const t = (e.innerText || '').trim();
      if (t.length < 30 && DATE_RE.some(re => re.test(t))) found = t;
    });
    return found;
  };

  // Everything that is structurally present but is not the description.
  const isChrome = (line, title, host) => {
    const l = line.trim();
    if (!l || l === title) return true;
    if (/^https?:\/\//.test(l)) return true;   // the URL line
    if (l.includes('›')) return true;      // breadcrumb, "site › path › page"
    if (l === host || l.endsWith(host)) return true;
    if (/^(web results|videos|images|news|people also ask)$/i.test(l)) return true;
    if (l.length < 25) return true;             // source labels: "GitHub", "Wikipedia"
    return false;
  };

  // Count ads at their containers, not on the organic path.
  //
  // Measured 2026-08-07: Google's ad units carry NO <h3>. On h3-anchored verticals ads
  // therefore cannot enter the results at all -- the stripping guarantee is a property of
  // the selector. On role-anchored verticals that is NOT true (2/2 role anchors were ads
  // on a commercial query), so there inAdContainer below is the actual guarantee.
  let adCount = 0;
  ['#tads', '#tadsb', '#bottomads'].forEach(sel => {
    const root = document.querySelector(sel);
    if (!root) return;
    const advertisers = new Set();
    root.querySelectorAll('a[href]').forEach(a => {
      try {
        const u = new URL(a.href);
        if (/^https?:$/.test(u.protocol) && !isGoogle(u.hostname)) advertisers.add(u.origin);
      } catch (e) { /* skip */ }
    });
    adCount += advertisers.size;
  });

  // The image grid has no heading element and its <img> alt is empty -- the 16x16
  // base64 images inside each anchor are favicons, not the picture. The only title
  // Google gives is the anchor's own text, so the anchor IS the node here.
  const ANCHORS = {
    h3:    '#search a h3, #rso a h3',
    role:  '#search a [role="heading"], #rso a [role="heading"]',
    image: '#search a:has(img), #rso a:has(img)',
  };

  const organic = [];
  const seen = new Set();

  document.querySelectorAll(ANCHORS[cfg.anchor] || ANCHORS.h3).forEach(node => {
    const a = node.closest('a');
    if (!a || !a.href) return;

    let u;
    try { u = new URL(a.href); } catch (e) { return; }
    if (!/^https?:$/.test(u.protocol)) return;
    // /url, /aclk and internal chrome -- except on verticals whose results ARE google-hosted.
    if (isGoogle(u.hostname) && !cfg.googleHosts) return;

    const block = resultBlock(a, BARE[cfg.anchor] || BARE.h3);
    const blockText = (block.innerText || '');
    const labelled = AD_WORDS.some(w => blockText.slice(0, 60).toLowerCase().includes(w));
    if (inAdContainer(a) || labelled) return;

    // Google Books puts the identity of a result in the query string -- every link shares
    // the path /books and differs only in ?id=. Keying on origin+pathname collapsed a
    // whole page of books into two results. So google-hosted verticals key on the query too.
    const key = u.origin + u.pathname + (cfg.googleHosts ? u.search : '');
    if (seen.has(key)) return;
    seen.add(key);

    // On the image grid the anchor's own text is the title; the <img> alt is empty and
    // the 16x16 base64 images inside are favicons. No thumbnail field is emitted: the
    // real thumbnails are lazy-loaded, so off-screen ones have naturalWidth 0 and would
    // be null for most of the page. A field that is usually null is worse than no field.
    const title = (node.innerText || '').trim();
    if (!title) return;

    const snippet = blockText
      .split('\n')
      .filter(l => !isChrome(l, title, u.hostname))
      .join(' ')
      .replace(/\s+/g, ' ')
      // Google's own affordances are appended INTO the description text, not emitted as
      // separate lines, so they survive the line filter above. Anchoring these to end-of-
      // string does not work either -- "Read more" is routinely followed by further chrome
      // ("Missing: x | Show results with: x"), so each is stripped wherever it appears.
      .replace(/\b(Read more|Mehr anzeigen|Weitere Informationen)\b/gi, ' ')
      .replace(/\bMissing:.*$/i, '')
      .replace(/\bShow results with:.*$/i, '')
      .replace(/\s+/g, ' ')
      .trim();

    // Images never carry a usable per-tile date; the one-result-per-block rule in
    // findDate would catch it anyway, but this states it rather than relying on it.
    const date = cfg.anchor === 'image' ? null : findDate(block, cfg.anchor);
    organic.push({ url: u.href, host: u.hostname, title, snippet, date });
  });

  const stats = document.querySelector('#result-stats');

  return {
    organic,
    ads_removed: adCount,
    result_stats: stats ? (stats.innerText || '').replace(/\s+/g, ' ').trim() : null,
    // Distinguishes "Google says nothing matched" from "our selectors broke".
    no_results_banner: /did not match any documents|keine Dokumente/i.test(document.body.innerText || ''),
  };
}
"""


def build_query(
    query: str,
    site: str | None = None,
    filetype: str | None = None,
    exact: str | None = None,
    exclude: Iterable[str] | None = None,
    before: str | None = None,
    after: str | None = None,
) -> str:
    """Assemble Google's operator syntax from structured arguments."""
    parts = [query.strip()]
    if exact:
        parts.append(f'"{exact}"')
    if site:
        parts.append(f"site:{site}")
    if filetype:
        parts.append(f"filetype:{filetype}")
    for term in exclude or []:
        term = term.strip()
        if term:
            parts.append(f"-{term}")
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    return " ".join(p for p in parts if p)


def _cdr(after: str | None, before: str | None) -> str:
    """Tools > Custom range, which wants M/D/YYYY rather than ISO."""

    def us(d: str) -> str:
        y, m, day = d.split("-")
        return f"{int(m)}/{int(day)}/{y}"

    parts = ["cdr:1"]
    if after:
        parts.append(f"cd_min:{us(after)}")
    if before:
        parts.append(f"cd_max:{us(before)}")
    return ",".join(parts)


def build_url(
    q: str,
    page: int = 0,
    lang: str | None = None,
    country: str | None = None,
    freshness: str | None = None,
    vertical: str = "web",
    personalized: bool = True,
    verbatim: bool = False,
    tbs_extra: str | None = None,
) -> str:
    # Empty string means unset, explicitly rather than incidentally. Agents and CLI flags
    # both produce "" for "not given", and letting it fall through to the truthiness check
    # below would silently skip validation on a value the caller did supply.
    lang = lang or None
    country = country or None
    freshness = freshness or None
    vertical = vertical or "web"

    params: dict[str, Any] = {"q": q, "hl": lang or "en"}
    if page:
        params["start"] = page * RESULTS_PER_PAGE
    if country:
        params["cr"] = f"country{country.upper()}"
        params["gl"] = country.lower()

    # tbs is ONE parameter carrying comma-separated directives, and **their order decides
    # whether they all apply**. Measured 2026-08-07 on `renewable energy storage`:
    #
    #     qdr:w        -> 514,000    electrek.co, the-european.eu, dyness.com
    #     li:1         -> 124,000,000  nationalgrid, iso.org, sciencedirect
    #     qdr:w,li:1   -> 124,000,000  identical to li:1 alone -- freshness DROPPED
    #     li:1,qdr:w   -> 453,000    electrek.co, dyness.com -- both applied
    #
    # Freshness after verbatim is silently discarded: no error, no warning, just a year of
    # results for a caller who asked for a week. So verbatim is emitted first, always.
    # Validation runs before the early-outs so a bad freshness raises even when unused.
    fresh = None
    if freshness:
        fresh = FRESHNESS.get(freshness)
        if not fresh:
            raise BadArgument(f"freshness must be one of {sorted(FRESHNESS)}")

    tbs: list[str] = []
    if verbatim:
        tbs.append("li:1")
    if tbs_extra:          # cdr custom range; supersedes a preset window
        tbs.append(tbs_extra)
    elif fresh:
        tbs.append(fresh)
    if tbs:
        params["tbs"] = ",".join(tbs)

    spec = VERTICALS.get(vertical)
    if spec is None:
        raise BadArgument(f"vertical must be one of {sorted(VERTICALS)}")
    if spec["udm"] is not None:
        params["udm"] = spec["udm"]

    if not personalized:
        params["pws"] = "0"
    return "https://www.google.com/search?" + urlencode(params)


_STATS_RE = re.compile(r"([\d][\d.,\s]*)\s*(?:results|Ergebnisse)", re.I)
_TIME_RE = re.compile(r"\(([\d.,]+)\s*(?:seconds?|Sekunden|s)\)", re.I)


def parse_result_stats(text: str | None) -> dict:
    """`About 187.000.000 results (0,24s)` -> {total, seconds}.

    Thousands separators follow the *browser* locale, not `hl` -- this box renders
    `187.000.000` and `0,24s` under `hl=en`. So digits are extracted and every separator
    dropped, rather than trusting either convention.
    """
    if not text:
        return {"total": None, "seconds": None, "raw": None}
    total = None
    m = _STATS_RE.search(text)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        total = int(digits) if digits else None
    seconds = None
    t = _TIME_RE.search(text)
    if t:
        try:
            seconds = float(t.group(1).replace(",", "."))
        except ValueError:
            seconds = None
    return {"total": total, "seconds": seconds, "raw": text}


def _fetch_page(url: str, spec: dict) -> dict:
    """One SERP. MUST run on the browser thread."""
    page = _session.get_page()
    _throttle.wait(url)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)

    if "/sorry/" in page.url:
        raise RateLimited(
            "Google served the /sorry/ CAPTCHA. Slow the calling pattern down; if a VPN "
            "is on, turn it off -- VPN exits trip this on every vehicle."
        )

    time.sleep(random.uniform(0.9, 1.6))
    data = page.evaluate(
        EXTRACT_JS,
        {"anchor": spec["anchor"], "googleHosts": bool(spec.get("google_hosts"))},
    )

    if not data["organic"] and not data["no_results_banner"]:
        raise SchemaDrift(
            "A 200 SERP yielded zero organic results and Google showed no no-results "
            "banner. The extractor is almost certainly stale against a markup change. "
            "This is NOT an empty result set."
        )
    return data


def search(
    query: str,
    pages: int = 1,
    site: str | None = None,
    filetype: str | None = None,
    exact: str | None = None,
    exclude: Iterable[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    lang: str | None = None,
    country: str | None = None,
    freshness: str | None = None,
    vertical: str = "web",
    personalized: bool = True,
    verbatim: bool = False,
    strict_dates: bool = False,
    with_content: bool = False,
    content_top_n: int = CONTENT_DEFAULT_TOP_N,
    content_chars: int = CONTENT_DEFAULT_CHARS,
) -> dict:
    """One query.

    `strict_dates` routes `before`/`after` through Tools > Custom range (`tbs=cdr`) instead
    of the `before:`/`after:` query operators. Both are real and both were measured; the
    operators filter on the document date Google infers, the range filter on its own index.
    One pair of arguments drives both so there is never a question of which dates won.

    `with_content` reads the top `content_top_n` results as markdown, in the same warmed
    browser, and attaches each to its result. Bounded by default on purpose: unbounded it
    is a crawler, and ten pages of article text is a bigger payload than most questions are
    worth. Each page costs a real page load, so budget a few seconds per result.
    """
    pages = max(1, min(int(pages), MAX_PAGES))
    vertical = vertical or "web"
    spec = VERTICALS.get(vertical)
    if spec is None:
        raise BadArgument(f"vertical must be one of {sorted(VERTICALS)}")
    if vertical in SINGLE_PAGE:
        pages = 1

    use_ops = not strict_dates
    q = build_query(
        query, site, filetype, exact, exclude,
        before if use_ops else None,
        after if use_ops else None,
    )
    tbs_extra = _cdr(after, before) if (strict_dates and (before or after)) else None

    def _work() -> dict:
        results: list[dict] = []
        seen: set[str] = set()
        ads_removed = 0
        stats = None
        p = 0

        for p in range(pages):
            url = build_url(q, p, lang, country, freshness, vertical, personalized,
                            verbatim=verbatim, tbs_extra=tbs_extra)
            data = _fetch_page(url, spec)
            ads_removed += data["ads_removed"]
            if stats is None:
                stats = parse_result_stats(data.get("result_stats"))

            for item in data["organic"]:
                u = urlparse(item["url"])
                # Same reason as the in-page dedupe: on google-hosted verticals the result
                # identity lives in the query string, so dropping it merges the whole page
                # into one entry.
                key = f"{u.netloc}{u.path}".rstrip("/")
                if spec.get("google_hosts"):
                    key = f"{key}?{u.query}"
                if key in seen:
                    continue
                seen.add(key)
                snippet = item.get("snippet") or ""
                results.append(
                    {
                        "rank": len(results) + 1,
                        "title": item["title"],
                        "url": item["url"],
                        "host": item["host"],
                        "snippet": snippet[:MAX_SNIPPET],
                        "date": item.get("date"),
                    }
                )
            # Fewer than a full page means the result set ran out; stop paging.
            if len(data["organic"]) < RESULTS_PER_PAGE - 2:
                break

        # Content last, and inside the same browser-thread call: every SERP page is already
        # collected, so navigating away costs nothing, and this avoids re-entering the
        # single browser thread (which would deadlock on itself).
        if with_content:
            n = max(1, min(int(content_top_n), CONTENT_MAX_TOP_N))
            for item in results[:n]:
                got = _fetch_one(item["url"], content_chars)
                item["content"] = got.get("markdown") if got.get("ok") else None
                if not got.get("ok"):
                    item["content_error"] = got.get("error")
                elif got.get("truncated"):
                    item["content_truncated"] = True
                    item["content_chars_total"] = got.get("chars_total")

        return {
            "query": q,
            "vertical": vertical,
            "pages_fetched": p + 1,
            "personalized": personalized,
            "ads_removed": ads_removed,
            "total_matches": (stats or {}).get("total"),
            "count": len(results),
            "results": results,
        }

    return _session.in_browser_thread(_work)


# --- Page content --------------------------------------------------------------------
#
# The one thing neither the harness WebSearch nor the Brave API can do on this box: read
# the page through a warmed, logged-in Chrome. JS-rendered SPAs, soft paywalls and
# anything gated behind a Google login are all readable here and are not readable by a
# plain HTTP fetch.
#
# **Google's cache is not an option and never was.** It was retired 2 Feb 2024 -- `cache:`
# and webcache.googleusercontent.com are both gone (recorded in API.md's intent map). The
# Wayback Machine is the only real cache left, and it is the wrong source for a tool whose
# entire edge is recency: it would serve months-old text for a `freshness='day'` query.
# Live fetch through the browser we already have is both fresher and more capable.
#
# Splitting the work: the browser renders (JS, cookies, login), trafilatura strips the
# boilerplate. Boilerplate removal is a hard, well-solved problem and reimplementing it as
# another pile of selectors here would be the same mistake as over-fitting the SERP parser.
# Markdown rather than text because structure is most of the meaning in a technical page --
# headings, code fences and lists survive, and it costs fewer tokens than the HTML did.

def _to_markdown(html: str, url: str) -> str:
    try:
        import trafilatura
    except ImportError:  # soft dependency: searching must not break because extraction is absent
        return ""
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            # Precision over recall: a short clean article beats a long one with the
            # comment section and the "related stories" rail glued to the end.
            favor_precision=True,
        )
        or ""
    )


def _truncate(md: str, max_chars: int) -> tuple[str, bool]:
    """Cut on a paragraph boundary when one is close, so the tail is never half a sentence."""
    if len(md) <= max_chars:
        return md, False
    cut = md[:max_chars]
    boundary = cut.rfind("\n\n")
    if boundary > max_chars * 0.6:
        cut = cut[:boundary]
    return cut.rstrip(), True


def _fetch_one(url: str, max_chars: int) -> dict:
    """One page to markdown. MUST run on the browser thread."""
    max_chars = max(200, min(int(max_chars), CONTENT_MAX_CHARS))
    if not url.lower().startswith(("http://", "https://")):
        return {"url": url, "ok": False, "error": "not an http(s) URL"}

    page = _session.get_page()
    _throttle.wait(url)  # per-host, so a fresh host is not made to wait for Google's window
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(0.6, 1.2))  # let deferred content paint
        html = page.content()
    except Exception as exc:
        # One dead link must not sink a search. Same per-item isolation as multi_search.
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    md = _to_markdown(html, url)
    if not md.strip():
        return {
            "url": url,
            "ok": False,
            "error": "no article content extracted (login wall, or not an article page)",
        }
    text, truncated = _truncate(md, max_chars)
    return {
        "url": url,
        "ok": True,
        "markdown": text,
        "chars": len(text),
        "chars_total": len(md),
        "truncated": truncated,
    }


def fetch(urls: list[str], max_chars: int = CONTENT_DEFAULT_CHARS) -> dict:
    """Read pages as markdown through the warmed browser.

    Sequential and throttled per host, for the same reason `multi_search` is: this is a
    real browser making real requests, and the polite pattern is also the unblocked one.
    """
    urls = list(urls)[:CONTENT_MAX_TOP_N]

    def _work() -> dict:
        pages = [_fetch_one(u, max_chars) for u in urls]
        return {
            "count": len(pages),
            "ok_count": sum(1 for p in pages if p.get("ok")),
            "pages": pages,
        }

    return _session.in_browser_thread(_work)


# --- AI Mode -------------------------------------------------------------------------
#
# udm=50. A different shape entirely: prose, not a result list, so it is a separate tool
# rather than a vertical -- a caller asking for `results` must never silently get an essay.
#
# It streams. Measured 2026-08-07: 1260 chars at t=2s, 3003 at t=4s, then flat through
# t=16s. So it is polled until the text stops growing rather than slept on for a fixed
# guess, which keeps the common case fast and the slow case correct.

AIMODE_JS = r"""
() => {
  // Anchored on a container hook, NOT on "the biggest block of text". That heuristic was
  // tried and it picked a different wrong thing on every query measured 2026-08-07:
  //
  //   renewable energy storage  -> a citation card: `AltE StoreEnergy Storage
  //                                Technologies...6 Jan 2023 - ...`
  //   what is the model context protocol -> the nav strip, then the query echoed twice
  //   a nonsense query          -> an inline stylesheet, `.rWfmBc .YNk70c{display:none}`,
  //                                reported as a confident 1260-character answer
  //
  // Link density did not save it either: card descriptions are not anchor text. The DOM
  // dump found a real hook -- `[data-subtree="aimc"]` held exactly the answer prose,
  // 2532 chars at link ratio 0.03, no nav and no cards.
  const HOOKS = ['[data-subtree="aimc"]', '[data-scope-id="turn"]'];
  let node = null, hook = null;
  for (const h of HOOKS) {
    const n = document.querySelector(h);
    if (n && (n.innerText || '').trim().length > 200) { node = n; hook = h; break; }
  }

  const text = node ? (node.innerText || '').replace(/\s+/g, ' ').trim() : '';

  // Citations live in the source-card rail OUTSIDE the answer container, so they are
  // collected page-wide. They are the part of this tool worth trusting.
  const cites = [];
  const seen = new Set();
  document.querySelectorAll('a[href^="http"]').forEach(a => {
    let u; try { u = new URL(a.href); } catch (e) { return; }
    if (/(^|\.)google\.[a-z.]+$/.test(u.hostname)) return;
    if (seen.has(u.href)) return;
    seen.add(u.href);
    cites.push({ url: u.href, host: u.hostname,
                 text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) });
  });

  return { text, chars: text.length, hook, citations: cites.slice(0, 20) };
}
"""

AIMODE_MIN_CHARS = 400  # below this it is page furniture, not an answer


def ai_mode(query: str, lang: str | None = None, country: str | None = None,
            max_wait: float = 20.0) -> dict:
    """Google's AI Mode answer for a query, with its citations.

    **Absence is a normal outcome, not a failure.** AI Mode is not offered for every query,
    every region or every account, so "not there" returns `available: False` with the
    reason -- never `schema_drift`. Treating it as drift would fire a false alarm about a
    stale extractor on any ordinary query Google simply chose not to answer.
    """
    params: dict[str, Any] = {"q": query, "hl": lang or "en", "udm": "50"}
    if country:
        params["cr"] = f"country{country.upper()}"
        params["gl"] = country.lower()
    url = "https://www.google.com/search?" + urlencode(params)

    def _work() -> dict:
        page = _session.get_page()
        _throttle.wait(url)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if "/sorry/" in page.url:
            raise RateLimited("Google served the /sorry/ CAPTCHA while opening AI Mode.")

        # Poll until the answer stops growing, or we run out of patience.
        last, stable, waited = -1, 0, 0.0
        data = {"text": "", "chars": 0, "citations": [], "hook": None}
        while waited < max_wait:
            time.sleep(2.0)
            waited += 2.0
            data = page.evaluate(AIMODE_JS)
            if data["chars"] == last and data["chars"] >= AIMODE_MIN_CHARS:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = data["chars"]

        if data["chars"] < AIMODE_MIN_CHARS:
            return {
                "query": query,
                "available": False,
                "reason": (
                    "No AI Mode answer rendered for this query. It is not offered for every "
                    "query, region or account, so this is usually a normal outcome and is "
                    "not worth retrying unchanged. If it persists on queries that DO show an "
                    "answer in a real browser, the container hook has rotated -- see the "
                    "AI Mode note in docs/API.md."
                ),
                "answer": None,
                "citations": [],
                "waited_seconds": waited,
            }
        return {
            "query": query,
            "available": True,
            "answer": data["text"],
            "chars": data["chars"],
            "citations": data["citations"],
            "waited_seconds": waited,
        }

    return _session.in_browser_thread(_work)


def multi_search(queries: list[str], **kwargs) -> dict:
    """The compound tool (paradigm §1.6): the manual loop this site forces, collapsed.

    Researching anything on Google means running several related queries and reading
    ten results at a time. That is the part that does not scale, and it is what an agent
    otherwise spends N tool round-trips and N browser warm-ups on.

    Sequential, not concurrent -- see the module docstring for why that inversion of the
    paradigm's fan-out rule is deliberate here.
    """
    out, errors = [], {}
    for q in queries:
        try:
            out.append(search(q, **kwargs))
        except Exception as exc:  # per-item isolation: one bad query cannot sink the call
            kind = getattr(exc, "kind", "unknown")
            errors[q] = {"kind": kind, "error": str(exc)}
            if kind == "rate_limited":
                break  # pointless to keep hammering; return what we have

    return {
        "queries_run": len(out),
        "total_results": sum(r["count"] for r in out),
        "searches": out,
        "errors": errors,
    }
