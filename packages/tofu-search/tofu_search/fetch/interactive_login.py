"""tofu_search.fetch.interactive_login — Headful login → cookie capture.

Drives a *non-headless* Chromium so the user can log in to a site (scan a QR
code, enter a password, …) once; we then capture the resulting session
cookies via ``storage_state``. The captured cookies are returned to the
caller, who can persist them into whatever auth-source store the host uses.

Why a separate, short-lived browser (not the pool)? The fetch pool launches
``headless=True`` and is shared across fetches — it can't be shown to the
user. Interactive login is a rare, user-initiated, one-at-a-time action.

Availability: requires a display on the host AND a Chromium build that can
open a window. The latter is NOT implied by "Playwright is installed": Tofu's
installers deliberately fetch ``--only-shell`` (-60% download), and
chrome-headless-shell has NO headed mode — it is a separate, smaller binary,
not a flag. Launching headed against a shell-only install fails with
"Executable doesn't exist at .../chromium-<rev>/chrome-linux64/chrome" plus
Playwright's "just installed or updated" banner, which sends the reader off to
reinstall something already correctly installed. So we check for a headed-
capable binary up front and return a truthful, actionable reason instead.
Set ``TOFU_INTERACTIVE_LOGIN=0`` to force-disable.
"""

from __future__ import annotations

import os
import threading
import time

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['capture_login_cookies', 'is_interactive_login_available']

# Only one interactive login at a time — a visible browser is inherently a
# single-user, single-window action.
_login_lock = threading.Lock()

# Cookie names that signal a completed login per site.
_LOGIN_COOKIE_HINTS = {
    'xiaohongshu.com': ('web_session', 'customerClientId', 'galaxy_creator_session_id'),
}


#: Actionable recovery for a shell-only install. Names the exact command,
#: because the raw Playwright error names the WRONG one (it tells you to run
#: `playwright install`, which is what produced this state).
_HEADED_MISSING_MSG = (
    'Interactive login needs a Chromium that can open a window, but only the '
    'headless shell is installed (chrome-headless-shell has no headed mode). '
    'Install the full build with:  python -m playwright install chromium'
)


def _headed_chromium_path() -> str:
    """Path to a Chromium that can open a VISIBLE window, or '' if none.

    Delegates to the Tofu host's ``chromium_env`` when importable (single
    source of truth, guard-covered there). tofu_search ships independently, so
    the fallback repeats only the minimal rule: a build whose binary is named
    ``chrome-headless-shell`` cannot go headed; anything else can.
    """
    try:
        from chromium_env import headed_chromium_executable
        return headed_chromium_executable()
    except Exception as e:
        logger.debug('[Login] host chromium_env unavailable: %s', e)
    import glob
    import shutil
    roots = []
    env_root = (os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or '').strip()
    if env_root and env_root != '0':
        roots.append(env_root)
    else:
        home = os.path.expanduser('~')
        roots += [os.path.join(home, '.cache', 'ms-playwright'),
                  os.path.join(home, 'Library', 'Caches', 'ms-playwright'),
                  os.path.join(home, 'AppData', 'Local', 'ms-playwright')]
    for root in roots:
        for pat in ('chromium-*/chrome-linux64/chrome',
                    'chromium-*/chrome-linux/chrome',
                    'chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium',
                    'chromium-*/chrome-win*/chrome.exe'):
            for hit in sorted(glob.glob(os.path.join(root, pat)), reverse=True):
                if os.path.isfile(hit):
                    return hit
    for name in ('google-chrome', 'google-chrome-stable', 'chromium',
                 'chromium-browser', 'microsoft-edge'):
        found = shutil.which(name)
        if found:
            return found
    return ''


def is_interactive_login_available() -> bool:
    """Best-effort check: is headful login plausibly usable here?

    Checks for a HEADED-CAPABLE binary, not merely that Playwright imports.
    The old check (``HAS_PLAYWRIGHT`` alone) was constitutionally incapable of
    failing for the reason that actually breaks this feature on a shell-only
    install, so it reported the feature available and then died at launch.
    """
    if os.environ.get('TOFU_INTERACTIVE_LOGIN', '').strip() == '0':
        return False
    try:
        from tofu_search.fetch.utils import HAS_PLAYWRIGHT
        if not HAS_PLAYWRIGHT:
            return False
    except Exception as e:
        logger.debug('[Login] availability check failed: %s', e)
        return False
    return bool(_headed_chromium_path())


