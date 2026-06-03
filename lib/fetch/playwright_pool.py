"""lib/fetch/playwright_pool.py — Lazy-loaded singleton Playwright browser pool.

Playwright's sync_playwright() binds to the calling thread's event loop.
Flask's threaded=True uses different worker threads per request, so we
run Playwright in a dedicated daemon thread and dispatch via a queue.
"""

import atexit
import os
import queue as _queue_mod
import re
import threading
import time

from lib.fetch.utils import HAS_PLAYWRIGHT
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PlaywrightPool',
]


def _ensure_chromium_library_path():
    """Augment LD_LIBRARY_PATH so Chromium can find its shared-library deps.

    On Linux systems without sudo (e.g. CentOS 7 HPC nodes), users can't run
    ``playwright install-deps`` to install Chromium's X11/GTK dependencies
    (libatk, libgbm, libXcomposite, …) system-wide. Instead, we install them
    via conda-forge into the active conda env. Those libs live under
    ``$CONDA_PREFIX/lib`` and (for cos7-compat packages) the gcc sysroot at
    ``$CONDA_PREFIX/x86_64-conda-linux-gnu/sysroot/usr/lib64``.

    Chromium is a child subprocess of the Python process, so it inherits
    ``LD_LIBRARY_PATH`` from ``os.environ``. We mutate os.environ once
    before the first browser launch so subprocesses see the conda paths.

    No-op on macOS/Windows (uses DYLD_LIBRARY_PATH / DLL search paths which
    Playwright already handles via its own bundled binaries).

    Users can override detection via CHROMIUM_EXTRA_LIB_DIRS (colon-separated).
    """
    # Linux-only — macOS/Windows use different mechanisms
    import sys as _sys
    if not _sys.platform.startswith('linux'):
        return

    extra_dirs: list[str] = []

    # 1. Explicit override via env var
    override = os.environ.get('CHROMIUM_EXTRA_LIB_DIRS', '').strip()
    if override:
        extra_dirs.extend(p for p in override.split(':') if p)

    # 2. Auto-detect conda env
    conda_prefix = os.environ.get('CONDA_PREFIX', '').strip()
    if conda_prefix and os.path.isdir(conda_prefix):
        lib_dir = os.path.join(conda_prefix, 'lib')
        if os.path.isdir(lib_dir):
            extra_dirs.append(lib_dir)
        # cos7 sysroot (used by mesa-libgbm-cos7-x86_64 etc.)
        sysroot_lib = os.path.join(
            conda_prefix, 'x86_64-conda-linux-gnu', 'sysroot', 'usr', 'lib64'
        )
        if os.path.isdir(sysroot_lib):
            extra_dirs.append(sysroot_lib)

    if not extra_dirs:
        return

    current = os.environ.get('LD_LIBRARY_PATH', '')
    current_parts = [p for p in current.split(':') if p]
    # Prepend only the dirs that aren't already in the path (preserves caller intent)
    new_parts = [d for d in extra_dirs if d not in current_parts]
    if not new_parts:
        return
    combined = ':'.join(new_parts + current_parts)
    os.environ['LD_LIBRARY_PATH'] = combined
    logger.info(
        '[Playwright] Augmented LD_LIBRARY_PATH with %d conda lib dir(s): %s',
        len(new_parts), ':'.join(new_parts),
    )


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
        self._started = False
        self._last_fail_ts = 0     # 上次启动失败时间戳 (防止无限重启)
        self._missing_binary = False  # last launch failed because chromium not installed

    # ── 专用线程的主循环 ──
    def _worker_loop(self, task_q):
        """运行在 _pw_thread 上；拥有 Playwright 的事件循环。"""
        from playwright.sync_api import sync_playwright

        from lib.compat import IS_LINUX

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
        except Exception as e:
            if getattr(self, '_missing_binary', False):
                logger.info(
                    'Playwright chromium binary missing — SPA-render fallback disabled. '
                    'Run `python -m playwright install chromium` (or re-run install.sh) to enable it.'
                )
            else:
                logger.warning('Playwright launch failed: %s', e, exc_info=True)
            self._ready = False
            # 排空已经在等的任务
            while True:
                try:
                    _, result_q = task_q.get_nowait()
                    result_q.put(None)
                except _queue_mod.Empty:
                    logger.debug('[Fetch] Task queue drained after browser launch failure')
                    break
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

    def _do_fetch(self, browser, url, timeout, max_chars):
        """在专用线程内执行：打开页面 → 渲染 → 提取文本。"""
        # Import here to avoid circular imports — html_extract is a sibling module
        from lib.fetch.html_extract import extract_html_text

        context = None
        try:
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/125.0.0.0 Safari/537.36'
                ),
                ignore_https_errors=True,
                java_script_enabled=True,
            )
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
                logger.debug('🎭 Playwright OK: %s chars in %.1fs — %s', f'{len(body_text):,}', elapsed, url[:80])
                return body_text

            # innerText 不行，退而用 trafilatura/BS4 解析渲染后的 HTML
            html = page.content()
            text = extract_html_text(html, max_chars or 0, url=url)
            if text and len(text) > 50:
                logger.debug('🎭 Playwright (extract) OK: %s chars in %.1fs — %s', f'{len(text):,}', elapsed, url[:80])
                return text

            logger.debug('🎭 Playwright got empty content — %s', url[:80])
            return None

        except Exception as e:
            # Self-recovering fallback: caller treats None as "Playwright failed"
            # and falls back to HTTP/trafilatura extraction. Common causes are
            # renderer crashes (Target crashed), navigation timeouts, and hostile
            # JS — all expected for a subset of pages. Keep at debug so
            # error.log stays clean; upgrade to warning only if it blocks the
            # whole pipeline (which it doesn't).
            ename = type(e).__name__
            logger.debug('🎭 Playwright error (%s) — %s: %s', ename, url[:80], e, exc_info=True)
            return None
        finally:
            if context:
                try:
                    context.close()
                except Exception as e:
                    logger.debug('[Fetch] Playwright context.close() failed for %s: %s', url[:80], e, exc_info=True)

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
            self._thread = threading.Thread(
                target=self._worker_loop,
                args=(self._task_q,),
                daemon=True,
                name='pw-worker',
            )
            self._thread.start()
            # 等待浏览器启动完成 (最多 15 秒)
            for _ in range(150):
                if self._ready or not self._thread.is_alive():
                    break
                time.sleep(0.1)
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
            logger.warning('🎭 Playwright worker timeout — %s', url[:80], exc_info=True)
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
