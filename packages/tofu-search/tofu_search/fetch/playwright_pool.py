"""lib/fetch/playwright_pool.py — Lazy-loaded singleton Playwright browser pool.

Playwright's sync_playwright() binds to the calling thread's event loop.
Flask's threaded=True uses different worker threads per request, so we
run Playwright in a dedicated daemon thread and dispatch via a queue.
"""

import atexit
import os
import queue as _queue_mod
import re
import sys
import threading
import time

from tofu_search.fetch.utils import HAS_PLAYWRIGHT
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PlaywrightPool',
]

# Default UA for browser contexts (the fetch/auth/search tasks all used the
# same Chrome-on-Windows string).
_DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)


#: Libs whose presence proves a directory really carries Chromium's GUI deps.
#: Used only by the FALLBACK below; the host's chromium_env owns the real rule.
_SENTINEL_LIBS = (
    'libatk-1.0.so.0', 'libatk-bridge-2.0.so.0', 'libnss3.so',
    'libgbm.so.1', 'libxkbcommon.so.0',
)


def _fallback_prefix_candidates():
    """Prefixes that may hold Chromium's GUI libs, most-authoritative first.

    ``sys.prefix`` FIRST and ``$CONDA_PREFIX`` only as a late fallback. This
    ordering is the whole point: ``sys.prefix`` is a property of the RUNNING
    INTERPRETER, so it is correct for conda envs, uv venvs and plain
    virtualenvs alike and cannot be un-set by the shell. ``$CONDA_PREFIX`` is
    set only by ``conda activate`` — measured 2026-07-29 on the dev host it is
    EMPTY, which made the previous implementation a silent no-op.
    """
    out = []
    for cand in (sys.prefix, getattr(sys, 'base_prefix', ''),
                 os.environ.get('CONDA_PREFIX', '')):
        cand = (cand or '').strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def _fallback_ensure_chromium_env():
    """Stand-alone copy of the host's rule, used when chromium_env is absent.

    tofu_search ships independently of the Tofu app, so it cannot REQUIRE the
    host module — but when it is importable that module is authoritative and
    this function never runs. Kept deliberately small: candidate prefixes are
    accepted only on EVIDENCE (a sentinel lib is really there), never inferred
    from a directory's shape.
    """
    # ── LD_LIBRARY_PATH (Linux-only; macOS/Windows use other mechanisms) ──
    if sys.platform.startswith('linux'):
        extra_dirs = []
        override = os.environ.get('CHROMIUM_EXTRA_LIB_DIRS', '').strip()
        if override:
            extra_dirs.extend(p for p in override.split(os.pathsep) if p)
        for prefix in _fallback_prefix_candidates():
            lib_dir = os.path.join(prefix, 'lib')
            if lib_dir in extra_dirs or not os.path.isdir(lib_dir):
                continue
            if not any(os.path.isfile(os.path.join(lib_dir, s))
                       for s in _SENTINEL_LIBS):
                continue
            extra_dirs.append(lib_dir)
            sysroot_lib = os.path.join(
                prefix, 'x86_64-conda-linux-gnu', 'sysroot', 'usr', 'lib64')
            if os.path.isdir(sysroot_lib) and sysroot_lib not in extra_dirs:
                extra_dirs.append(sysroot_lib)
        current = os.environ.get('LD_LIBRARY_PATH', '')
        current_parts = [p for p in current.split(os.pathsep) if p]
        new_parts = [d for d in extra_dirs if d not in current_parts]
        if new_parts:
            os.environ['LD_LIBRARY_PATH'] = os.pathsep.join(
                new_parts + current_parts)
            logger.info('[Playwright] LD_LIBRARY_PATH += %d GUI lib dir(s): %s',
                        len(new_parts), os.pathsep.join(new_parts))

    # ── fontconfig ──
    # Without this Chromium LAUNCHES but resolves zero fonts and draws every
    # glyph as nothing, so a fetch returns a blank-but-styled page. That reads
    # as "the site didn't load" rather than as an error, which is strictly
    # harder to diagnose than a crash. Measured 2026-07-29: this host has no
    # /etc/fonts and the pool had no fontconfig handling at all -> canvas
    # measureText('tofu') == 0.
    if os.path.isdir('/etc/fonts') or os.environ.get('FONTCONFIG_FILE'):
        return
    for prefix in _fallback_prefix_candidates():
        conf_dir = os.path.join(prefix, 'etc', 'fonts')
        conf_file = os.path.join(conf_dir, 'fonts.conf')
        if os.path.isfile(conf_file):
            os.environ['FONTCONFIG_PATH'] = conf_dir
            os.environ['FONTCONFIG_FILE'] = conf_file
            logger.info('[Playwright] FONTCONFIG_FILE=%s', conf_file)
            return


