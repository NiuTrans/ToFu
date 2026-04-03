"""lib/fetch/utils.py — Fetch infrastructure: sessions, SSL, circuit breaker, helpers.

Extracted from lib/fetch.py to keep the main module focused on fetch logic.
All symbols are re-imported by lib/fetch.py for backward compatibility.
"""

import re
import ssl
import threading
import time
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)

# Suppress InsecureRequestWarning for SSL-fallback retries
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Suppress noisy "Retrying ... after connection broken by ReadTimeoutError" warnings
# These are logged by urllib3 internally; we handle errors at the requests level already
import logging as _logging

_logging.getLogger('urllib3.connectionpool').setLevel(_logging.ERROR)

from lib.log import get_logger

logger = get_logger(__name__)

# utils.py is internal infrastructure — nothing is re-exported via the
# package façade.  Sub-modules import specific names directly.
__all__: list[str] = []


# ═══════════════════════════════════════════════════════
#  Lazy third-party imports (deferred for fast startup)
# ═══════════════════════════════════════════════════════

def _get_bs4():
    from bs4 import BeautifulSoup
    return BeautifulSoup


try:
    import trafilatura  # noqa: F401
    HAS_TRAFILATURA = True
except ImportError as e:
    trafilatura = None  # type: ignore[assignment]
    HAS_TRAFILATURA = False
    logger.warning('[Fetch] trafilatura not installed — advanced text extraction disabled: %s', e)

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError as e:
    sync_playwright = None  # type: ignore[assignment]
    HAS_PLAYWRIGHT = False
    logger.warning('[Fetch] playwright not installed — JS-rendered page fetching disabled: %s', e)

try:
    import pymupdf  # noqa: F401
    HAS_FITZ = True
except ImportError as e:
    pymupdf = None  # type: ignore[assignment]
    HAS_FITZ = False
    logger.warning('[Fetch] pymupdf not installed — PDF parsing disabled: %s', e)

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError as e:
    Image = None  # type: ignore[assignment]
    HAS_PIL = False
    logger.warning('[Fetch] Pillow not installed — image processing disabled: %s', e)

HAS_PYPDF2 = False


# ═══════════════════════════════════════════════════════
#  Constants & compiled patterns
# ═══════════════════════════════════════════════════════

_URL_RE = re.compile(r'https?://[^\s<>"\')\]，。！？、）】}]+[^\s<>"\')\]，。！？、）】}.,:;!?]')

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

# NOTE: trafilatura/playwright availability logged on first use (lazy imports)


# ═══════════════════════════════════════════════════════
#  连接池（带正确的重试策略）
# ═══════════════════════════════════════════════════════

_retry_strategy = Retry(
    total=2,                    # 最多重试2次（共3次请求）
    read=0,                     # ← 禁止 read-timeout 重试（8s 都读不出来，重试也白搭）
    connect=2,                  # 连接失败可以重试（可能是瞬时网络抖动）
    backoff_factor=0.5,         # 重试间隔: 0.5s, 1s
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET', 'HEAD'],
    raise_on_status=False,      # 不在 retry 层抛异常，让 requests 处理
)

_session = requests.Session()
_session.headers.update(_HEADERS)
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=_retry_strategy)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

# 无 SSL 验证的 session（用于证书过期降级）
_session_no_ssl = requests.Session()
_session_no_ssl.headers.update(_HEADERS)
_session_no_ssl.verify = False
_adapter_no_ssl = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=_retry_strategy)
_session_no_ssl.mount('https://', _adapter_no_ssl)
_session_no_ssl.mount('http://', _adapter_no_ssl)

