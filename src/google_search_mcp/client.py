"""All Google knowledge lives here. No I/O framing, no MCP, no CLI.

Three jobs, in the paradigm's order:

- **Build the query.** The advanced-operator surface is the reason this tool beats a
  keyword search API, and operators are fiddly and easy to get subtly wrong. Callers pass
  structured arguments (`site`, `filetype`, `before`, `after`, `exact`, `exclude`) and this
  assembles them, so the agent never has to remember whether it is `before:` or `daterange:`.
- **Fetch.** Throttled, warmed, ad-aware.
- **Normalise at the boundary.** Raw DOM never reaches the agent. Results are projected to
  {rank, title, url, host, snippet}, deduped by origin+path, ads dropped.

Deliberate deviation from paradigm §1.6: the compound tool does **not** fan out
concurrently. apthunt and doctolib both do, and both are right to -- but concurrency is
precisely the signature Google's anti-bot watches for. `multi_search` therefore walks its
queries sequentially through one warmed browser, and its win is amortising the browser
launch and warm-up (~10s) across N queries rather than parallelism. Recorded here because
a future reader will otherwise "fix" it back to a ThreadPoolExecutor.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

from . import session as _session
from .errors import RateLimited, SchemaDrift

RESULTS_PER_PAGE = 10  # Google stopped honouring num= on 11 Sept 2025. Depth costs round trips.
MAX_PAGES = 5
MAX_SNIPPET = 400

FRESHNESS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
VERTICALS = {"web": None, "news": "nws", "images": "isch", "videos": "vid", "shopping": "shop"}


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
# Anchored on structure that has to exist for the page to work -- an <a> wrapping an <h3>.
EXTRACT_JS = r"""
() => {
  const AD_ROOTS = ['#tads', '#tadsb', '#bottomads', '[data-text-ad]', '[aria-label="Ads"]'];
  const AD_WORDS = ['sponsored', 'gesponsert', 'anzeige'];

  const inAdContainer = (el) => AD_ROOTS.some(sel => el.closest(sel));

  // Walk up from the anchor to the smallest block that ADDS a description.
  //
  // A plain length threshold does not work and produced empty snippets on the first
  // build: the anchor's own subtree already carries title + URL breadcrumb (~70-105
  // chars), so any fixed cutoff trips at level 0 and never reaches the description,
  // which sits ~4 levels up. Measured against live SERPs 2026-08-07. So the test is
  // relative -- keep climbing until the text grows meaningfully beyond the anchor's own.
  const resultBlock = (a) => {
    const baseline = (a.innerText || '').length;
    let node = a;
    for (let i = 0; i < 6 && node.parentElement; i++) {
      node = node.parentElement;
      if ((node.innerText || '').length > baseline + 60) return node;
    }
    return node;
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
  // Measured 2026-08-07: Google's ad units carry NO <h3>. Since organic extraction below
  // is anchored on `a h3`, ads structurally cannot enter the results -- the stripping
  // guarantee is a property of the selector, not of a filter that might miss one. The
  // first build counted ads on the organic path and therefore always reported 0, which
  // read as "this query had no ads" when it actually meant "the counter is unreachable".
  // Verified on `hotel berlin buchen`: #tads 716 chars, #tadsb/#bottomads 865, h3 count 0.
  let adCount = 0;
  ['#tads', '#tadsb', '#bottomads'].forEach(sel => {
    const root = document.querySelector(sel);
    if (!root) return;
    const advertisers = new Set();
    root.querySelectorAll('a[href]').forEach(a => {
      try {
        const u = new URL(a.href);
        if (/^https?:$/.test(u.protocol) && !/(^|\.)google\.[a-z.]+$/.test(u.hostname)) {
          advertisers.add(u.origin);
        }
      } catch (e) { /* skip */ }
    });
    adCount += advertisers.size;
  });

  const organic = [];
  const seen = new Set();

  document.querySelectorAll('#search a h3, #rso a h3').forEach(h3 => {
    const a = h3.closest('a');
    if (!a || !a.href) return;

    let u;
    try { u = new URL(a.href); } catch (e) { return; }
    if (!/^https?:$/.test(u.protocol)) return;
    if (/(^|\.)google\.[a-z.]+$/.test(u.hostname)) return;   // /url, /aclk, internal chrome

    const block = resultBlock(a);
    const blockText = (block.innerText || '');
    const labelled = AD_WORDS.some(w => blockText.slice(0, 60).toLowerCase().includes(w));

    // Defence in depth. Known-redundant today (ads carry no h3) but kept, because the
    // day that changes this is the only thing standing between an ad and the agent.
    if (inAdContainer(a) || labelled) return;

    const key = u.origin + u.pathname;
    if (seen.has(key)) return;
    seen.add(key);

    const title = (h3.innerText || '').trim();
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

    organic.push({ url: u.href, host: u.hostname, title, snippet });
  });

  return {
    organic,
    ads_removed: adCount,
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


def build_url(
    q: str,
    page: int = 0,
    lang: str | None = None,
    country: str | None = None,
    freshness: str | None = None,
    vertical: str = "web",
    personalized: bool = True,
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
    if freshness:
        tbs = FRESHNESS.get(freshness)
        if not tbs:
            raise ValueError(f"freshness must be one of {sorted(FRESHNESS)}")
        params["tbs"] = tbs
    tbm = VERTICALS.get(vertical, "MISSING")
    if tbm == "MISSING":
        raise ValueError(f"vertical must be one of {sorted(VERTICALS)}")
    if tbm:
        params["tbm"] = tbm
    if not personalized:
        params["pws"] = "0"
    return "https://www.google.com/search?" + urlencode(params)


def _fetch_page(url: str) -> dict:
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
    data = page.evaluate(EXTRACT_JS)

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
) -> dict:
    pages = max(1, min(int(pages), MAX_PAGES))
    q = build_query(query, site, filetype, exact, exclude, before, after)

    def _work() -> dict:
        results: list[dict] = []
        seen: set[str] = set()
        ads_removed = 0

        for p in range(pages):
            url = build_url(q, p, lang, country, freshness, vertical, personalized)
            data = _fetch_page(url)
            ads_removed += data["ads_removed"]

            for item in data["organic"]:
                u = urlparse(item["url"])
                key = f"{u.netloc}{u.path}".rstrip("/")
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
                    }
                )
            # Fewer than a full page means the result set ran out; stop paging.
            if len(data["organic"]) < RESULTS_PER_PAGE - 2:
                break

        return {
            "query": q,
            "pages_fetched": p + 1,
            "personalized": personalized,
            "ads_removed": ads_removed,
            "count": len(results),
            "results": results,
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
