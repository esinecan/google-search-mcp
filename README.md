# google-search-mcp

MCP tools over Google Search, through a dedicated logged-in browser profile. Personalized
organic results, ads stripped, the full advanced-operator surface, and pagination.

Published on PyPI as **`gsearch-mcp`** (`google-search-mcp` was already taken, by an
unrelated Custom Search API wrapper). Import package and repo keep the longer name.

Three-layer shape: all site knowledge in `client.py`, a CLI that mirrors the tools 1:1 as the
debugging surface, and a thin MCP wrapper.

## Why this exists

No official Google Search API serves this intent. The Custom Search JSON API returns results
from a *configured subset* of the web with no personalization, is **closed to new customers**,
and **sunsets 1 Jan 2027**. That is a §0 Q1 negative on all three sub-questions.

What you get that a keyword search API does not:

- **The advanced-operator surface** — `site:`, `filetype:`, exact phrase, exclusions,
  `before:`/`after:` bounds, freshness windows down to the **past hour**, **verbatim**
  (no synonyms or stemming), every Google vertical, region and language bias. All
  server-side, all free once the transport works. This is the strongest reason to use it;
  no keyword API has an equivalent.
- **Better sources on technical queries.** Measured head-to-head against the harness
  `WebSearch` and the Brave API on `MCP stateless server migration`: this returned the
  Google and Cloudflare engineering blogs, the spec's own GitHub issue and the Spring
  docs, where both alternatives returned a page of SEO blogspam paraphrasing the same
  announcement. Full comparison in `docs/ROADMAP.md`.
- **Personalized ranking.** Measured at **~3.4× the run-to-run noise floor**: disabling it moved
  roughly a third of the top-10. Qualitatively it resolves ambiguous technical queries toward the
  domain sense — `spring` returns Spring Framework rather than the film, `mcp server` returns
  Cloudflare's engineering blog rather than a content farm. Full method and caveats in
  `~/dev/google-search-recon/RESULT.md`.
- **Ads stripped** structurally, before the agent sees them.

## Setup

No checkout needed. Sign in once, then point your MCP client at it.

```
uvx --from gsearch-mcp gsearch login
```

`login` opens a window; it downloads a browser first if you have neither Chrome nor
Playwright's Chromium. Sign in to the account you want searches personalized to. The window
closes itself once the sign-in lands. **This profile is separate from your Chrome**, so
Chrome's `/u/0` default does not apply — whatever you sign in as here is what gets used,
deliberately. Check with `uvx --from gsearch-mcp gsearch status`, which reads the account off
the page rather than assuming it.

Signed out still works; it just returns the neutral, unpersonalized view.

Then, in your MCP client's config:

```json
{
  "mcpServers": {
    "google-search": {
      "type": "stdio",
      "command": "uvx",
      "args": ["gsearch-mcp"]
    }
  }
}
```

That entry is the same on every OS. Nothing in it is a path.

### Bring your own account

There is no API key. The credential is a Google account you sign into once, and the tool
uses whatever that is — so results are personalized to *your* history, not to a service
account's. Run `gsearch login` again to switch accounts.

Several agents on one box each want their own profile, because Chromium takes an exclusive
lock on a profile directory and two agents sharing one will collide:

```
GOOGLE_MCP_PROFILE=agent  uvx --from gsearch-mcp gsearch login
```

then set `"env": {"GOOGLE_MCP_PROFILE": "agent"}` on that client's server entry.

### Environment

| var | default | what it does |
|---|---|---|
| `GOOGLE_MCP_PROFILE` | `default` | which signed-in profile to use; one per agent |
| `GOOGLE_MCP_SESSION_ROOT` | per-OS user data dir | where profiles live |
| `GOOGLE_MCP_LOCALE` | the host's | browser locale, e.g. `en-US` |
| `GOOGLE_MCP_TIMEZONE` | the host's | browser timezone, e.g. `Europe/Berlin` |
| `GOOGLE_MCP_HEADLESS` | `0` | headless is a different fingerprint; verify against `/sorry/` before trusting it |
| `GOOGLE_MCP_OFFSCREEN` | `1` | park the window offscreen instead of taking over the desktop |