# 兼容 legacy TLS renegotiation 的 session（OpenSSL 3.x 默认禁止）
# 用于 chinamoney.org.cn, group.ccb.com 等老旧服务器
try:
    _legacy_ctx = ssl.create_default_context()
    _legacy_ctx.check_hostname = False
    _legacy_ctx.verify_mode = ssl.CERT_NONE
    _legacy_ctx.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)

    class _LegacySSLAdapter(HTTPAdapter):
        """HTTPAdapter that allows unsafe legacy TLS renegotiation."""
        def init_poolmanager(self, *a, **kw):
            kw['ssl_context'] = _legacy_ctx
            return super().init_poolmanager(*a, **kw)
        def proxy_manager_for(self, proxy, **kw):
            kw['ssl_context'] = _legacy_ctx
            return super().proxy_manager_for(proxy, **kw)

    _session_legacy_ssl = requests.Session()
    _session_legacy_ssl.headers.update(_HEADERS)
    _session_legacy_ssl.verify = False
    _session_legacy_ssl.mount('https://', _LegacySSLAdapter(
        pool_connections=5, pool_maxsize=10, max_retries=_retry_strategy))
    _session_legacy_ssl.mount('http://', _adapter_no_ssl)
    _HAS_LEGACY_SSL = True
except Exception as e:
    _logging.getLogger(__name__).warning('Legacy SSL adapter unavailable, falling back to standard SSL: %s', e, exc_info=True)
    _session_legacy_ssl = _session_no_ssl  # fallback
    _HAS_LEGACY_SSL = False


# ═══════════════════════════════════════════════════════
#  域名级熔断器 — 连续失败的域名暂时跳过
# ═══════════════════════════════════════════════════════

class _DomainCircuitBreaker:
    """跟踪每个域名的失败次数；短时间内连续失败则跳过该域名一段时间。"""
    FAIL_THRESHOLD = 5          # 连续失败 N 次触发熔断（宽松：避免误杀正常域名）
    COOLDOWN       = 120        # 熔断冷却 2 分钟（缩短：快速恢复）
    WINDOW         = 120        # 失败计数窗口 2 分钟

    def __init__(self):
        self._lock = threading.Lock()
        # domain -> {'fails': int, 'first_fail': float, 'tripped_at': float|None}
        self._domains: dict = {}

    def _get_domain(self, url):
        try:
            return urlparse(url).netloc.lower()
        except Exception as e:
            logger.debug('Failed to parse domain from URL: %.80s: %s', url, e, exc_info=True)
            return ''

    def is_open(self, url):
        """True = 熔断已触发，应跳过此域名。"""
        domain = self._get_domain(url)
        if not domain:
            return False
        with self._lock:
            state = self._domains.get(domain)
            if not state or state['tripped_at'] is None:
                return False
            if time.time() - state['tripped_at'] > self.COOLDOWN:
                # 冷却完毕，重置
                del self._domains[domain]
                return False
            return True

    def record_failure(self, url):
        domain = self._get_domain(url)
        if not domain:
            return
        now = time.time()
        with self._lock:
            state = self._domains.get(domain)
            if not state:
                self._domains[domain] = {'fails': 1, 'first_fail': now, 'tripped_at': None}
                return
            # 超出窗口，重新计数
            if now - state['first_fail'] > self.WINDOW:
                self._domains[domain] = {'fails': 1, 'first_fail': now, 'tripped_at': None}
                return
            state['fails'] += 1
            if state['fails'] >= self.FAIL_THRESHOLD and state['tripped_at'] is None:
                state['tripped_at'] = now
                logger.warning('Circuit OPEN for %s — %d failures in %.0fs, cooling down %ds',
                      domain, state['fails'], now - state['first_fail'], self.COOLDOWN)

    def record_success(self, url):
        domain = self._get_domain(url)
        if not domain:
            return
        with self._lock:
            self._domains.pop(domain, None)

    def get_status(self):
        """返回当前被熔断的域名列表（调试用）。"""
        now = time.time()
        with self._lock:
            return {d: round(self.COOLDOWN - (now - s['tripped_at']))
                    for d, s in self._domains.items()
                    if s['tripped_at'] and now - s['tripped_at'] <= self.COOLDOWN}

_circuit = _DomainCircuitBreaker()


# ═══════════════════════════════════════════════════════
#  URL 缓存
# ═══════════════════════════════════════════════════════

_CACHE_EXTRACT_LIMIT = max(_lib.FETCH_MAX_CHARS_DIRECT, _lib.FETCH_MAX_CHARS_SEARCH) * 2


