"""The browser IS the transport. This owns it.

Why a dedicated Playwright profile rather than the user's own Chrome (the doctolib
argument, and it holds here for a second reason):

1. Chrome wraps its cookie store in App-Bound Encryption, so lifting the session out of
   it offline is unreliable. The answer is to not try -- own a separate profile.
2. The account sitting at Chrome's `/u/0` is frequently not the one you want searches
   attributed to. A personalized search tool built on "whoever was already signed in"
   silently runs every query as the wrong identity and files it in the wrong account's
   history. A dedicated profile makes the account one deliberate decision, recorded and
   readable via `session_status()`.

Transport findings, measured 2026-08-07 on a residential consumer IP in Germany
(full write-up in docs/RECON.md):

    bundled Chromium, --enable-automation present   -> /sorry/ on query 1
    bundled Chromium, --enable-automation stripped  -> OK
    real Chrome (channel), --enable-automation present -> OK
    any vehicle via a VPN exit                      -> CAPTCHA

`--enable-automation` is the tell. It survives playwright-stealth and it survives full
behavioural realism (curved mouse approach, per-keystroke jitter, organic homepage ->
type -> Enter, `navigator.webdriver` already False). None of that substituted for it.

Two consequences the rest of the code depends on:

- **Warming.** A fresh profile refuses a cold direct `/search?q=` navigation. One organic
  in-page search unlocks it, permanently for that profile. Warming is therefore a
  transport concern, and it is what makes the URL-param surface -- `start`, `tbs`, `tbm`,
  `pws`, every operator -- reachable at all.
- **Threading.** `sync_playwright()` raises inside an asyncio loop, and the MCP server
  runs one. Every Playwright touch is funnelled through a single dedicated worker thread
  that owns the browser for the process lifetime. Nothing outside that thread may hold a
  Playwright object.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .errors import AuthExpired, RateLimited, SchemaDrift

# Legacy location: repo-relative, from when this only ever ran out of a checkout.
_LEGACY_SESSION_ROOT = Path(__file__).resolve().parent.parent.parent / ".session"


def _default_session_root() -> Path:
    """Where the logged-in browser profiles live.

    This MUST NOT be derived from `__file__` once the package is installed rather than
    checked out. Under `uvx` the code lives in a cached environment keyed on the resolved
    requirement: it survives between runs, but it is rebuilt on every version bump and
    discarded by `uv cache clean`. A Google session stored there evaporates on upgrade --
    and the symptom is `AuthExpired`, which sends the reader looking at login rather than
    at packaging. So: a per-OS user data directory, stable across upgrades.

    The legacy repo-relative path still wins when it actually exists, which is true in a
    development checkout and false in every installed copy. That keeps an existing signed-in
    profile working without a config change, without pinning installed copies to a path
    inside their own venv.
    """
    if override := os.environ.get("GOOGLE_MCP_SESSION_ROOT"):
        return Path(override).expanduser()

    if _LEGACY_SESSION_ROOT.is_dir():
        return _LEGACY_SESSION_ROOT

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "gsearch-mcp" / "profiles"


SESSION_ROOT = _default_session_root()
DEFAULT_PROFILE = os.environ.get("GOOGLE_MCP_PROFILE", "default")

# Locale and timezone are a fingerprint, not a preference: they should look like the box
# the browser is actually running on. Hardcoding Berlin/en-US was right for one machine and
# wrong for a distributed one, so both are overridable and both default to the system.
LOCALE = os.environ.get("GOOGLE_MCP_LOCALE") or None
TIMEZONE = os.environ.get("GOOGLE_MCP_TIMEZONE") or None

# Headful by default and deliberately. Headless is a different fingerprint and was not the
# configuration the transport findings above were measured on. The window is parked far
# offscreen (apthunt's IS24 trick) so an unattended run does not take over the desktop.
# GOOGLE_MCP_HEADLESS=1 opts in to headless; verify against /sorry/ before trusting it.
HEADLESS = os.environ.get("GOOGLE_MCP_HEADLESS", "0") == "1"
OFFSCREEN = os.environ.get("GOOGLE_MCP_OFFSCREEN", "1") == "1"

# Off by default, and the default is the measured one. See `_new_playwright`: against real
# Chrome, playwright-stealth empties `navigator.userAgentData.brands`, and an empty brands
# array is a sharper bot tell than anything stealth hides. Kept as an opt-in because the
# bundled-Chromium fallback is a different vehicle and was never re-measured without it.
STEALTH = os.environ.get("GOOGLE_MCP_STEALTH", "0") == "1"

# One thread owns the browser. See module docstring.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gsearch-browser")
_lock = threading.Lock()

_pw_cm: Any = None
_pw: Any = None
_ctx: Any = None
_page: Any = None
_warmed = False

# Set from the context's own "close" event, which fires on the Playwright connection
# thread when the browser process dies. Read (and cleared) only on the browser thread.
# The alternative -- discovering the death from the first failed call -- is what this
# file did before 2026-08-23, and the failure mode was measured on this box: Chrome died
# under a long-lived server (every stdio session, and the shared http daemon), `_ctx`
# stayed non-None, and every call from then to process exit raised
# "Target page, context or browser has been closed". `status()` had it worst of all:
# `has_auth_session` swallows the dead-context exception, so a crashed browser reported
# itself as `signed_in: false` -- a logout that never happened, sending the reader to
# `gsearch login` for a profile that was signed in all along.
_ctx_dead = False


def _mark_dead(*_args) -> None:
    global _ctx_dead
    _ctx_dead = True


# Playwright's transport-level death rattles. The class check is the primary signal
# (TargetClosedError covers page/context/browser/driver death); the message markers
# cover equivalent errors raised before a class unwrap, and "connection closed" is the
# driver-process-died case, where no close event can ever fire.
_DEAD_BROWSER_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
)


def _looks_like_dead_browser(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _DEAD_BROWSER_MARKERS)


def in_browser_thread(fn: Callable, *args, **kwargs):
    """Run `fn` on the thread that owns Playwright. All browser access goes through here.

    If the browser (or the Playwright driver) died under the process, `fn` raises a
    closed-target error. Rather than letting that poison every later call, tear the
    browser down and run `fn` once more on a fresh launch. Read-only workloads make the
    retry safe; the alternative -- a server that stays dead until someone restarts the
    process -- was the 2026-08-23 outage on both the stdio seats and the shared daemon.
    """
    try:
        return _executor.submit(fn, *args, **kwargs).result()
    except Exception as exc:
        if not _looks_like_dead_browser(exc):
            raise
        _executor.submit(_teardown).result()
        return _executor.submit(fn, *args, **kwargs).result()


def profile_dir(name: str = DEFAULT_PROFILE) -> Path:
    d = SESSION_ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _launch_kwargs(profile: str, headless: bool) -> dict:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--password-store=basic",
    ]
    if not headless and OFFSCREEN:
        # Far offscreen rather than minimised: minimised windows get throttled timers,
        # which makes page loads flaky. Offscreen keeps it a normal foregroundable window.
        args.append("--window-position=-2400,-2400")
    kw = {
        "user_data_dir": str(profile_dir(profile)),
        "headless": headless,
        "channel": "chrome",
        "no_viewport": True,
        "args": args,
        # The load-bearing line. See module docstring.
        "ignore_default_args": ["--enable-automation"],
    }
    # Omitted rather than defaulted: Playwright then inherits the host's own locale and
    # timezone, which is the consistent fingerprint. Set them only to override.
    if LOCALE:
        kw["locale"] = LOCALE
    if TIMEZONE:
        kw["timezone_id"] = TIMEZONE
    return kw


def _new_playwright():
    """Start Playwright. Stealth is opt-in via GOOGLE_MCP_STEALTH=1.

    It used to be applied whenever installed, on the reasoning that stealth was
    belt-and-braces and every passing run had it. Measured against real Chrome 151 with
    playwright-stealth 2.0.3, that reasoning inverted. Same profile, same launch kwargs,
    only stealth differing:

        stealth off  navigator.userAgentData.brands =
                     [Not=A?Brand 99, Google Chrome 151, Chromium 151]
        stealth on   navigator.userAgentData.brands = []

    Real Chrome always populates brands, so an empty array is not concealment, it is a
    declaration. The automation-flag strip is the change that was actually measured to
    defeat the interstitial and it carries the load on its own.
    """
    cm = _playwright_cm()
    return cm, cm.__enter__()


def _playwright_cm():
    """The Playwright context manager, unentered, so callers can `with` it themselves."""
    from playwright.sync_api import sync_playwright

    if STEALTH:
        try:
            from playwright_stealth import Stealth

            return Stealth().use_sync(sync_playwright())
        except ImportError:
            pass
    return sync_playwright()


def _ensure_browser(profile: str = DEFAULT_PROFILE):
    """Lazily start the browser. MUST be called on the browser thread.

    Also the recovery point when the browser died under a long-lived process: the
    context's close event set `_ctx_dead`, so tear the corpse down and relaunch. The
    `_warmed` reset inside `_teardown` matters -- a fresh context refuses direct /search
    navigation until warmed again, so the flag must not survive the relaunch.
    """
    global _pw_cm, _pw, _ctx, _page, _ctx_dead
    if _ctx_dead:
        _teardown()
    if _ctx is not None:
        return _page

    _pw_cm, _pw = _new_playwright()
    try:
        _ctx = _pw.chromium.launch_persistent_context(**_launch_kwargs(profile, HEADLESS))
    except Exception as first:
        if _is_profile_lock(first):
            raise _locked(profile) from first
        # Chrome absent (pi, a server, a fresh box). Bundled Chromium passes on its own
        # once the automation flag is stripped -- measured, not assumed.
        kw = _launch_kwargs(profile, HEADLESS)
        kw.pop("channel", None)
        try:
            _ctx = _pw.chromium.launch_persistent_context(**kw)
        except Exception as second:
            if _is_profile_lock(second):
                raise _locked(profile) from second
            if _is_missing_browser(second):
                raise _missing_browser() from second
            raise

    _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    # Fires when the browser process goes away, which is the only reliable early
    # signal -- every later Playwright call would raise, but a probe-first design
    # pays on every call, and `status()` would swallow the exception and lie.
    _ctx.on("close", _mark_dead)
    _ctx_dead = False
    return _page


def _is_missing_browser(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "executable doesn't exist" in msg or "playwright install" in msg


def _missing_browser() -> SchemaDrift:
    """No usable browser: neither system Chrome nor a downloaded Chromium.

    Filed under `schema_drift` rather than a sixth error kind. The taxonomy is closed on
    purpose, and schema_drift's contract -- stop, the instrument is unusable, never retry,
    needs a human -- is exactly right here. What it must not be is `rate_limited`, which
    would have an agent politely backing off and retrying forever against a binary that is
    never going to appear on its own.
    """
    return SchemaDrift(
        "No usable browser. This tool drives a real browser, so it needs either Google "
        "Chrome installed system-wide (preferred: it is the configuration the transport "
        "was measured on) or Playwright's bundled Chromium downloaded once. Fix with:\n"
        "    uvx --from gsearch-mcp gsearch login\n"
        "which downloads Chromium if it is missing and then signs you in."
    )


def ensure_browser_binary(quiet: bool = False) -> bool:
    """Download Playwright's Chromium if no browser is available. Returns True if it ran.

    Only called from `login` -- a one-time, human-present, interactive command. Deliberately
    NOT called from the server: a ~150MB download inside a tool call is indistinguishable
    from a hang, and an agent that hits it has no way to report progress.
    """
    import subprocess

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        for get in (lambda: pw.chromium.executable_path, lambda: None):
            try:
                if get() and Path(get()).exists():
                    return False
            except Exception:
                break
        try:
            # A system Chrome makes the bundled download unnecessary.
            b = pw.chromium.launch(channel="chrome", headless=True)
            b.close()
            return False
        except Exception:
            pass

    if not quiet:
        print("No browser found. Downloading Playwright's Chromium (one time, ~150MB)...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    return True


def _is_profile_lock(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "singletonlock",
            "profile appears to be in use",
            "cannot create a new browser",
            "failed to create",
            # Real Chrome (channel="chrome") does not report the lock as an error at all:
            # it hands the URL to the instance that already owns the profile, prints this,
            # and exits 0. Playwright then fails on a browser that vanished, and without
            # this line the user gets a 60-line launch-args dump instead of "it's locked".
            # Hit 2026-08-08 the first time two agents shared a profile on this box.
            "opening in existing browser session",
        )
    )


def _locked(profile: str) -> RateLimited:
    """Two agents, one profile directory.

    Chromium takes an exclusive lock on a user-data-dir, so Claude Code and pi cannot both
    have this server's browser open on the same profile at once -- and with pi-dispatch
    drones that is a live collision, not a theoretical one. Classified `rate_limited`
    because the correct agent behaviour genuinely is back-off-and-retry: the other holder
    is short-lived. The message names the real fix so nobody debugs it as an auth problem.
    """
    return RateLimited(
        f"The browser profile {profile!r} is locked by another process (Chromium takes an "
        f"exclusive lock on a user-data-dir). Another agent -- Claude Code, pi, a dispatch "
        f"worker -- has it open. Retry shortly, or give this agent its own profile by "
        f"setting GOOGLE_MCP_PROFILE to a different name and running `gsearch login` for it."
    )


def _blocked(page) -> bool:
    return "/sorry/" in page.url


# Reject-all, per locale. Only the reject variant is ever clicked: this is a consent
# decision made on someone else's behalf, so the privacy-preserving branch is the only one
# the tool is allowed to take. If none of these match, we do NOT fall back to "click the
# first button in the dialog" -- on Google's consent page that button is often Accept all.
_CONSENT_REJECT_LABELS = (
    "Reject all",           # en
    "Alle ablehnen",        # de
    "Tout refuser",         # fr
    "Rechazar todo",        # es
    "Rifiuta tutto",        # it
    "Alles afwijzen",       # nl
    "Rejeitar tudo",        # pt
    "Odrzuć wszystko",      # pl
    "Tümünü reddet",        # tr
    "Avvisa alla",          # sv
    "Afvis alle",           # da
    "Avvis alle",           # no
    "Hylkää kaikki",        # fi
    "Odmítnout vše",        # cs
    "Odmietnuť všetko",     # sk
    "Az összes elutasítása",  # hu
    "Refuzați tot",         # ro
    "Απόρριψη όλων",        # el
    "Отхвърляне на всичко",  # bg
    "Odbij sve",            # hr
    "Zavrni vse",           # sl
    "Atmesti viską",        # lt
    "Noraidīt visu",        # lv
    "Lükka kõik tagasi",    # et
)


def _consent_present(page) -> bool:
    """Is the consent interstitial still up? Checked by URL and by the form Google posts."""
    try:
        if "consent.google." in page.url:
            return True
        return page.locator('form[action*="consent"], div[aria-modal="true"]').count() > 0
    except Exception:
        return False


def _dismiss_consent(page) -> None:
    """Click reject-all if the EU consent interstitial is up.

    Silent when there is no dialog, which is the non-EU case and most of the world. When a
    dialog IS up and no label matches, that is a stale extractor -- the same class of
    failure as SERP markup drift -- so it is reported as such rather than left to surface
    later as a search-box timeout, which reads as anti-bot and sends the reader to the
    wrong place entirely.
    """
    for label in _CONSENT_REJECT_LABELS:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count():
                btn.first.click(timeout=4000)
                time.sleep(random.uniform(1.2, 2.0))
                return
        except Exception:
            continue

    if _consent_present(page):
        raise SchemaDrift(
            "Google's consent interstitial is up and no known reject-all label matched, so "
            "the profile cannot be warmed. This is a locale gap, not a block: add this "
            "locale's reject-all label to _CONSENT_REJECT_LABELS in session.py. Workaround "
            "in the meantime: run `gsearch login` and dismiss the dialog by hand once, or "
            "set GOOGLE_MCP_LOCALE=en-US."
        )


def _warm(page) -> bool:
    """Unlock direct /search navigation on this profile. Idempotent, cheap after the first."""
    page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(1.5, 2.5))

    _dismiss_consent(page)

    try:
        box = page.locator('textarea[name="q"], input[name="q"]').first
        box.wait_for(state="visible", timeout=15000)
        box.click()
        time.sleep(random.uniform(0.25, 0.6))
        for ch in "weather":
            page.keyboard.type(ch, delay=0)
            time.sleep(random.uniform(0.08, 0.22))
        time.sleep(random.uniform(0.5, 1.0))
        page.keyboard.press("Enter")
        page.wait_for_url("**/search**", timeout=25000)
        time.sleep(random.uniform(1.5, 2.5))
    except Exception:
        return False

    if _blocked(page):
        raise RateLimited("Google served /sorry/ while warming the profile.")
    return True


def get_page(profile: str = DEFAULT_PROFILE):
    """The warmed page. MUST be called on the browser thread."""
    global _warmed
    page = _ensure_browser(profile)
    if not _warmed:
        if not _warm(page):
            raise RateLimited(
                "Could not warm the profile; direct search URLs will be refused. "
                "Usually a transient block -- wait a few minutes and retry."
            )
        _warmed = True
    return page


# Presence of any of these on .google.com IS what signed-in means. A cookie is checked
# rather than a rendered string because the page is localised and the string is not: the
# avatar reads "Google Account" in English and "Google Hesabi" in Turkish, so the old
# English-only selector reported a signed-in Turkish profile as signed out. That was not
# merely a cosmetic status bug - `login()` derived its own success from the same selector,
# so a sign-in that completed, cookie and all, announced itself as a failure and sent the
# caller back to re-run a login that had already worked.
AUTH_COOKIE_NAMES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "SAPISID")


def has_auth_session(ctx) -> bool:
    """Is there a live Google session in this context. Locale-independent, no page load."""
    try:
        names = {c.get("name") for c in ctx.cookies("https://www.google.com/")}
    except Exception:
        return False
    return any(n in names for n in AUTH_COOKIE_NAMES)


def read_account(page) -> str | None:
    """Which address this profile is signed in as, read off the page rather than assumed.

    Advisory only, and never the signed-in test. Scans every aria-label for an address
    instead of matching one English phrase, so it survives the interface language. A None
    means "could not read the address", which is not the same as "not signed in" -
    `has_auth_session` is what answers that.
    """
    import re

    try:
        page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.0)
        labels = page.eval_on_selector_all(
            "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
        )
        for lab in labels or []:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", lab or "")
            if m:
                return m.group(0)
    except Exception:
        pass
    return None


def status(profile: str = DEFAULT_PROFILE) -> dict:
    def _work():
        page = _ensure_browser(profile)
        signed_in = has_auth_session(page.context)
        # Only worth a page load when there is a session to name. Skipping it when signed
        # out also keeps `status()` from navigating on every call.
        account = read_account(page) if signed_in else None
        return {
            "profile": profile,
            "profile_dir": str(profile_dir(profile)),
            "signed_in": signed_in,
            "account": account,
            "headless": HEADLESS,
            "warmed": _warmed,
        }

    with _lock:
        return in_browser_thread(_work)


def require_account(profile: str = DEFAULT_PROFILE) -> str | None:
    """The signed-in address, or None when there is a session whose address is unreadable.

    Returning None on a live session is deliberate: the gate is the session, not whether
    the address could be scraped off a localised page.
    """
    st = status(profile)
    if not st["signed_in"]:
        raise AuthExpired(
            "No live Google session in the dedicated profile. "
            "Run `gsearch login` in a terminal and sign in once."
        )
    return st["account"]


def _teardown() -> None:
    """Drop the browser and playwright handles. MUST run on the browser thread.

    Takes no lock, on purpose: it is also the recovery path called from inside
    `in_browser_thread`, where the caller may already hold `_lock` (see `status`) and a
    lock-taking teardown would deadlock the single browser thread against its caller.
    """
    global _pw_cm, _pw, _ctx, _page, _warmed, _ctx_dead
    for closer in (lambda: _ctx and _ctx.close(), lambda: _pw_cm and _pw_cm.__exit__(None, None, None)):
        try:
            closer()
        except Exception:
            pass
    _pw_cm = _pw = _ctx = _page = None
    _warmed = False
    _ctx_dead = False


def shutdown() -> None:
    with _lock:
        in_browser_thread(_teardown)


def login(profile: str = DEFAULT_PROFILE, timeout: float = 600.0) -> dict:
    """Interactive by design. Opens a real window; closes itself once you are signed in.

    Never call this from the MCP server. A tool that blocks for minutes, and whose failure
    looks like a timeout instead of a missing login, gets debugged in the wrong place --
    the doctolib lesson, and it applies unchanged here.

    The first version waited on `page.wait_for_event("close")` and nothing else, so a
    successful sign-in produced no acknowledgement at all: the window just sat there until
    the human guessed they were done. Hit for real 2026-08-07. It now watches for the
    session cookie, confirms which account landed, and closes -- so the success case ends
    on its own and the return value says who you are, rather than only where the profile is.

    Closing the window by hand still works and still returns; it is the fallback path, not
    the happy one.
    """
    from playwright.sync_api import sync_playwright

    # The one place a browser download is allowed to happen: interactive, human present,
    # once per box. Everywhere else a missing browser is an error with instructions.
    ensure_browser_binary()

    # Same stealth policy as `_new_playwright`, and it matters more here: signing in on a
    # browser with an empty Client Hints brands array is how an account gets flagged before
    # it has issued a single search.
    cm = _playwright_cm()

    account = None
    signed_in = False
    with cm as pw:
        kw = _launch_kwargs(profile, headless=False)
        kw["args"] = [a for a in kw["args"] if not a.startswith("--window-position")]
        try:
            ctx = pw.chromium.launch_persistent_context(**kw)
        except Exception as first:
            if _is_profile_lock(first):
                raise _locked(profile) from first
            kw.pop("channel", None)
            ctx = pw.chromium.launch_persistent_context(**kw)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded")

        # `SID` only lands after the whole flow completes, 2FA included, so it is a
        # truthful "done" signal rather than "the form was submitted".
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if page.is_closed():
                    break  # closed by hand; fall through and report whatever we can
                cookies = ctx.cookies("https://www.google.com/")
            except Exception:
                break
            if any(c.get("name") in AUTH_COOKIE_NAMES for c in cookies):
                signed_in = True
                time.sleep(2.0)  # let the redirect settle before reading the account
                try:
                    account = read_account(page)
                except Exception:
                    account = None
                break
            time.sleep(2.0)

        # The loop also exits when the window is closed by hand or the deadline passes,
        # and either can happen AFTER a sign-in has already landed. Re-check the cookies
        # rather than reporting failure on the strength of how the loop ended.
        if not signed_in:
            signed_in = has_auth_session(ctx)

        try:
            ctx.close()
        except Exception:
            pass

    return {
        "profile": profile,
        "profile_dir": str(profile_dir(profile)),
        "signed_in": signed_in,
        "account": account,
    }
