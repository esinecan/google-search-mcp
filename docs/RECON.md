# Recon log — what was tried, and what was rejected

Companion to `API.md` (the contract). This is the part that stops the next person
re-deriving five dead ends. Dated 2026-08-07, residential consumer IP in Germany.

Full measurement harness and captures: `~/dev/google-search-recon/` (`RESULT.md`, `out/`).

---

## Triage (paradigm §0)

**Q1 — official API?** Negative on all three sub-questions:

| sub-question | answer |
|---|---|
| Does one exist? | Yes — Custom Search JSON API |
| Does it serve the intent? | **No.** Returns a *configured subset* of the web, no personalization. The intent was "the whole index, as me". |
| Accepting new customers? | **No.** Explicitly closed. |
| Sunset date? | **1 Jan 2027.** Google steers to Vertex AI Search. |

Pricing when it was open: 100 queries/day free, $5/1k, 10k/day cap.

**Q2 — server round-trip?** Yes, emphatically. `q`, `start`, `tbs`, `tbm`, `cr`/`gl`, `pws`
and every query operator are server-side.

**Q3 — blast radius?** The Google account. Mitigated by a dedicated profile rather than the
user's Chrome, which also resolves the sharper problem: local Chrome's `/u/0` on this box is
not necessarily the account you want, so "whoever was signed in" would have run every query as
the wrong identity and filed it in the wrong history. The profile makes the account a
deliberate, readable decision.

## Vehicle — five attempts

| # | vehicle | outcome |
|---|---|---|
| 1 | bundled Chromium, headful, real UA, direct `/search?q=` | `/sorry/` on query 1 |
| 2 | + homepage → type → Enter (organic flow) | inconclusive; Enter did not submit |
| 3 | + `playwright-stealth`, curved mouse approach, per-keystroke jitter | `/sorry/` — and the URL carried `sca_esv`, proving the query *did* submit organically |
| 4 | real Chrome via `channel="chrome"` | **OK**, 6 organic + warmed direct-navigation OK |
| 5 | bundled Chromium + `ignore_default_args=["--enable-automation"]` | **OK** |

**Rejected: "it's the browser build."** Attempts 1–4 confounded the build with the flag.
Attempt 5 isolated it: bundled Chromium passes once the automation flag is stripped, so the
tool does not require Chrome to be installed and stays portable to pi.

**Rejected: stealth and behavioural realism as the fix.** Both were present in attempt 3 and
neither helped. `navigator.webdriver` was already `False` throughout. Do not spend time here.

**Rejected: VPN.** Tested at the user's suggestion mid-run. VPN exits CAPTCHA on every
vehicle; residential passed identical code. Turning a VPN on makes this strictly worse.

**Found by grep, not by reasoning.** `~/dev/notebooklm-py/src/notebooklm/cli/services/playwright_login.py`
had `ignore_default_args=["--enable-automation"]` the entire time. Checking the box for a
solved launch is now PASS 0 step 1 in the capture skill.

## Personalization — is it worth it?

Three arms plus a control, same profile, same IP, ~2 minutes apart. Set overlap (Jaccard) of
top-10 organic URLs:

| comparison | mean jaccard | moved | isolates |
|---|---|---|---|
| `authed` vs `authed-repeat` | **0.907** | 4/9 | noise floor, nothing changed |
| `authed` vs `authed-pws0` | **0.683** | 9/9 | personalization, same session |
| `clean` vs `authed` | 0.672 | 9/9 | logged-out vs signed-in (confounded) |

Personalization costs ~0.32 of overlap against a ~0.09 noise floor: **~3.4× the flux**.
Qualitatively it resolves ambiguous technical queries toward the domain sense — `spring` →
Spring Framework rather than the film; `mcp server` → Cloudflare's engineering blog rather
than a German content farm.

**The control arm is the load-bearing part.** Without it 0.683 is uninterpretable. It also
validated the instrument: the extractor was known-lossy, and scoring 0.907 on an unchanged
comparison proved it at least *stable*.

**One near-miss worth recording.** The first comparison globbed `authed-*.json`, which also
matches `authed-pws0-*.json`, diffed a capture against itself, and reported a flawless 1.000
with a confident "no meaningful difference". Caught only because perfect agreement across
nine live SERPs is not plausible. Fixture selection is now on a field *inside* the file.

## Parser traps found while building

- **Ads carry no `<h3>`.** Anchoring extraction on `a h3` excludes them structurally rather
  than by filter. The first build also *counted* ads on the organic path, so it always
  reported 0 — which read as "no ads on this query" but meant "the counter is unreachable".
  Ads are now counted at `#tads`/`#tadsb`/`#bottomads` by distinct advertiser origin.
  Those containers are emitted **even when empty**; only their content length is evidence.
- **The description sits ~4 DOM levels above the anchor.** The anchor's own subtree already
  holds title + breadcrumb (~70–105 chars), so an *absolute* length threshold for "found the
  block" trips at level 0 and yields empty snippets. The test must be relative to the
  anchor's own text length.
- **Google appends its own affordances into the description text**, not as separate lines:
  "Read more", "Missing: x", "Show results with: x". They survive line-level filtering and
  are not end-anchored, so they are stripped wherever they appear.

## Known-unverified

- **Headless.** The transport findings were all measured headful. `GOOGLE_MCP_HEADLESS=1`
  exists but has not been tested against `/sorry/`. Do not assume it is safe.
- **A second account.** Personalization was measured on one account only. A second history
  would strengthen the effect size considerably; one `login` plus two arms.
- **Long-run drift rate.** No data yet on how often Google's markup rotates in practice.
  `schema_drift` will report it; that log is the input to deciding a maintenance cadence.