def _ensure_chromium_library_path():
    """Make headless Chromium launchable AND legible before the first launch.

    Chromium is a child subprocess, so it inherits ``os.environ``; both the
    GUI-lib path and the fontconfig config must be set BEFORE the driver
    starts or the launch either dies or renders nothing.

    Resolution is DELEGATED to the Tofu host's ``chromium_env`` module when it
    is importable — that module is the single source of truth for both halves
    and is exercised by the host's guard suites. tofu_search is an independent
    package, so it degrades to :func:`_fallback_ensure_chromium_env` (same
    rule, no host dependency) rather than requiring it.

    History (why the delegation, not another local copy): this function used to
    key off ``$CONDA_PREFIX`` alone. That variable is set only by ``conda
    activate`` — measured EMPTY on the dev host — so the self-heal was a silent
    no-op and every browser fetch died with ``TargetClosedError`` from a bare
    shell. It was one of four hand-copied divergent copies of this logic; the
    other three were consolidated 2026-07-28/29 and this one was the last.
    """
    try:
        from chromium_env import ensure_chromium_env
    except Exception as e:
        logger.debug('[Playwright] host chromium_env unavailable, using '
                     'built-in fallback: %s', e)
        _fallback_ensure_chromium_env()
        return
    report = ensure_chromium_env()
    if report.get('lib_dirs_added') or report.get('fontconfig'):
        logger.info('[Playwright] chromium env via host resolver: libs=%s fontconfig=%s',
                    report.get('lib_dirs_added'), report.get('fontconfig'))