class _FetchCache:
    def __init__(self, ttl=600, max_size=200):
        self._data, self._lock = {}, threading.Lock()
        self._ttl, self._max = ttl, max_size
    def get(self, url):
        with self._lock:
            e = self._data.get(url)
            if e and e[1] > time.time(): return e[0]
            self._data.pop(url, None); return None
    def put(self, url, content):
        if not content: return
        with self._lock:
            if len(self._data) >= self._max:
                del self._data[min(self._data, key=lambda k: self._data[k][1])]
            self._data[url] = (content, time.time() + self._ttl)
    @property
    def size(self):
        with self._lock: return len(self._data)

_fetch_cache = _FetchCache(ttl=600, max_size=200)
# Light cache: store raw HTML head (first 20KB) for publish-date extraction
_html_head_cache = _FetchCache(ttl=600, max_size=300)


# ═══════════════════════════════════════════════════════
#  反爬检测
# ═══════════════════════════════════════════════════════

def _is_bot_protection(html_text):
    """Detect bot-protection / challenge pages from raw HTML.

    Checks the HTML source for indicators of Cloudflare, Akamai, DDoS-Guard,
    and other bot-protection services.  No length ceiling — modern Cloudflare
    challenge pages routinely exceed 8 KB with inline JS/CSS.
    """
    if not html_text:
        return False
    # Only scan the first 50 KB — sufficient for all challenge pages while
    # avoiding perf issues on very large HTML documents.
    lower = html_text[:51200].lower()
    indicators = (
        'probe.js', '/challenge', 'captcha', 'cf-browser-verification',
        'just a moment', 'checking your browser', 'ddos-guard',
        '_cf_chl', 'cf_chl_opt', 'turnstile', 'bot detection',
        'security check', 'security verification',
        'please wait', 'verify you are human', 'access denied',
        'attention required', 'enable javascript and cookies',
        'ray id:', 'performance &amp; security by',
        'performance & security by cloudflare',
        'checking if the site connection is secure',
    )
    matched = sum(1 for s in indicators if s in lower)
    if matched >= 1:
        m = re.search(r'<body[^>]*>(.*?)</body>', html_text, re.DOTALL | re.I)
        # Small body + indicator = challenge page
        if not m or len(m.group(1).strip()) < 200:
            return True
        # 2+ indicators in a modest body = still very likely a challenge page
        if matched >= 2 and len(m.group(1).strip()) < 3000:
            return True
    m = re.search(r'<body[^>]*>(.*?)</body>', html_text, re.DOTALL | re.I)
    if m and len(m.group(1).strip()) < 30 and '<script' in lower:
        return True
    return False


# ── Post-extraction bot-content detection ──
# Catches pages where _is_bot_protection missed the HTML check (e.g. large
# HTML, new Cloudflare variants) but the *extracted text* is clearly a
# challenge / verification page.  Real articles extract to 500+ chars;
# bot protection pages typically extract to ~50-300 chars.
_BOT_TEXT_PATTERNS = (
    'ray id:', 'security verification', 'verify you are human',
    'checking your browser', 'please complete the security check',
    'enable javascript and cookies',
    'checking if the site connection is secure',
    'this process is automatic',
    'performance & security by cloudflare',
    'performance and security by cloudflare',
    'attention required', 'ddos protection by',
    'just a moment', 'access denied',
    'cf-browser-verification',
)


def _is_bot_extracted_text(text):
    """Detect bot-protection pages from *extracted* text (post-extraction).

    Only checks short extractions (≤600 chars) — real content is longer.
    Returns True if the text looks like a bot-protection / challenge page.
    """
    if not text or len(text) > 600:
        return False
    lower = text.lower()
    return any(p in lower for p in _BOT_TEXT_PATTERNS)


# ═══════════════════════════════════════════════════════
#  编码检测
# ═══════════════════════════════════════════════════════