def capture_login_cookies(domain: str, login_url: str, timeout_s: int = 180) -> dict:
    """Open a visible browser at ``login_url`` and capture cookies on login.

    Blocks (up to ``timeout_s``) while the user signs in. On success returns
    ``{'ok': True, 'cookie_count': N, 'cookies': [...]}`` — the caller decides
    where to persist them (e.g. via an AuthSourceProvider-backed store).

    On failure returns ``{'ok': False, 'reason': ..., 'error': ...}`` where
    ``reason`` is ``'unavailable'`` / ``'timeout'`` / ``'busy'`` / ``'error'``.
    """
    if not is_interactive_login_available():
        # Distinguish the two causes: "disabled / no Playwright" is a different
        # fix from "only the headless shell is installed", and collapsing them
        # into one message is what made this failure unreadable.
        if os.environ.get('TOFU_INTERACTIVE_LOGIN', '').strip() == '0':
            return {'ok': False, 'reason': 'unavailable',
                    'error': 'Interactive login disabled (TOFU_INTERACTIVE_LOGIN=0)'}
        try:
            from tofu_search.fetch.utils import HAS_PLAYWRIGHT
        except Exception:
            HAS_PLAYWRIGHT = False
        if not HAS_PLAYWRIGHT:
            return {'ok': False, 'reason': 'unavailable',
                    'error': 'Playwright is not installed'}
        return {'ok': False, 'reason': 'headed_unavailable',
                'error': _HEADED_MISSING_MSG}

    if not _login_lock.acquire(blocking=False):
        return {'ok': False, 'reason': 'busy',
                'error': 'Another interactive login is already in progress'}
    try:
        return _run_capture(domain, login_url, timeout_s)
    finally:
        _login_lock.release()


def _login_hints(domain: str) -> tuple:
    """Cookie names signalling a completed login for ``domain``.

    The host's site registry (AuthSourceProvider row ``fields`` — the same
    spec the Settings UI renders one input per cookie from) is the single
    source of truth; the module table below is only the standalone-library
    fallback. This is the last per-site hardcode in the login flow —
    internalizing a site no longer edits library code.
    """
    try:
        from tofu_search.providers import get_auth_source_provider
        provider = get_auth_source_provider()
        row = provider.get_source(domain) if provider is not None else None
        fields = (row or {}).get('fields') or []
        names = tuple(f['name'] for f in fields
                      if isinstance(f, dict) and f.get('name'))
        if names:
            return names
    except Exception as e:
        logger.debug('[Login] registry field hints unavailable for %s: %s',
                     domain, e)
    return _LOGIN_COOKIE_HINTS.get(domain, ())


def _run_capture(domain: str, login_url: str, timeout_s: int) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.warning('[Login] Playwright import failed: %s', e)
        return {'ok': False, 'reason': 'unavailable', 'error': str(e)}

    hints = _login_hints(domain)
    logger.info('[Login] launching headful browser for %s -> %s (timeout=%ds)',
                domain, login_url, timeout_s)

    pw = None
    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=False, args=['--no-sandbox'])
        except Exception as e:
            # A shell-only install produces Playwright's "just installed or
            # updated" banner here, which is actively misleading — running
            # `playwright install` again is exactly what created this state.
            # Say what is really wrong when we can tell.
            if not _headed_chromium_path():
                logger.warning('[Login] no headed-capable Chromium for %s: %s', domain, e)
                return {'ok': False, 'reason': 'headed_unavailable',
                        'error': _HEADED_MISSING_MSG}
            logger.warning('[Login] headful launch failed (no display?) for %s: %s', domain, e)
            return {'ok': False, 'reason': 'unavailable', 'error': str(e)}

        context = browser.new_context(locale='zh-CN')
        page = context.new_page()
        page.goto(login_url, wait_until='domcontentloaded', timeout=30_000)

        deadline = time.time() + timeout_s
        captured = []
        while time.time() < deadline:
            if not context.pages:
                logger.info('[Login] window closed by user for %s', domain)
                break
            try:
                cookies = context.cookies()
            except Exception as e:
                logger.debug('[Login] cookies() read failed (window closing?): %s', e)
                break
            names = {c.get('name') for c in cookies}
            if hints and any(h in names for h in hints):
                captured = cookies
                logger.info('[Login] login cookie detected for %s (%d cookies)',
                            domain, len(cookies))
                break
            time.sleep(1.5)
        else:
            try:
                captured = context.cookies()
            except Exception as e:
                logger.debug('[Login] final cookies() read failed: %s', e)
                captured = []

        if not captured:
            try:
                captured = context.cookies()
            except Exception:
                captured = []

        if not captured:
            return {'ok': False, 'reason': 'timeout',
                    'error': 'No session cookies captured (login not completed?)'}

        logger.info('[Login] captured %d cookies for %s', len(captured), domain)
        return {'ok': True, 'cookie_count': len(captured), 'cookies': captured}
    except Exception as e:
        logger.error('[Login] capture failed for %s: %s', domain, e, exc_info=True)
        return {'ok': False, 'reason': 'error', 'error': str(e)}
    finally:
        for closer, label in ((context, 'context'), (browser, 'browser')):
            if closer is not None:
                try:
                    closer.close()
                except Exception as e:
                    logger.debug('[Login] %s close failed: %s', label, e)
        if pw is not None:
            try:
                pw.stop()
            except Exception as e:
                logger.debug('[Login] playwright stop failed: %s', e)
