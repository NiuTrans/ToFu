"""lib/fetch/playwright_pool.py — Lazy-loaded singleton Playwright browser pool.

Playwright's sync_playwright() binds to the calling thread's event loop.
Flask's threaded=True uses different worker threads per request, so we
run Playwright in a dedicated daemon thread and dispatch via a queue.
"""

import atexit
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
                # ── Auto-install: if the executable doesn't exist, try installing ──
                if 'Executable doesn\'t exist' in str(_launch_err) or 'executable' in str(_launch_err).lower():
                    logger.info('Playwright browser not installed — attempting auto-install...')
                    import subprocess
                    try:
                        subprocess.run(
                            ['python', '-m', 'playwright', 'install', 'chromium'],
                            timeout=120, capture_output=True, check=True,
                        )
                        logger.info('Playwright chromium installed successfully, retrying launch...')
                        browser = pw.chromium.launch(headless=True, args=_launch_args)
                    except Exception as _install_err:
                        logger.warning('Playwright auto-install failed: %s', _install_err, exc_info=True)
                        raise _launch_err from _install_err
                else:
                    raise
            logger.info('Playwright browser launched (dedicated thread)')
            self._ready = True
        except Exception as e:
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
            (url, timeout, max_chars), result_q = item
            result = self._do_fetch(browser, url, timeout, max_chars)
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
            body_text = page.inner_text('body')
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
            ename = type(e).__name__
            logger.warning('🎭 Playwright error (%s) — %s: %s', ename, url[:80], e, exc_info=True)
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


# Module-level singleton
_pw_pool = PlaywrightPool()