class PlaywrightPool:
    """
    懒加载单例 Playwright 浏览器实例。

    关键设计: sync_playwright() 的事件循环绑定到调用线程。
    Flask 的 threaded=True 会让不同请求跑在不同 worker 线程，
    如果直接在 worker 线程里操作 Playwright 会报
    "cannot switch to a different thread"。

    解决: 启动一个专用守护线程 (_pw_thread)，所有 Playwright 操作
    都通过队列派发到该线程执行，调用方阻塞等待结果。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None       # 专用 Playwright 线程
        self._task_q = None       # 发送任务的队列
        self._ready = False       # 浏览器是否就绪
        # Signalled by the worker thread once the startup attempt finishes
        # (success OR failure). Replaces an unlocked cross-thread read of
        # _ready with a proper synchronisation primitive.
        self._ready_event = threading.Event()
        self._started = False
        self._last_fail_ts = 0     # 上次启动失败时间戳 (防止无限重启)
        self._missing_binary = False  # last launch failed because chromium not installed
        # Extra task kinds registered by the HOST app (see register_task_kind).
        # The worker loop consults this AFTER the built-in kinds, so tofu_search
        # ships zero host-feature code while the host can still ride this pool
        # instead of spawning a second Chromium.
        self._kind_handlers = {}

    # ── 专用线程的主循环 ──
    def _worker_loop(self, task_q):
        """运行在 _pw_thread 上；拥有 Playwright 的事件循环。"""
        from playwright.sync_api import sync_playwright

        IS_LINUX = sys.platform.startswith("linux")

        pw = None
        browser = None
        try:
            pw = sync_playwright().start()
            _launch_args = [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ]
            # --disable-setuid-sandbox is Linux-specific (setuid not used on macOS/Windows)
            if IS_LINUX:
                _launch_args.append('--disable-setuid-sandbox')
            try:
                browser = pw.chromium.launch(headless=True, args=_launch_args)
            except Exception as _launch_err:
                # Missing binary is a config state, not a runtime bug — install.sh
                # Step 8 (`python -m playwright install chromium`) handles it for
                # full installs, and users on `--skip-playwright` opted out. We
                # don't auto-install at request time because a ~50s subprocess
                # would silently stall user-visible fetches. Surface a one-line
                # remediation hint and propagate so _ensure_thread()'s cooldown
                # kicks in.
                self._missing_binary = (
                    'Executable doesn\'t exist' in str(_launch_err)
                    or 'executable' in str(_launch_err).lower()
                )
                raise
            logger.info('Playwright browser launched (dedicated thread)')
            self._ready = True
            self._ready_event.set()
        except Exception as e:
            if getattr(self, '_missing_binary', False):
                logger.info(
                    'Playwright chromium binary missing — SPA-render fallback disabled. '
                    'Run `python -m playwright install chromium` (or re-run install.sh) to enable it.'
                )
            else:
                logger.warning('Playwright launch failed: %s', e, exc_info=True)
            self._ready = False
            # Stop the Playwright driver so its node subprocess doesn't leak.
            # sync_playwright().start() spawns a long-lived driver process even
            # when the chromium *launch* fails; without this every failed start
            # (and every cooldown-triggered restart) orphaned one driver.
            if pw is not None:
                try:
                    pw.stop()
                except Exception as _stop_err:
                    logger.debug('[Fetch] playwright stop after launch failure failed: %s',
                                 _stop_err, exc_info=True)
            # 排空已经在等的任务
            while True:
                try:
                    _, result_q = task_q.get_nowait()
                    result_q.put(None)
                except _queue_mod.Empty:
                    logger.debug('[Fetch] Task queue drained after browser launch failure')
                    break
            # Unblock _ensure_thread waiters (startup attempt finished, failed).
            self._ready_event.set()
            return

        # 主循环: 从队列取任务执行
        while True:
            try:
                item = task_q.get()
            except Exception as e:
                logger.warning('[Fetch] browser worker task queue error: %s', e, exc_info=True)
                break
            if item is None:          # 收到 sentinel → 退出
                break
            payload, result_q = item
            # ── Task discriminator ────────────────────────────────────
            # Legacy fetches arrive as ((url, timeout, max_chars), q).
            # Newer task kinds arrive as ((kind:str, payload:dict), q).
            # Distinguish by the FIRST element type so older callers
            # keep working without changes.
            try:
                if (isinstance(payload, tuple)
                        and len(payload) == 2
                        and isinstance(payload[0], str)):
                    kind, kpayload = payload
                    if kind == 'pdf_render':
                        result = self._do_pdf_render(browser, kpayload)
                    elif kind == 'auth_fetch':
                        result = self._do_fetch_authenticated(browser, kpayload)
                    elif kind == 'auth_search':
                        result = self._do_search_authenticated(browser, kpayload)
                    elif kind in self._kind_handlers:
                        result = self._kind_handlers[kind](browser, kpayload)
                    else:
                        logger.warning(
                            '[Pool] unknown task kind=%r — returning None', kind,
                        )
                        result = None
                else:
                    url, timeout, max_chars = payload
                    result = self._do_fetch(browser, url, timeout, max_chars)
            except Exception as e:
                logger.warning(
                    '[Pool] worker task crashed: %s', e, exc_info=True,
                )
                result = {'error': str(e)}
            result_q.put(result)

        # 清理
        try:
            browser.close()
        except Exception as e:
            logger.debug('[Fetch] browser close failed: %s', e, exc_info=True)
        try:
            pw.stop()
        except Exception as e:
            logger.debug('[Fetch] playwright stop failed: %s', e, exc_info=True)

    def _new_context(self, browser, *, cookies=None, proxy='', locale=None,
                     java_script_enabled=True, user_agent=_DEFAULT_UA):
        """Create a browser context with the common options, then add cookies.

        Consolidates the near-identical context setup repeated by the fetch /
        auth-fetch / auth-search tasks. Cookie-add failures are logged and
        swallowed (a bad cookie shape should yield the login wall, not abort
        the whole fetch). Runs on the Playwright worker thread.
        """
        ctx_kwargs = dict(
            user_agent=user_agent,
            ignore_https_errors=True,
            java_script_enabled=java_script_enabled,
        )
        if locale:
            ctx_kwargs['locale'] = locale
        if proxy:
            ctx_kwargs['proxy'] = {'server': proxy}
        context = browser.new_context(**ctx_kwargs)
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                logger.warning('[Pool] add_cookies failed: %s', e)
        return context

    def _do_pdf_render(self, browser, payload):
        """PDF-render a self-contained HTML document.  Returns bytes or
        an error dict.  Runs on the Playwright worker thread.

        See lib/artifacts/pdf_export.py for the caller; payload shape is
        ``{'html': <full document str>, 'title': <str>}``.
        """
        html = (payload or {}).get('html') or ''
        title = (payload or {}).get('title') or ''
        if not html:
            return {'error': 'empty html'}

        context = None
        t0 = time.time()
        try:
            context = browser.new_context(
                java_script_enabled=False,  # never run model JS in the pool
                ignore_https_errors=True,
            )
            page = context.new_page()
            # Block all network — the document is fully self-contained
            # and we want predictable, isolated rendering.
            page.route('**/*', lambda route: route.abort())
            page.set_content(html, wait_until='domcontentloaded', timeout=15_000)
            pdf_bytes = page.pdf(
                format='A4',
                print_background=True,
                margin={'top': '14mm', 'bottom': '14mm',
                        'left': '14mm', 'right': '14mm'},
            )
            elapsed = time.time() - t0
            logger.info(
                '[Pool:pdf] rendered title=%r bytes=%d elapsed=%.2fs',
                title[:60], len(pdf_bytes), elapsed,
            )
            return pdf_bytes
        except Exception as e:
            logger.warning(
                '[Pool:pdf] render failed title=%r: %s', title[:60], e, exc_info=True,
            )
            return {'error': str(e)}
        finally:
            if context:
                try:
                    context.close()
                except Exception as e:
                    logger.debug('[Pool:pdf] context close failed: %s', e)

    def _do_search_authenticated(self, browser, payload):
        """Run a search on a login-walled site inside the user's session.

        Navigates ``url`` (the site's search-results page) in a context
        carrying the user's cookies/proxy, optionally waits for a result
        selector, then evaluates a site-supplied JS extractor that returns
        an array of ``{title, snippet, url}`` objects scraped from the DOM.

        Runs on the Playwright worker thread. ``payload`` shape::

            {'url': str, 'timeout': int, 'cookies': [...], 'proxy': str|'',
             'extractor_js': str,        # a JS expression → array of dicts
             'wait_selector': str|''}    # optional CSS selector to await

        Returns a list of result dicts (possibly empty) or None on hard
        error. The engine layer owns the per-site extractor + URL.
        """
        url = (payload or {}).get('url') or ''
        timeout = int((payload or {}).get('timeout') or 20)
        cookies = (payload or {}).get('cookies') or []
        proxy = ((payload or {}).get('proxy') or '').strip()
        extractor_js = (payload or {}).get('extractor_js') or '[]'
        wait_selector = (payload or {}).get('wait_selector') or ''

        context = None
        try:
            context = self._new_context(browser, cookies=cookies, proxy=proxy,
                                        locale='zh-CN')
            page = context.new_page()
            page.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,otf,mp4,mp3,webm}',
                       lambda route: route.abort())

            t0 = time.time()
            page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=min(timeout, 12) * 1000)
                except Exception as e:
                    logger.debug('[Pool:authsearch] wait_selector %r timed out: %s',
                                 wait_selector, e)
            else:
                try:
                    page.wait_for_timeout(1500)
                except Exception as e:
                    logger.debug('[Pool:authsearch] settle wait failed: %s', e)

            try:
                items = page.evaluate(extractor_js)
            except Exception as e:
                logger.warning('[Pool:authsearch] extractor eval failed for %s: %s',
                               url[:80], e)
                items = []
            elapsed = time.time() - t0
            if not isinstance(items, list):
                logger.warning('[Pool:authsearch] extractor returned non-list (%s) for %s',
                               type(items).__name__, url[:80])
                items = []
            logger.info('[Pool:authsearch] %d item(s) in %.1fs — %s',
                        len(items), elapsed, url[:80])
            return items
        except Exception as e:
            logger.warning('[Pool:authsearch] error (%s) — %s: %s',
                           type(e).__name__, url[:80], e, exc_info=True)
            return None
        finally:
            if context:
                try:
                    context.close()
                except Exception as e:
                    logger.debug('[Pool:authsearch] context close failed for %s: %s', url[:80], e)

    def _do_fetch_authenticated(self, browser, payload):
        """Fetch a login-walled URL inside a context carrying the user's cookies.

        Runs on the Playwright worker thread. ``payload`` shape::

            {'url': str, 'timeout': int, 'max_chars': int,
             'cookies': [playwright-cookie-dict, ...], 'proxy': str|''}

        Returns extracted text (str) or None. The cookies make the
        difference between the login wall and real content; the optional
        proxy routes around datacenter-IP bans. We reuse the same render +
        extract logic as :meth:`_do_fetch` so output is identical to the
        anonymous path.
        """
        from tofu_search.fetch.html_extract import extract_html_text

        url = (payload or {}).get('url') or ''
        timeout = int((payload or {}).get('timeout') or 20)
        max_chars = (payload or {}).get('max_chars')
        cookies = (payload or {}).get('cookies') or []
        proxy = ((payload or {}).get('proxy') or '').strip()

        context = None
        try:
            context = self._new_context(browser, cookies=cookies, proxy=proxy,
                                        locale='zh-CN')
            page = context.new_page()
            page.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,otf,mp4,mp3,webm}',
                       lambda route: route.abort())

            t0 = time.time()
            page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')

            # ── Render wait for an AUTHENTICATED load ──
            # "innerText > 200 chars" is NOT a usable readiness signal here: an
            # SSO login wall is itself a full page (~1.9k chars), so the
            # condition is satisfied INSTANTLY and we return the wall while the
            # session redirect is still in flight — the whole point of replaying
            # cookies is then lost. Readiness for an authenticated fetch means
            # "we are no longer on an auth host AND the text has stopped
            # growing", so wait for both.
            _max_render_wait = min(timeout, 12)
            _AUTH_HOST_HINT = ('/sson/login', '/login?', 'ssosv.', 'passport.',
                               'account.', '/oauth', '/sso/')

            def _on_auth_wall():
                u = (page.url or '').lower()
                return any(h in u for h in _AUTH_HOST_HINT)

            # 1) If we landed on a login wall, the cookies may still be being
            #    exchanged for a session — give the redirect a chance to leave.
            if _on_auth_wall():
                _wall_deadline = time.time() + _max_render_wait
                while time.time() < _wall_deadline and _on_auth_wall():
                    page.wait_for_timeout(500)
                if _on_auth_wall():
                    logger.info('[Pool:auth] still on an auth wall after %.1fs — '
                                'stored session likely expired/insufficient: %s',
                                time.time() - t0, page.url[:100])
                else:
                    logger.debug('[Pool:auth] left the auth wall in %.1fs — now %s',
                                 time.time() - t0, page.url[:90])

            try:
                page.wait_for_function(
                    'document.body && document.body.innerText.trim().length > 200',
                    timeout=max(int((_max_render_wait - (time.time() - t0)) * 1000), 1000),
                )
            except Exception as e:
                logger.debug('[Pool:auth] render wait timed out for %s: %s', url[:80], e)

            # 2) Text-stability settle — the SPA mounts its real content after
            #    the auth check resolves, so the first non-empty body is usually
            #    a shell. Two consecutive equal lengths = done.
            _prev_len, _stable = 0, 0
            for _ in range(8):
                if time.time() - t0 > timeout:
                    break
                try:
                    _cur = page.evaluate('document.body.innerText.trim().length')
                except Exception:
                    break
                if _cur == _prev_len and _cur > 0:
                    _stable += 1
                    if _stable >= 2:
                        break
                else:
                    _stable = 0
                _prev_len = _cur
                page.wait_for_timeout(500)
            elapsed = time.time() - t0

            _remaining_ms = max(int((timeout - elapsed) * 1000), 3000)
            try:
                body_text = page.locator('body').text_content(timeout=_remaining_ms) or ''
            except Exception as _tc_err:
                logger.debug('[Pool:auth] text_content failed, trying evaluate: %s', _tc_err)
                try:
                    body_text = page.evaluate('document.body?.innerText || ""')
                except Exception as _eval_err:
                    logger.debug('[Pool:auth] evaluate also failed: %s', _eval_err)
                    body_text = ''
            body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()

            if body_text and len(body_text) > 50:
                if max_chars and len(body_text) > max_chars:
                    body_text = body_text[:max_chars] + '\n[…truncated]'
                logger.info('[Pool:auth] OK %s chars in %.1fs — %s',
                            f'{len(body_text):,}', elapsed, url[:80])
                return body_text

            html = page.content()
            text = extract_html_text(html, max_chars or 0, url=url)
            if text and len(text) > 50:
                logger.info('[Pool:auth] OK (extract) %s chars in %.1fs — %s',
                            f'{len(text):,}', elapsed, url[:80])
                return text
            logger.info('[Pool:auth] empty content (login wall?) — %s', url[:80])
            return None
        except Exception as e:
            logger.warning('[Pool:auth] error (%s) — %s: %s',
                           type(e).__name__, url[:80], e, exc_info=True)
            return None
        finally:
            if context:
                try:
                    context.close()
                except Exception as e:
                    logger.debug('[Pool:auth] context close failed for %s: %s', url[:80], e)

    def _do_fetch(self, browser, url, timeout, max_chars):
        """在专用线程内执行：打开页面 → 渲染 → 提取文本。"""
        # Import here to avoid circular imports — html_extract is a sibling module
        from tofu_search.fetch.html_extract import extract_html_text

        context = None
        try:
            context = self._new_context(browser)
            page = context.new_page()
            # 屏蔽不必要的资源加载 (图片/字体/媒体) 加速渲染
            page.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,otf,mp4,mp3,webm}',
                        lambda route: route.abort())
            page.route('**/analytics*', lambda route: route.abort())
            page.route('**/tracking*', lambda route: route.abort())

            t0 = time.time()
            page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')

            # ── 智能渲染等待 ──
            _max_render_wait = min(timeout, 12)
            try:
                page.wait_for_function(
                    'document.body && document.body.innerText.trim().length > 200',
                    timeout=_max_render_wait * 1000,
                )
            except Exception as e:
                logger.debug('[Fetch] page render wait timed out for: %s: %s', url[:100], e, exc_info=True)

            # 等文本稳定: 每 0.5s 检查一次，连续 2 次长度不变 → 渲染完毕
            _prev_len = 0
            _stable_count = 0
            for _ in range(8):
                try:
                    _cur_len = page.evaluate('document.body.innerText.trim().length')
                except Exception as e:
                    logger.debug('[Fetch] body text length check failed for %s: %s', url[:80], e, exc_info=True)
                    break
                if _cur_len == _prev_len and _cur_len > 200:
                    _stable_count += 1
                    if _stable_count >= 2:
                        break
                else:
                    _stable_count = 0
                _prev_len = _cur_len
                page.wait_for_timeout(500)

            elapsed = time.time() - t0

            # ── 提取文本 ──
            # Use locator('body').text_content() instead of page.inner_text('body').
            # inner_text() performs actionability checks (waits for element to be
            # visible & stable) which hangs on pages that never finish JS execution
            # (e.g. Google News redirect stubs). text_content() skips actionability
            # checks entirely — it reads raw DOM text immediately, which is exactly
            # what we need for scraping. Falls back to evaluate() if needed.
            _remaining_ms = max(int((timeout - elapsed) * 1000), 3000)
            try:
                body_text = page.locator('body').text_content(timeout=_remaining_ms) or ''
            except Exception as _tc_err:
                logger.debug('[Fetch] locator text_content failed, trying evaluate: %s', _tc_err)
                try:
                    body_text = page.evaluate('document.body?.innerText || ""')
                except Exception as _eval_err:
                    logger.debug('[Fetch] evaluate also failed: %s', _eval_err)
                    body_text = ''
            body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()

            if body_text and len(body_text) > 50:
                if max_chars and len(body_text) > max_chars:
                    body_text = body_text[:max_chars] + '\n[…truncated]'
                logger.debug('[Pool] Playwright OK: %s chars in %.1fs — %s', f'{len(body_text):,}', elapsed, url[:80])
                return body_text

            # innerText 不行，退而用 trafilatura/BS4 解析渲染后的 HTML
            html = page.content()
            text = extract_html_text(html, max_chars or 0, url=url)
            if text and len(text) > 50:
                logger.debug('[Pool] Playwright (extract) OK: %s chars in %.1fs — %s', f'{len(text):,}', elapsed, url[:80])
                return text

            logger.debug('[Pool] Playwright got empty content — %s', url[:80])
            return None

        except Exception as e:
            # Self-recovering fallback: caller treats None as "Playwright failed"
            # and falls back to HTTP/trafilatura extraction. Common causes are
            # renderer crashes (Target crashed), navigation timeouts, and hostile
            # JS — all expected for a subset of pages. Keep at debug so
            # error.log stays clean; upgrade to warning only if it blocks the
            # whole pipeline (which it doesn't).
            ename = type(e).__name__
            logger.debug('[Pool] Playwright error (%s) — %s: %s', ename, url[:80], e, exc_info=True)
            return None
        finally:
            if context:
                try:
                    context.close()
                except Exception as e:
                    logger.debug('[Fetch] Playwright context.close() failed for %s: %s', url[:80], e, exc_info=True)

    #: Kinds the pool itself implements; register_task_kind refuses these.
    _BUILTIN_KINDS = frozenset({'pdf_render', 'auth_fetch', 'auth_search'})

    def register_task_kind(self, kind, handler):
        """Register an extra task kind dispatched on the pool worker thread.

        ``handler`` is called as ``handler(browser, payload) -> result`` ON the
        dedicated Playwright thread, so it may use the sync Playwright API
        directly. Registration is process-global and idempotent ONLY for the
        same handler object: re-registering a kind with a DIFFERENT handler
        raises ValueError (a silent swap would mask a double-registration
        bug), and built-in kinds can never be overridden.
        """
        if not kind or not isinstance(kind, str):
            raise ValueError(f'kind must be a non-empty str, got {kind!r}')
        if not callable(handler):
            raise ValueError(f'handler for kind={kind!r} must be callable')
        if kind in self._BUILTIN_KINDS:
            raise ValueError(
                f'kind={kind!r} is a built-in pool kind and cannot be overridden')
        with self._lock:
            existing = self._kind_handlers.get(kind)
            if existing is not None and existing is not handler:
                raise ValueError(
                    f'kind={kind!r} already registered with a different handler')
            self._kind_handlers[kind] = handler

    def _ensure_thread(self):
        """确保专用 Playwright 线程已启动。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._ready
            # ── 冷却期: 启动失败后 60 秒内不再重试，避免日志刷屏 ──
            if self._last_fail_ts and (time.time() - self._last_fail_ts < 60):
                return False
            if self._started and self._thread is not None:
                logger.info('Playwright thread died, restarting...')
            if not HAS_PLAYWRIGHT:
                if not self._started:
                    logger.info('Playwright not installed — SPA fallback disabled')
                self._started = True
                return False

            self._started = True
            self._task_q = _queue_mod.Queue()
            self._ready = False
            self._ready_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                args=(self._task_q,),
                daemon=True,
                name='pw-worker',
            )
            self._thread.start()
            # Wait for the worker to finish its startup attempt (success OR
            # failure both set _ready_event). The worker sets self._ready
            # before the event, so reading it here is safe (event provides the
            # happens-before barrier).
            self._ready_event.wait(timeout=15)
            if not self._ready:
                if self._missing_binary:
                    # Already explained at INFO above — don't escalate to ERROR
                    # since the user opted into this state (skipped installer).
                    pass
                else:
                    logger.error('Playwright thread failed to start browser')
                self._last_fail_ts = time.time()
            else:
                self._last_fail_ts = 0
                atexit.register(self._shutdown)
            return self._ready

    def _shutdown(self):
        if self._task_q:
            try:
                self._task_q.put(None)   # sentinel
            except Exception as e:
                logger.debug('[Fetch] Playwright shutdown sentinel send failed: %s', e, exc_info=True)

    def fetch(self, url, timeout=20, max_chars=None):
        """
        用真实浏览器渲染页面，提取正文文本。
        线程安全：任何线程均可调用，内部派发到专用 Playwright 线程。
        返回 str 或 None。
        """
        if not self._ensure_thread():
            return None

        result_q = _queue_mod.Queue()
        self._task_q.put(((url, timeout, max_chars), result_q))
        try:
            return result_q.get(timeout=timeout + 30)  # 宽裕超时
        except _queue_mod.Empty:
            logger.warning('[Pool] Playwright worker timeout (%ss) — %s', timeout, url[:80])
            return None

    def fetch_authenticated(self, url, cookies, proxy='', timeout=25, max_chars=None):
        """Render a login-walled URL with the user's cookies (+ optional proxy).

        Thread-safe; dispatches to the dedicated Playwright thread like
        :meth:`fetch`. Returns str or None.
        """
        if not self._ensure_thread():
            return None
        result_q = _queue_mod.Queue()
        payload = {'url': url, 'timeout': timeout, 'max_chars': max_chars,
                   'cookies': cookies or [], 'proxy': proxy or ''}
        self._task_q.put((('auth_fetch', payload), result_q))
        try:
            return result_q.get(timeout=timeout + 30)
        except _queue_mod.Empty:
            logger.warning('[Pool] Playwright auth worker timeout (%ss) — %s', timeout, url[:80])
            return None

    def search_authenticated(self, url, cookies, proxy='', timeout=20,
                             extractor_js='[]', wait_selector=''):
        """Scrape a login-walled search-results page with the user's session.

        Thread-safe; dispatches to the dedicated Playwright thread. Returns
        a list of ``{title, snippet, url}`` dicts (possibly empty) or None.
        """
        if not self._ensure_thread():
            return None
        result_q = _queue_mod.Queue()
        payload = {'url': url, 'timeout': timeout, 'cookies': cookies or [],
                   'proxy': proxy or '', 'extractor_js': extractor_js,
                   'wait_selector': wait_selector}
        self._task_q.put((('auth_search', payload), result_q))
        try:
            return result_q.get(timeout=timeout + 30)
        except _queue_mod.Empty:
            logger.warning('[Pool] Playwright auth-search worker timeout (%ss) — %s', timeout, url[:80])
            return None


# ── Module-load side effect ──
# Augment LD_LIBRARY_PATH once on import, before any Chromium subprocess spawns.
# This needs to happen before sync_playwright().start() so the node driver
# inherits the right env. Safe no-op on non-Linux.
try:
    _ensure_chromium_library_path()
except Exception as _e:
    logger.debug('[Playwright] LD_LIBRARY_PATH augmentation skipped: %s', _e)


# Module-level singleton
_pw_pool = PlaywrightPool()
