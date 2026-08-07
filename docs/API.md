# Google Search API (CAPTURED 2026-08-07, browser transport)

- **Base host(s):** `www.google.com`
- **Auth:** cookie-session, from a dedicated Playwright persistent profile (`.session/<name>/`).
  Optional — signed out works and returns the neutral view.
- **Anti-bot:** yes, unnamed Google interstitial (`/sorry/index`, "unusual traffic")
  - **Evidence:** bundled Chromium with Playwright's default `--enable-automation` → `/sorry/`
    on query 1, four consecutive attempts. Same code with the flag stripped → 200 + results.
    Real Chrome (`channel="chrome"`) with the flag present → also 200. `playwright-stealth`
    applied and full behavioural realism (curved mouse approach, per-keystroke jitter, organic
    homepage → type → Enter, `navigator.webdriver` already `False`) did **not** substitute for
    stripping the flag.
- **Capture vehicle:** Playwright persistent context, headful, ad-hoc probe scripts
  (`~/dev/google-search-recon/`), not web-surfer-llm callbacks mode — the target is a rendered
  SERP, not an XHR surface, so there is no JSON round-trip to intercept.
- **Transport tier:** **4b** (tier 4 + `ignore_default_args=["--enable-automation"]` + warmed profile)
- **Profile warming required:** **yes.** A fresh profile refuses cold direct `/search?q=`
  navigation. One organic in-page search (homepage → type → Enter) unlocks direct URLs
  permanently for that profile.
- **Exit IP class:** **residential required.** Measured on a consumer line in Germany. VPN exits
  CAPTCHA on every vehicle tested.
- **Captured as identity:** `esinecan@gmail.com` (read off the page, not assumed — local Chrome's
  `/u/0` on this box is the *persona* account and would have been the wrong default)
- **Verified:** 2026-08-07 / live queries via the shipped client

---

## Intent → endpoint map

| user intent | endpoint | status |
|---|---|---|
| search the web with operators | Search | mapped |
| page deeper into results | Search (`start=`) | mapped |
| restrict by site/filetype/date/phrase | Search (query operators) | mapped |
| restrict by freshness / vertical / region | Search (`tbs`, `tbm`, `cr`+`gl`) | mapped |
| get results as a specific identity | Search + session profile | mapped |
| turn personalization off | Search (`pws=0`) | mapped |
| read the full text of a result | the result URL, in the same browser | mapped (`with_content`, `google_fetch`) |
| read the cached copy of a result | — | **explicitly unmapped**: Google retired the page cache 2 Feb 2024; `cache:` and `webcache.googleusercontent.com` are gone. The Wayback Machine is the only general cache left and is the wrong source for a recency tool — it would answer a `freshness='day'` query with months-old text. Live fetch through the warmed browser is fresher *and* reads more (JS, soft paywalls, login-gated pages) |
| 100 results in one request | — | **explicitly unmapped**: `num=` stopped being honoured 11 Sept 2025 |

## Search

- `GET https://www.google.com/search`
- Params (required vs optional):
  - `q` → the assembled query string (**required**)
  - `hl` → interface/results language (optional, defaults `en`)
  - `start` → result offset, **multiples of 10** (optional; omit for page 1 — sending `start=0`
    is a needless difference from what a browser sends)
  - `tbs` → comma-separated filter directives (optional). **Order is significant, see below**
    - `qdr:h|d|w|m|y` freshness window (`h` = past hour)
    - `li:1` verbatim — no synonyms, no stemming
    - `cdr:1,cd_min:M/D/YYYY,cd_max:M/D/YYYY` custom date range. **US date format, not ISO**
  - `udm` → vertical (optional; **omit for the default web SERP**).
    `web` Web tab, `2` images, `7` videos, `12` news, `36` books, `39` short videos,
    `50` AI Mode, `28` shopping. **`tbm` is dead as a scheme** — Google redirects
    `tbm=isch` to `udm=2` itself, and `tbm=nws` renders byte-identical structure to `udm=12`.
  - `cr` → `country<XX>` uppercase, paired with `gl` lowercase (optional)
  - `pws` → `0` disables personalization (optional; absent = personalized)