def _decode_bytes(raw, hint):
    meta_enc = None
    try:
        m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_-]+)', raw[:4096], re.I)
        if m: meta_enc = m.group(1).decode('ascii', errors='ignore')
    except Exception as e:
        logger.debug('[Fetch] charset meta-tag extraction failed for encoding hint=%s: %s', hint, e, exc_info=True)
    CATCHALL = frozenset({'iso-8859-1','latin-1','latin1','ascii','us-ascii'})
    cands = []
    if meta_enc: cands.append(meta_enc)
    cands.append('utf-8')
    if hint and hint.lower().replace('_','-') not in CATCHALL: cands.append(hint)
    cands.extend(['gb18030','gbk','big5','latin-1'])
    seen = set()
    for enc in cands:
        if not enc: continue
        k = enc.lower().replace('_','-')
        if k in seen: continue
        seen.add(k)
        try: return raw.decode(enc)
        except Exception as e:
            logger.debug('[Fetch] decode attempt failed for encoding=%s: %s', enc, e, exc_info=True)
    return raw.decode('utf-8', errors='replace')


# ═══════════════════════════════════════════════════════
#  SPA detection helpers
# ═══════════════════════════════════════════════════════

# 已知需要 JS 渲染才能获取内容的 SPA 域名
SPA_DOMAINS = frozenset({
    'feishu.cn', 'open.feishu.cn', 'larksuite.com',
    'notion.so', 'notion.site',
    'yuque.com',
    'docs.google.com',
    'airtable.com',
    'figma.com',
    'miro.com',
    'app.slack.com',
    'web.telegram.org',
    'trello.com',
    'canva.com',
    'v0.dev',
})

# Playwright 需要 JS 渲染的最小可提取文本长度阈值
_SPA_MIN_TEXT_LEN = 150


def _is_known_spa(url):
    """检查 URL 是否属于已知需要 JS 渲染的 SPA 域名。"""
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith('.' + d) for d in SPA_DOMAINS)
    except Exception as e:
        logger.debug('[Fetch] SPA domain check failed for url=%s: %s', url[:80], e, exc_info=True)
        return False


def _looks_like_spa_shell(raw_html, extracted_text):
    """
    启发式判断 requests 拿到的 HTML 是不是一个 JS 空壳 (需要 JS 渲染)。
    同时要避免误判 —— 有些页面本来就文字很少 (如 example.com)。

    判定为 SPA 空壳的条件 (需满足 至少一个):
      A) HTML 中包含典型 SPA 挂载点标记 (id="root"/"app"/__next 等) 且文本极少
      B) HTML > 2KB 但提取文本 < 100 字符 (大 HTML 却没内容, 强信号)
    """
    if not raw_html:
        return False
    html_str = raw_html if isinstance(raw_html, str) else raw_html.decode('utf-8', errors='ignore')
    html_len = len(html_str)
    text_len = len(extracted_text) if extracted_text else 0

    # A) 检查 SPA 挂载点标记
    _SPA_MARKERS = (
        'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
        'id="main-app"', 'id="react-root"', 'id="vue-app"',
        'data-reactroot', 'ng-app=', 'ng-version=',
    )
    html_lower = html_str[:8000].lower()
    has_spa_marker = any(m.lower() in html_lower for m in _SPA_MARKERS)

    if has_spa_marker and text_len < _SPA_MIN_TEXT_LEN:
        return True

    # B) 大 HTML + 极少文本 = 强 SPA 信号 (2KB+ HTML 但 < 100 字文本)
    if html_len > 2000 and text_len < 100:
        return True

    return False


# ═══════════════════════════════════════════════════════
#  Code-hosting URL normalization (GitHub/GitLab/Bitbucket → raw)
# ═══════════════════════════════════════════════════════

# GitLab:    /owner/repo/-/blob/ref/path  → /owner/repo/-/raw/ref/path
_GITLAB_BLOB_RE = re.compile(
    r'^(?P<base>https?://[^/]*gitlab[^/]*/'
    r'(?:[^/]+/)+)-/blob/(?P<rest>.+)$'
)
# Bitbucket:  /owner/repo/src/ref/path  → /owner/repo/raw/ref/path
_BITBUCKET_SRC_RE = re.compile(
    r'^(?P<base>https?://bitbucket\.org/'
    r'[^/]+/[^/]+)/src/(?P<rest>.+)$'
)

