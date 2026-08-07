# Gap analysis vs the alternatives, and what closing each one costs

Companion to `API.md` (the contract) and `RECON.md` (what was tried during the build).
This one asks a different question: **against the two search tools already on this box, where
does this server actually stand, and what would make it the one worth reaching for first?**

Measured 2026-08-07, live queries, throwaway `probe` profile, residential Berlin IP.
Probe scripts under the session scratchpad; numbers below are all reproducible.

---

## The three surfaces

| | harness `WebSearch` | `brave-search` MCP | **this server** |
|---|---|---|---|
| transport | vendor API | Brave API | logged-in headful browser |
| latency | ~2-4s | ~1s | **~10s cold, 4-7s/page warm** |
| concurrency | free | free | **serialised, one browser thread** |
| returns | synthesised prose **+ links only** | title/url/description | {rank,title,url,host,snippet} |
| snippets | none in the link list | long, often a full paragraph | short, Google-style |
| **dates** | none | **structured** (`age`, `page_age`) on news | ~~inline in snippet~~ → **structured `date`** |
| region control | **none** (self-declared US-only) | none *in this wrapper* | **`cr`+`gl`+`hl`** |
| freshness | none | none *in this wrapper* | **`qdr:h/d/w/m/y`** — down to the past hour |
| date-bounded | none | none | **`before:`/`after:`, plus Tools custom range** |
| verbatim | none | none | **`li:1`** |
| operators | `allowed_domains` only | honours `site:`/`filetype:` typed in | **structured, assembled** |
| verticals | none | news, video, image (local 500s) | ~~all broken~~ → **7 working** |
| result count | none | none | **`total_matches`** |
| AI answer | synthesised from results | `summarizer` — **not in our plan** | **AI Mode**, with citations |
| page content | synthesised for you | `llm_context` — **not in our plan** | none — **still the real gap** |
| personalization | none | none | **yes, ~3.4x noise floor** |
| ads | n/a | n/a | **stripped structurally** |

Struck-through cells are what this document originally found; the arrow is where they
landed the same day. The one row that did not move is `page content`, and it is now the
only place either alternative does something this cannot.

Two corrections to assumptions worth recording, because both cut against this server:

- **Brave's operator surface is not as thin as it looks.** `site:arxiv.org ... filetype:pdf`
  typed straight into `brave_web_search` worked. The structured-argument advantage here is
  real but narrower than the module docstring implies — it holds for `before:`/`after:`,
  freshness and region, not for `site:`/`filetype:`.
- **Brave's headline feature is unavailable to us.** `brave_llm_context` (pre-extracted page
  text, the RAG-shaped tool) returns `OPTION_NOT_IN_PLAN`, and `brave_local_search` 500s.
  The Brave surface we *actually* have is web + news + video + image.

## Where this server already wins, and it is not close

Same query — `MCP stateless server migration streamable HTTP transport` — top hits:

| this server | Brave / WebSearch |
|---|---|
| developers.googleblog.com (2d) | wavect.io |
| blog.cloudflare.com (1d) | apigene.ai |
| github.com SEP-1442 issue | chatforest.com |
| docs.spring.io | botoi.com |
| equixly.com (1d) | channel.tel |

Google returned primary sources — the vendor blogs, the actual spec issue, official SDK docs.
Brave and WebSearch returned a wall of SEO blogspam that all paraphrases the same announcement.
On a technical query where the answer has an owner, that gap is the whole ballgame, and it is
the thing neither of the other two can be configured into.

Google's snippets also carry recency inline (`2 days ago`, `23 Jul 2026`). Brave carries dates
only on the news endpoint. WebSearch carries none at all.

---

## P0 — advertised and broken — **FIXED 2026-08-07**

Shipped in the same session this was written. Verified live on the `dev` profile, one query
across every vertical:

| vertical | before | after |
|---|---|---|
| `news` | `schema_drift` | 10 results, **10 dated, 9 distinct** |
| `videos` | `schema_drift` | 8 results, dated |
| `short_videos` | did not exist | 12 results |
| `books` | `schema_drift` | 10 results |
| `images` | `schema_drift` | 94 results |
| `web_only` | did not exist | 10 results |

Three bugs in this work were caught only by measuring the output, never by reading the
code — each produced confident, plausible, wrong results:

- **`tbs` order.** `qdr:w,li:1` silently discards the freshness; `li:1,qdr:w` applies both.
  A caller asking for the past week would have received the whole index, with no error.
- **Shared-container dates.** One date stamped onto every row — 96/96 identical on images,
  10/10 on news. `videos` returning 4 distinct dates was the control that made the rest
  legible.
- **Books dedupe.** Both dedupe layers keyed on `origin + path`, and every Books URL shares
  `/books`. Ten results collapsed to exactly two.

Also landed: `date` as a structured field, `total_matches` from `#result-stats`, freshness
down to `hour`, `verbatim`, `strict_dates` (Tools > Custom range), `google_ai_mode`, and a
`login()` that detects its own success. The original analysis below is kept as the record of
why each change was made.

### Every non-web vertical returns `schema_drift`

`vertical='news'|'images'|'videos'|'shopping'` is documented in the tool description and in
`API.md`. All of them fail. Not "return poor results" — return zero, and then correctly
self-diagnose as extractor drift.

Measured, `mcp stateless specification`:

| SERP | `a h3` | external result anchors |
|---|---|---|
| web (control) | 8 | 21 |
| `tbm=nws` | **0** | 10 |
| `udm=12` | **0** | 10 |

So the results are there. The extractor cannot see them, because **non-web verticals render
no `<h3>` at all** (h1/h2/h3/h4 all zero on a news SERP). Extraction is anchored on `a h3`.

Two things this is **not**:

- **Not a `tbm` → `udm` migration.** Both were tested. `tbm=nws` and `udm=12` produce byte-for-byte
  equivalent structure, and Google redirects `tbm=isch` → `udm=2` on its own. The params are fine.
- **Not schema drift in the sense the error claims.** It has never worked. `schema_drift` says
  "the extractor is stale, do not retry", which sends a reader hunting for a markup change that
  did not happen.

**The fix, and the trap in it.** `[role="heading"]` is the portable anchor — 10/10 on news.
But it must **not** be applied to the web vertical. On an ad-heavy query (`hotel berlin buchen`):

| selector | total | inside an ad container |
|---|---|---|
| `a h3` | 10 | **0** |
| `a [role="heading"]` | 2 | **2** |

Both `[role="heading"]` anchors were ads. The structural ad-exclusion guarantee — "ads carry no
`<h3>`", the property the whole ad story rests on — **dies the moment the selector broadens**.
And it dies invisibly: on the technical control query `a [role="heading"]` returns 7 with 0 ads,
so a union selector tests clean on exactly the queries a developer would try, and starts leaking
ads only on commercial ones.

Therefore: **per-vertical extractors, not one broadened selector.** Web keeps `a h3` and keeps its
structural guarantee. Non-web verticals anchor on `[role="heading"]` and there the `inAdContainer`
filter is promoted from defence-in-depth to load-bearing, with a regression test that asserts it.

Validated end to end — proposed news extraction returned 10 clean results:

```
Scaling AI Agent Infrastructure with the MCP Stateless updates | developers.googleblog.com | 1 day ago
The next generation of MCP                                    | blog.cloudflare.com        | 1 day ago
GitHub MCP Server supports the next MCP specification         | github.blog                | 1 day ago
Microsoft updates MCP C# SDK for stateless MCP                | www.infoworld.com          | 1 day ago
```

Until it is fixed, the honest move is to **delete the vertical argument from the tool
description**. A documented capability that always fails is worse than an absent one.

---

## P1 — parity gaps

### 1. Dates are text, not a field

Brave returns `age` and `page_age`. This server buries `2 days ago —` at the head of the snippet,
where it cannot be sorted, filtered or compared.

They are discrete DOM elements — bare `<span>`, no class — and extract cleanly. Measured 6 on one
technical SERP, 3 on an informational one, and the proposed extractor already pulls them
(`1 day ago`, `7 days ago`, `28 Jul 2026`, `null` where genuinely absent).

Cheapest high-value change in this document. Add `date` to the result projection.

### 2. No page content

WebSearch reads the pages and hands back prose. Brave sells `llm_context` for it. This server
returns links, so the agent pays a `WebFetch` round trip per URL it actually wants.

