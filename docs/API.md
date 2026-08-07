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
| read the cached copy of a result | — | **explicitly unmapped**: Google retired the page cache 2 Feb 2024; `cache:` and `webcache.googleusercontent.com` are gone |
| 100 results in one request | — | **explicitly unmapped**: `num=` stopped being honoured 11 Sept 2025 |

## Search

- `GET https://www.google.com/search`
- Params (required vs optional):
  - `q` → the assembled query string (**required**)
  - `hl` → interface/results language (optional, defaults `en`)
  - `start` → result offset, **multiples of 10** (optional; omit for page 1 — sending `start=0`
    is a needless difference from what a browser sends)
  - `tbs` → freshness window: `qdr:d|w|m|y` (optional)
  - `tbm` → vertical: `nws` news, `isch` images, `vid` videos, `shop` shopping; **omit for web** (optional)
  - `cr` → `country<XX>` uppercase, paired with `gl` lowercase (optional)
  - `pws` → `0` disables personalization (optional; absent = personalized)
- Query-string operators, assembled into `q`: `site:`, `filetype:`, `-term`, `"exact phrase"`,
  `before:YYYY-MM-DD`, `after:YYYY-MM-DD`
- **Pagination:** `start` in steps of 10. Page size is 10 and not adjustable. Stop condition:
  a page returning materially fewer than 10 organic results is the last one.
- **Response:** HTML. No JSON endpoint is used. Organic results are extracted from anchors
  wrapping an `<h3>` under `#search` / `#rso`.

## Field semantics & traps

- **Ads carry no `<h3>`.** This is why anchoring extraction on `a h3` excludes them
  *structurally* rather than by filtering. Ad units live in `#tads` / `#tadsb` / `#bottomads`,
  which are emitted **even when empty** — their presence proves nothing, only their content length does.
- **The description sits ~4 DOM levels above the anchor**, not adjacent to it. The anchor's own
  subtree already holds title + URL breadcrumb (~70–105 chars), so any *absolute* length threshold
  for "found the result block" trips immediately and yields empty snippets. The test must be
  relative to the anchor's own text length.
- **`start=0` vs omitted** — behaviourally identical, but omitting matches browser traffic.
- **`num=` is dead** (11 Sept 2025). Anything claiming 100 results per request is stale advice.
- **The `cache:` operator is dead** (2 Feb 2024).

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
