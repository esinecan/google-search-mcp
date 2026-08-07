"""The browser IS the transport. This owns it.

Why a dedicated Playwright profile rather than the user's own Chrome (the doctolib
argument, and it holds here for a second reason):

1. Chrome wraps its cookie store in App-Bound Encryption, so lifting the session out of
   it offline is unreliable. The answer is to not try -- own a separate profile.
2. On this box Chrome's `/u/0` is not necessarily the account you want searches attributed to. A
   personalized search tool built on "whoever was already signed in" would silently run
   every query as the wrong identity and file it in the wrong account's history. A
   dedicated profile makes the account one deliberate decision, recorded and readable
   via `session_status()`.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .errors import AuthExpired, RateLimited

SESSION_ROOT = Path(__file__).resolve().parent.parent.parent / ".session"
DEFAULT_PROFILE = os.environ.get("GOOGLE_MCP_PROFILE", "default")

# Headful by default and deliberately. Headless is a different fingerprint and was not the
# configuration the transport findings above were measured on. The window is parked far
# offscreen (apthunt's IS24 trick) so an unattended run does not take over the desktop.
# GOOGLE_MCP_HEADLESS=1 opts in to headless; verify against /sorry/ before trusting it.
HEADLESS = os.environ.get("GOOGLE_MCP_HEADLESS", "0") == "1"
OFFSCREEN = os.environ.get("GOOGLE_MCP_OFFSCREEN", "1") == "1"

# One thread owns the browser. See module docstring.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gsearch-browser")
_lock = threading.Lock()

_pw_cm: Any = None
_pw: Any = None
_ctx: Any = None
_page: Any = None
_warmed = False


def in_browser_thread(fn: Callable, *args, **kwargs):
    """Run `fn` on the thread that owns Playwright. All browser access goes through here."""
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
    return {
        "user_data_dir": str(profile_dir(profile)),
        "headless": headless,
        "channel": "chrome",
        "locale": "en-US",
        "timezone_id": "Europe/Berlin",
        "no_viewport": True,
        "args": args,
        # The load-bearing line. See module docstring.
        "ignore_default_args": ["--enable-automation"],
    }


def _new_playwright():
    """Start Playwright, with stealth applied when it is installed.

    Stealth is belt-and-braces, not the fix: the automation flag was the measured tell and
    stealth alone did not defeat it. Kept because every passing run had it applied, and a
    search tool is the wrong place to find out which of two changes mattered.
    """
    from playwright.sync_api import sync_playwright

    try:
        from playwright_stealth import Stealth

        cm = Stealth().use_sync(sync_playwright())
    except ImportError:
        cm = sync_playwright()
    return cm, cm.__enter__()


def _ensure_browser(profile: str = DEFAULT_PROFILE):
    """Lazily start the browser. MUST be called on the browser thread."""
    global _pw_cm, _pw, _ctx, _page
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
            raise

    _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    return _page


def _is_profile_lock(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("singletonlock", "profile appears to be in use", "cannot create a new browser", "failed to create")
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


def _warm(page) -> bool:
    """Unlock direct /search navigation on this profile. Idempotent, cheap after the first."""
    page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(1.5, 2.5))

    for label in ("Reject all", "Alle ablehnen"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=4000)
                time.sleep(random.uniform(1.2, 2.0))
                break
        except Exception:
            continue

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


def read_account(page) -> str | None:
    """Which account this profile is signed in as, read off the page rather than assumed."""
    try:
        page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.0)
        el = page.query_selector('a[aria-label*="@"], [aria-label*="Google Account"]')
        if el:
            import re

            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", el.get_attribute("aria-label") or "")
            if m:
                return m.group(0)
    except Exception:
        pass
    return None


def status(profile: str = DEFAULT_PROFILE) -> dict:
    def _work():
        page = _ensure_browser(profile)
        account = read_account(page)
        return {
            "profile": profile,
            "profile_dir": str(profile_dir(profile)),
            "signed_in": bool(account),
            "account": account,
            "headless": HEADLESS,
            "warmed": _warmed,
        }

    with _lock:
        return in_browser_thread(_work)


def require_account(profile: str = DEFAULT_PROFILE) -> str:
    st = status(profile)
    if not st["signed_in"]:
        raise AuthExpired(
            "No live Google session in the dedicated profile. "
            "Run `gsearch login` in a terminal and sign in once."
        )
    return st["account"]


def shutdown() -> None:
    def _work():
        global _pw_cm, _pw, _ctx, _page, _warmed
        for closer in (lambda: _ctx and _ctx.close(), lambda: _pw_cm and _pw_cm.__exit__(None, None, None)):
            try:
                closer()
            except Exception:
                pass
        _pw_cm = _pw = _ctx = _page = None
        _warmed = False

    with _lock:
        in_browser_thread(_work)


def login(profile: str = DEFAULT_PROFILE) -> dict:
    """Interactive by design. Opens a real window and blocks until it is closed.

    Never call this from the MCP server. A tool that blocks for minutes, and whose failure
    looks like a timeout instead of a missing login, gets debugged in the wrong place --
    the doctolib lesson, and it applies unchanged here.
    """
    from playwright.sync_api import sync_playwright

    try:
        from playwright_stealth import Stealth

        cm = Stealth().use_sync(sync_playwright())
    except ImportError:
        cm = sync_playwright()

    with cm as pw:
        kw = _launch_kwargs(profile, headless=False)
        kw["args"] = [a for a in kw["args"] if not a.startswith("--window-position")]
        try:
            ctx = pw.chromium.launch_persistent_context(**kw)
        except Exception:
            kw.pop("channel", None)
            ctx = pw.chromium.launch_persistent_context(**kw)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass

    return {"profile": profile, "profile_dir": str(profile_dir(profile))}