Profiles default to `%LOCALAPPDATA%\gsearch-mcp\profiles` on Windows,
`~/Library/Application Support/gsearch-mcp/profiles` on macOS, and
`$XDG_DATA_HOME/gsearch-mcp/profiles` on Linux. They are deliberately **not** stored next to
the code: under `uvx` that location is rebuilt on every version bump, and a session kept
there would vanish on upgrade and report itself as a login failure.

## Use

```
uvx --from gsearch-mcp gsearch status
uvx --from gsearch-mcp gsearch search "model context protocol" --site modelcontextprotocol.io
uvx --from gsearch-mcp gsearch search "agent harness" --freshness week --no-personalized
uvx --from gsearch-mcp gsearch multi-search "mcp spec" "mcp security" "mcp transports"
```

As an MCP server (stdio): `gsearch-mcp`.

### From a checkout

```
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/Scripts/python.exe -m google_search_mcp.cli login
```

A checkout keeps its profiles in a repo-local `.session/` if that directory already exists,
so an existing signed-in profile keeps working after upgrading to a packaged install.

| tool | what it is for |
|---|---|
| `google_search` | one query, ads stripped, full operator surface, every vertical |
| `google_multi_search` | several queries through one warmed browser — **the compound tool** |
| `google_fetch` | read pages as markdown through the logged-in browser |
| `google_ai_mode` | Google's AI Mode answer + citations. **Unreliable — see below** |
| `google_session_status` | whether the profile is signed in, and as whom |

### Reading pages

`google_search(..., with_content=True)` attaches the top results' article text as markdown,
and `google_fetch` does it for arbitrary URLs. Both go through the same warmed, logged-in
Chrome, which is the point: **it reads JS-rendered apps, soft paywalls and cookie-walled
articles that a plain HTTP fetch cannot.** For a static public page an ordinary fetch is
cheaper and you should use one.