### `tbs` directive order is load-bearing

Measured 2026-08-07, `renewable energy storage`:

| `tbs` | reported total | top hosts |
|---|---|---|
| `qdr:w` | 514,000 | electrek.co, the-european.eu |
| `li:1` | 124,000,000 | nationalgrid, iso.org |
| `qdr:w,li:1` | 124,000,000 | **identical to `li:1` alone — the freshness was discarded** |
| `li:1,qdr:w` | 453,000 | electrek.co, dyness — both applied |

Freshness placed *after* verbatim is dropped with no error and no warning. A caller asking
for the past week silently receives the whole index. `li:1` is therefore always emitted
first. This is the worst failure class this tool can have — plausible, wrong, invisible —
and it is pinned by `TestTbsOrdering`.
- Query-string operators, assembled into `q`: `site:`, `filetype:`, `-term`, `"exact phrase"`,
  `before:YYYY-MM-DD`, `after:YYYY-MM-DD`
- **Pagination:** `start` in steps of 10. Page size is 10 and not adjustable. Stop condition:
  a page returning materially fewer than 10 organic results is the last one.
- **Response:** HTML. No JSON endpoint is used. Organic results are extracted from anchors
  wrapping an `<h3>` under `#search` / `#rso`.

## Field semantics & traps

- **Ads carry no `<h3>` — but they DO carry `[role="heading"]`.** Anchoring on `a h3`
  excludes them *structurally* rather than by filtering. That guarantee does **not** survive
  broadening the selector. On `hotel berlin buchen`: `a h3` → 10 anchors, 0 in an ad
  container; `a [role="heading"]` → 2 anchors, **2 in an ad container**. And it degrades
  invisibly — on a technical query the same selector returns 7 with 0 ads, so a union
  selector passes exactly the tests a developer would write and leaks only on commercial
  queries. Verticals with no `<h3>` therefore rely on the `#tads`/`#tadsb`/`#bottomads`
  container filter as the actual guarantee, not as a backstop.
  Ad containers are emitted **even when empty** — their presence proves nothing, only their
  content length does.
- **Non-web verticals render no `<h3>` at all.** h1/h2/h3/h4 are all zero on a news SERP,
  while 10 result anchors are present. Each vertical names its own anchor; see the table in
  the README.
- **Google Books puts result identity in the query string.** Every link shares the path
  `/books` and differs only in `?id=`. Deduping on `origin + pathname` collapses an entire
  page of results into **two** entries (one per `books.google.com` / `books.google.de`).
- **The image grid has no titles where you expect them.** `<img alt>` is empty and the
  16×16 base64 images inside each anchor are favicons. The anchor's own text is the title.
  Real thumbnails are lazy-loaded, so `naturalWidth` is 0 for anything off-screen.
- **`#result-stats` follows the browser locale, not `hl`.** This box renders
  `About 187.000.000 results (0,24s)` under `hl=en`. Parse by extracting digits and
  dropping every separator rather than trusting either convention.
- **Dates are discrete, class-less elements** — a bare `<span>` holding `2 days ago` or
  `28 Jul 2026`, adjacent to the snippet rather than inside it. Promoting them to a field
  is what makes recency sortable instead of prose.