But this is where the browser transport stops being a liability: **a warmed, logged-in Chrome can
read pages `WebFetch` cannot** — JS-heavy SPAs, soft paywalls, anything gated behind a Google
login. Nothing else on this box can do that.

Shape: `google_search(..., with_content=True, content_top_n=3)`, fetching through the same warmed
browser, same throttle, capped hard. Bounded by default because unbounded it is a crawler.

### 3. The AI Overview is on the page and discarded

Confirmed present: 1976 characters of prose under `#m-x-content`, on the informational query
`what is the model context protocol`. Absent on the technical and German queries — it is
query-dependent, roughly informational-intent only. `.related-question-pair` (People Also Ask)
appeared 4x on two of three queries.

Capturing it gives the digest that WebSearch produces with a model, and that Brave charges for,
at the cost of reading a div. Return it as a **clearly-labelled separate field**, never merged
into results, and never as a substitute for them — it is Google's summary of the same pages the
agent is about to get, and it is wrong often enough that laundering it into the result set would
be the single most damaging thing this server could do.

Note it streams in asynchronously. The 5s settle used in the probe is not free; make it opt-in
(`include_ai_overview=True`) so ordinary searches keep their current latency.

---

## P2 — operational

### 4. `login()` never tells you it worked

It blocks on `page.wait_for_event("close")`. Sign-in succeeds, nothing happens, and the window
sits there until you happen to close it. Hit during this very session.

Fix: poll `read_account()` and print `signed in as <account> — close the window when ready`, or
auto-close on detection. Cheap, and it removes the one moment this tool needs a human.

### 5. The profile lock has no pool

Chromium takes an exclusive lock per user-data-dir, so Claude Code, pi and any dispatch drone
contend for one profile. `_locked()` explains it well and classifies it as retryable, but the
answer is still manual.

A `dev` profile was created and signed in during this session precisely so probe work could run
without evicting the live server. Make that the documented pattern: a named profile per consumer,
`gsearch login` per profile.

### 6. No drift telemetry

`RECON.md` lists long-run drift rate as unknown, and that is the input to deciding a maintenance
cadence. `schema_drift` already fires; nothing counts it. A line appended to a local log per
occurrence answers the question in a month for the cost of one `open(...,'a')`.

### 7. Headless still unverified

Unchanged from `RECON.md`. `GOOGLE_MCP_HEADLESS=1` exists and has never been tested against
`/sorry/`. Anything unattended depends on this.

---

## Rejected

- **Concurrency in `multi_search`.** Already argued in the module docstring and it still holds.
  Serial is the polite pattern and the non-flagged one.
- **Competing on latency.** Brave answers in ~1s over an API; a browser never will. Reach for
  Brave when the question is cheap and the answer is uncontested. This server is for when
  ranking quality, recency, region or operators actually decide the answer.
- **Making this the default search tool.** Three tools with different costs is the correct
  configuration. What is missing is not consolidation, it is a one-line routing rule in the
  web-browsing skill.

## Order of work

Done 2026-08-07:

1. ~~Delete `vertical` from the tool description~~ — superseded: the verticals were fixed instead.
2. ~~`date` as a structured field~~ — plus `total_matches` from `#result-stats`.
3. ~~Per-vertical extractors~~ — seven verticals, `shopping` deliberately unmapped.
4. ~~`login()` reports success~~ — watches for the session cookie and closes itself.
5. ~~Full filter surface~~ — `hour` freshness, `verbatim`, `strict_dates` custom range,
   and the `tbs` ordering trap pinned by tests.
6. ~~AI Mode as its own tool~~ — anchored on a real container hook, absence handled as a
   normal result.

Still open:

7. **`with_content` through the warmed browser** (large) — the one feature neither
   alternative can match on this box, because a logged-in Chrome reads pages `WebFetch`
   cannot. Unstarted.
8. **AI Overview on the standard SERP** (medium) — distinct from AI Mode. Confirmed present
   at `#m-x-content`, 1976 chars, on informational queries only; absent on technical and
   German ones. Would need the same opt-in treatment and the same distrust.
9. **Drift counter** (small) — `schema_drift` fires but nothing counts it, so the
   maintenance cadence is still guesswork.
10. **Headless still unverified** — unchanged from `RECON.md`.
11. **A second personalization arm** — still measured on one account only.