The browser renders; [trafilatura](https://trafilatura.readthedocs.io/) strips the
boilerplate. Nav, footers, cookie banners and related-story rails go; headings, lists,
tables and code fences survive. Markdown rather than plain text because on a technical page
the structure carries most of the meaning, and it still costs far fewer tokens than the HTML.

Measured 2026-08-07, whole article against the raw DOM it came from:

| page | raw HTML | markdown | |
|---|---|---|---|
| `blog.cloudflare.com/mcp-v2` | 615,709 | 18,093 | **2.9%** |
| `modelcontextprotocol.io` spec | 294,995 | 4,708 | **1.6%** |

Roughly a 30–60× reduction before truncation even applies.

**There is no cached copy to read instead.** Google retired its page cache on 2 Feb 2024 —
`cache:` and `webcache.googleusercontent.com` are both gone. The Wayback Machine is the only
general cache left and it is the wrong source here: it would serve months-old text to a tool
whose entire edge is recency. Live fetch is fresher *and* more capable.

Bounded by default (3 results, 2000 chars each, 5 URLs per `google_fetch`) because unbounded
this is a crawler, and each page is a real load — budget a few seconds per result.

### Verticals

Each one renders differently and each names its own extractor. Counts below are live,
2026-08-07, on the same query.

| `vertical` | `udm` | anchor | notes |
|---|---|---|---|
| `web` | — | `a h3` | the default SERP, rich blocks included |
| `web_only` | `web` | `a h3` | the "Web" tab. **Cleanest for research** — 17 external anchors against 56 on the default SERP |
| `news` | `12` | `[role=heading]` | every result carries a date |
| `videos` | `7` | `a h3` | |
| `short_videos` | `39` | `[role=heading]` | |
| `books` | `36` | `a h3` | results are google-hosted by nature |
| `images` | `2` | `a:has(img)` | single page; title comes from the anchor, not `alt` |

`shopping` (`udm=28`) is **deliberately unmapped**: it renders product cards with no
external anchors and no headings, so there is nothing for a link-and-snippet projection to
return. Shipping it would mean advertising a vertical that always yields zero.

**Why not one selector for all of them.** Broadening the anchor to `[role="heading"]`
everywhere looks like the obvious fix and quietly breaks the ad guarantee. On
`hotel berlin buchen`, `a h3` found 10 results with 0 inside an ad container, while
`a [role="heading"]` found 2 — **both of them ads**. It fails invisibly, because on a
technical query the same selector returns 7 with no ads at all. So web keeps `a h3` and
its structural guarantee, and on role-anchored verticals the ad-container filter is
load-bearing rather than defence-in-depth.

### AI Mode

`google_ai_mode` returns Google's generated answer and its citations. **Treat the answer as
a lead, never as a fact.** It is hit or miss, confidently wrong in the same voice it is
right, and not authoritative even about Google's own products — which is the trap, since
those are the queries where it reads most credible. The citations are the part worth
keeping; read them and believe those.

Absence is normal, not a failure: it is not offered for every query, region or account, and
returns `available: false` with a reason rather than an error. Slower than a search — the
answer streams and is polled until it stops growing (~4s typical, 20s cap).

## Cost model

The first call in a process launches a browser and warms the profile (~10s). Each page after
that is one throttled load (4–7s). Pages are **10 results** and depth costs a round trip —
Google stopped honouring `num=` on 11 Sept 2025. Prefer `google_multi_search` over several
`google_search` calls; it amortises the launch across the set.

`google_ai_mode` costs more: the answer streams, so it is polled until the text stops
growing — typically ~4–8s on top of the page load, capped at 20s.

**Profiles take an exclusive lock.** Chromium locks a user-data-dir, so Claude Code, pi and
a dispatch worker cannot share one. Give each consumer its own: set `GOOGLE_MCP_PROFILE` and
run `cli login` once for that name. A collision surfaces as `rate_limited` with a message
naming the real cause, because the correct response genuinely is back off and retry.

## Transport, and the one flag that matters

`ignore_default_args=["--enable-automation"]`. Measured 2026-08-07 on a residential IP:

| vehicle | result |
|---|---|
| bundled Chromium, flag present | `/sorry/` on query 1 |
| bundled Chromium, flag stripped | OK |
| real Chrome (`channel`), flag present | OK |
| any vehicle over a VPN | CAPTCHA |

`playwright-stealth` and full behavioural realism did not substitute for it. A **residential
exit IP is required** regardless of vehicle. Chrome does not need to be installed — bundled
Chromium passes on its own with the flag stripped, so this runs on pi or any other box.

Environment: `GOOGLE_MCP_PROFILE` (default `default`), `GOOGLE_MCP_HEADLESS` (default `0`),
`GOOGLE_MCP_OFFSCREEN` (default `1`, parks the window at -2400,-2400).

## Errors

Failing results carry a `kind` field (paradigm §3.1): `auth_expired` (sign in, never retry),
`schema_drift` (extractor stale, never retry, flag it), `rate_limited` (back off),
`bad_argument` (fix the call, never retry unchanged).

`empty` is **not** an error — a query matching nothing returns `count: 0` with no `kind`.
Neither is an absent AI Mode answer, which returns `available: false` with a reason.

`schema_drift` is the expected long-run failure mode here: Google's SERP markup is obfuscated
and rotates by design. When it fires, the extractor in `client.py::EXTRACT_JS` needs updating,
and `docs/API.md`'s field-semantics section records the traps that make that quick.

## Legal

Automated querying of `google.com/search` is contrary to Google's ToS. Recorded as an owner
decision in `docs/API.md`: read-only, sequential, throttled, personal-scale, no republication.
The session profile is gitignored and is never copied, attached, or handed to another agent.