- **A date is only trustworthy if its block holds exactly one result.** On verticals whose
  tiles are shallow siblings, the block climb overshoots into the shared grid container,
  which holds a single date element — and that one value then gets stamped onto every row.
  Measured on `berlin climate policy`:

  | vertical | results | dated | distinct dates |
  |---|---|---|---|
  | `images` | 96 | 96 | **1** |
  | `news` | 10 | 10 | **1** (`3 weeks ago`) |
  | `short_videos` | 11 | 11 | **1** (`29 Jun 2026`) |
  | `videos` | 6 | 6 | 4 — genuine, and the control that makes the rest readable |

  Identical dates across every row is the tell. The block climb now stops one level short
  of the first ancestor holding two results, returning the largest block that still
  describes exactly one — which fixes the snippet for the same reason, since an overshoot
  would otherwise merge a neighbouring tile's text in.

  After the fix, same query: `news` 10 results, 10 dated, **9 distinct**; `videos` 6/6 with
  4 distinct (unchanged, the control); `short_videos` 11 results with 1 genuinely dated;
  `images` undated. Suppressing the bad dates alone was not enough — it cost news its dates
  entirely, and news is where they matter most.
- **The description sits ~4 DOM levels above the anchor**, not adjacent to it. The anchor's own
  subtree already holds title + URL breadcrumb (~70–105 chars), so any *absolute* length threshold
  for "found the result block" trips immediately and yields empty snippets. The test must be
  relative to the anchor's own text length.
- **`start=0` vs omitted** — behaviourally identical, but omitting matches browser traffic.
- **`num=` is dead** (11 Sept 2025). Anything claiming 100 results per request is stale advice.
- **The `cache:` operator is dead** (2 Feb 2024).

## AI Mode (`udm=50`)

- Streams. Measured: 1260 chars at t=2s, 3003 at t=4s, flat through t=16s. Polled until the
  text stops growing rather than slept on a fixed guess.
- **Anchored on `[data-subtree="aimc"]`**, with `[data-scope-id="turn"]` as fallback. This
  is the one place a container hook is used instead of generic structure, and it was earned:
  a "largest coherent text block" heuristic picked a *different* wrong thing on every query
  tried — a citation card, the nav strip plus the echoed query, and on a nonsense query an
  inline stylesheet returned as a confident 1260-character answer. Link density did not
  rescue it, because card descriptions are not anchor text.
- Citations are collected page-wide, not from inside the answer container — the source-card
  rail sits outside it.
- **Absence is a normal result, not drift.** AI Mode is not offered for every query, region
  or account; one of the two probe queries rendered only a share dialog. It returns
  `available: false` with a reason and never raises `schema_drift`, because a false drift
  alarm on ordinary queries would make real drift unreadable. The cost of that choice: a
  rotated hook also reports as "not available", so the reason string names both causes.
- The answer is **not trustworthy** and the tool description says so. The citations are the
  usable output.

## Probe classification

| probe | request sent | status | taxonomy kind | note |
|---|---|---|---|---|
| nonsense query | `q=<random string>` | 200 | `empty` | Google shows a no-results banner; distinguishable from drift |
| zero organic, no banner | any | 200 | `schema_drift` | the extractor is stale — never retried |
| automation flag present | `/search?q=` | 302→`/sorry/` | `rate_limited` | the interstitial, not a 429 |
| VPN exit | any | `/sorry/` | `rate_limited` | reproduces on every vehicle |
| signed out | any | 200 | *not an error* | returns the neutral view; personalization silently off |

`empty` deliberately has no exception class — a search matching nothing returns `count: 0`
with no `kind` field, because turning "no results" into an error teaches agents to retry
things that will never succeed.

## Legal / etiquette

Automated querying of `google.com/search` is contrary to Google's Terms of Service. This is
recorded as an owner decision, not an assumption:

- Read-only. No writes, no account mutation, no ad interaction.
- Single dedicated profile, one browser, **sequential requests only** — no concurrency, which
  is both the anti-bot signature and the impolite pattern.
- Throttled at the wrapper: 4s minimum interval plus 0–3s jitter, per host.
- Page depth capped at 5 (50 results).
- Personal-scale volume. No bulk harvesting, no republication of results, no resale.
- The session profile is gitignored and is never copied, attached, or handed to another agent
  or worker — same rule as `doctolib-mcp/docs/safety.md`.