# arXiv wrapper sites — JS SPAs that overlay comments/annotations on top of
# arXiv papers.  Static fetch returns only the SPA shell (nav buttons, toolbars),
# not the paper.  Rewrite to canonical arxiv.org for actual content.
# Matches: alphaxiv.org, arxiv-vanity.com, and similar wrappers
_ARXIV_WRAPPER_RE = re.compile(
    r'^https?://(?:www\.)?'
    r'(?P<host>alphaxiv\.org|arxiv-vanity\.com)'
    r'/(?P<path_type>abs|pdf|html|overview|resources)'
    r'/(?P<paper_id>\d{4}\.\d{4,5}(?:v\d+)?)\s*$'
)


def _normalize_code_hosting_url(url):
    """Rewrite code-hosting blob/view URLs to their raw-content equivalents.

    GitHub, GitLab, and Bitbucket serve source files as HTML pages at
    their default /blob/ (or /src/) URLs.  A plain HTTP GET returns
    navigation chrome, JS loaders, and feedback widgets — not the code.
    Rewriting to the raw endpoint gets us plain text/plain directly.

    Also normalizes arXiv wrapper sites (alphaxiv.org, arxiv-vanity.com)
    to canonical arxiv.org URLs — these wrappers are JS SPAs whose static
    HTML contains only navigation chrome, not paper content.

    Returns the rewritten URL, or the original URL unchanged if no rule matches.
    """
    # Strip trailing query params / fragments that don't affect raw content
    # (e.g. ?plain=1 on GitHub is for "plain view" but raw URL doesn't need it)
    clean = url.split('#')[0]  # strip fragment

    # ── arXiv wrapper sites → canonical arxiv.org ──
    m = _ARXIV_WRAPPER_RE.match(clean.strip())
    if m:
        paper_id = m.group('paper_id')
        path_type = m.group('path_type')
        host = m.group('host')
        # overview/resources are alphaxiv-specific pages — map to abs
        if path_type in ('overview', 'resources'):
            path_type = 'abs'
        arxiv_url = f'https://arxiv.org/{path_type}/{paper_id}'
        logger.info('[Fetch] arXiv wrapper %s → %s', host, arxiv_url)
        return arxiv_url

    # GitHub blob: do NOT rewrite — we extract code directly from the
    # embedded JSON payload in the HTML page (see html_extract._try_extract_github_blob).
    # This is more reliable than /raw/ endpoints which may be blocked by
    # corporate proxies (raw.githubusercontent.com) or rate-limited.

    # GitLab blob → raw
    m = _GITLAB_BLOB_RE.match(clean)
    if m:
        raw_url = f'{m.group("base")}-/raw/{m.group("rest")}'
        raw_url = raw_url.split('?')[0]
        logger.debug('[Fetch] GitLab blob → raw: %s', raw_url[:120])
        return raw_url

    # Bitbucket src → raw
    m = _BITBUCKET_SRC_RE.match(clean)
    if m:
        raw_url = f'{m.group("base")}/raw/{m.group("rest")}'
        raw_url = raw_url.split('?')[0]
        logger.debug('[Fetch] Bitbucket src → raw: %s', raw_url[:120])
        return raw_url

    return url


def _should_fetch(url):
    try:
        p = urlparse(url)
        if any(s in p.netloc.lower() for s in _lib.SKIP_DOMAINS): return False
        if any(p.path.lower().endswith(e) for e in
               ('.jpg','.jpeg','.png','.gif','.svg','.mp4','.mp3','.zip','.tar','.gz','.exe')):
            return False
        # 域名级熔断检查
        if _circuit.is_open(url):
            logger.warning('⚡ Skipped (circuit open): %s', url[:80])
            return False
        return True
    except Exception as e:
        logger.warning('Exception in guard clause for URL filter: %s', e, exc_info=True)
        return False
