"""lib/fetch/utils.py — Fetch infrastructure: sessions, SSL, circuit breaker, helpers.

Extracted from chatui lib/fetch.py to keep the main module focused on fetch logic.
All symbols are re-imported by tofu_search.fetch for backward compatibility.
"""

import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tofu_search.config import get_config

# Suppress InsecureRequestWarning for SSL-fallback retries
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Raise urllib3's connection-pool logger to ERROR so its transient
# "Retrying ... after connection broken by ReadTimeoutError" WARNINGs are
# muted (we already handle/log these at the requests level) — real pool
# errors are ERROR-and-above and still surface.
import logging as _logging

_logging.getLogger('urllib3.connectionpool').setLevel(_logging.ERROR)

from tofu_search.log import get_logger

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


# ═══════════════════════════════════════════════════════
#  Constants & compiled patterns
# ═══════════════════════════════════════════════════════

_URL_RE = re.compile(r'https?://[^\s<>"\')\]，。！？、）】}]+[^\s<>"\')\]，。！？、）】}.,:;!?]')

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}


# ═══════════════════════════════════════════════════════
#  连接池（带正确的重试策略）
# ═══════════════════════════════════════════════════════

class _SSRFGuardAdapter(HTTPAdapter):
    """HTTPAdapter that rejects requests to internal addresses.

    ``requests`` invokes ``adapter.send`` once per hop, so validating the
    request URL here guards the initial fetch AND every redirect target —
    closing the redirect-based SSRF hole that a one-shot pre-flight check on
    the original URL would miss. The check is a no-op when
    ``block_private_addresses`` is disabled in config.
    """
    def send(self, request, **kwargs):
        if get_config().block_private_addresses:
            host = urlparse(request.url).hostname or ''
            if not _host_is_safe(host):
                raise requests.exceptions.InvalidURL(
                    f'SSRF guard: blocked internal address for host {host!r}')
        return super().send(request, **kwargs)


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
_adapter = _SSRFGuardAdapter(pool_connections=100, pool_maxsize=100, max_retries=_retry_strategy)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

# 无 SSL 验证的 session（用于证书过期降级）
_session_no_ssl = requests.Session()
_session_no_ssl.headers.update(_HEADERS)
_session_no_ssl.verify = False
_adapter_no_ssl = _SSRFGuardAdapter(pool_connections=32, pool_maxsize=32, max_retries=_retry_strategy)
_session_no_ssl.mount('https://', _adapter_no_ssl)
_session_no_ssl.mount('http://', _adapter_no_ssl)

# 兼容 legacy TLS renegotiation 的 session（OpenSSL 3.x 默认禁止）
# 用于 chinamoney.org.cn, group.ccb.com 等老旧服务器
try:
    _legacy_ctx = ssl.create_default_context()
    _legacy_ctx.check_hostname = False
    _legacy_ctx.verify_mode = ssl.CERT_NONE
    _legacy_ctx.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)

    class _LegacySSLAdapter(_SSRFGuardAdapter):
        """SSRF-guarded HTTPAdapter that allows unsafe legacy TLS renegotiation."""
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
        pool_connections=32, pool_maxsize=32, max_retries=_retry_strategy))
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

_CACHE_EXTRACT_LIMIT = max(get_config().fetch_max_chars_direct, get_config().fetch_max_chars_search) * 2


class _FetchCache:
    """TTL-based URL content cache with LRU eviction.

    Tracks hits, misses, TTL expirations, and capacity evictions for
    diagnostic visibility. Stats accessible via ``stats`` property.
    """
    def __init__(self, ttl=600, max_size=200, name='fetch'):
        self._data, self._lock = {}, threading.Lock()
        self._ttl, self._max = ttl, max_size
        self._name = name
        # Diagnostic counters
        self._hits = 0
        self._misses = 0
        self._ttl_expirations = 0
        self._capacity_evictions = 0
        self._puts = 0

    def get(self, url):
        with self._lock:
            e = self._data.get(url)
            if e and e[1] > time.time():
                self._hits += 1
                return e[0]
            if e:
                # Entry exists but TTL expired
                self._ttl_expirations += 1
                self._data.pop(url, None)
                logger.debug('[%sCache] TTL expired for %.80s (ttl=%ds, size=%d)',
                             self._name, url, self._ttl, len(self._data))
            self._misses += 1
            return None

    def put(self, url, content):
        if not content:
            return
        with self._lock:
            self._puts += 1
            if len(self._data) >= self._max:
                evicted_url = min(self._data, key=lambda k: self._data[k][1])
                del self._data[evicted_url]
                self._capacity_evictions += 1
                logger.debug('[%sCache] Capacity eviction (%d/%d): %.80s',
                             self._name, len(self._data), self._max,
                             evicted_url)
            self._data[url] = (content, time.time() + self._ttl)

    @property
    def size(self):
        with self._lock:
            return len(self._data)

    @property
    def stats(self):
        """Return diagnostic stats dict."""
        with self._lock:
            total = self._hits + self._misses
            return {
                'name': self._name,
                'size': len(self._data),
                'max_size': self._max,
                'ttl': self._ttl,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate_pct': round(self._hits / max(total, 1) * 100),
                'ttl_expirations': self._ttl_expirations,
                'capacity_evictions': self._capacity_evictions,
                'puts': self._puts,
            }

_fetch_cache = _FetchCache(ttl=600, max_size=200, name='Fetch')
# Light cache: store raw HTML head (first 20KB) for publish-date extraction
_html_head_cache = _FetchCache(ttl=600, max_size=300, name='HtmlHead')


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
        'not a robot', 'verifying that you are',
        # Chinese variants
        '正在进行安全验证', '验证您不是自动程序', '安全服务防护',
        '人机验证', '正在检查您的浏览器', '正在检查浏览器',
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
    # ── JS-wall / robot-check / redirect stubs. Only matched against ≤600-char
    #    extractions (see _is_bot_extracted_text), so these short phrases carry
    #    very low false-positive risk — real articles extract far longer. ──
    'not a robot', 'verifying that you are', 'verify that you',
    'javascript is disabled', 'enable javascript', 'requires javascript',
    "required part of this site", 'does not redirect automatically',
    # ── Chinese variants (Cloudflare / bot-protection localized) ──
    '正在进行安全验证', '安全验证', '验证您不是自动程序',
    '安全检查', '请完成安全验证', '请稍候', '正在检查您的浏览器',
    '正在检查浏览器', '安全服务防护', 'ddos防护', '人机验证',
)


_LOGIN_WALL_PATTERNS = (
    # URL / host markers that land in extracted text of SSO redirect pages
    'ssosv.', 'sson/login', '/sso/web/auth', 'passport.', 'accounts.google',
    # Technical fingerprints of an SSO login bundle. These matter because a
    # login page can be LONG (the observed one extracts to ~1900 chars, mostly
    # inlined bootstrap JS) while carrying only ONE human-readable login phrase
    # — corroboration has to come from somewhere, and the auth bundle's own
    # identifiers are the most reliable second witness.
    'sso.auth.fe', 'qr code login', 'code_challenge',
    "get('client_id')", 'localstorage.getitem(\'_username\')',
    # English login-form vocabulary
    'forgot password', 'login center', 'sign in to continue',
    'log in to continue', 'please log in', 'please sign in',
    # Chinese login-form vocabulary
    '忘记密码', '二维码登录', '扫码登录', '登录您的账号', '请先登录',
    '账号登录', '密码登录', '登录后查看',
)


def _looks_like_login_wall(text):
    """True when extracted text is a login/SSO wall rather than page content.

    A login wall is a COMPLETE page, so "we got non-empty text" cannot
    distinguish it from a successful fetch — that conflation is what made an
    authenticated replay report the wall as its result. The judgement is
    therefore on the CONTENT, and length-bounded: a real article that merely
    mentions "忘记密码" in passing must not be misread as a wall, whereas a
    genuine wall is short (the observed SSO page extracts to ~25-1900 chars).

    Two or more independent markers are required past the very-short range, so
    a single incidental phrase in a longer page cannot trip it.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) > 4000:          # real content — never a bare wall
        return False
    lower = stripped.lower()
    hits = [p for p in _LOGIN_WALL_PATTERNS if p in lower]
    if not hits:
        return False
    # Very short + any marker = wall. Longer = demand corroboration.
    return len(stripped) <= 600 or len(hits) >= 2


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
    'volcengine.com',
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
      C) <noscript> 中包含 "enable JavaScript" 且有 SPA 挂载点 (最强信号 —
         即使导航菜单贡献了较多文字, 真实内容仍需 JS 渲染, 如 volcengine.com)
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

    # C) <noscript> 中明确要求启用 JavaScript + SPA 挂载点 = 确定是 SPA
    #    很多 SPA 框架 (React/Vue/Angular/Modern.js) 在 <noscript> 中放置
    #    "You need to enable JavaScript to run this app" 之类的提示。
    #    这种情况下即使 HTML 中有导航菜单等文字 (导致 text_len 较高),
    #    真实页面内容也必须靠 JS 渲染。只在有 SPA 挂载点时才触发，避免
    #    误判那些仅因增强功能而提示 JS 的非 SPA 页面。
    if has_spa_marker:
        # 搜索 <noscript> 块中的 JS-required 提示
        noscript_re = re.compile(r'<noscript[^>]*>(.*?)</noscript>', re.I | re.DOTALL)
        for m in noscript_re.finditer(html_str[:50000]):
            ns_text = m.group(1).lower()
            if any(kw in ns_text for kw in (
                'enable javascript', 'requires javascript',
                'javascript is required', 'javascript is disabled',
                'need to enable javascript', 'need javascript',
                'activate javascript', 'turn on javascript',
                '启用 javascript', '需要启用 javascript',
                '请启用 javascript', '开启 javascript',
            )):
                logger.debug('SPA noscript+marker detected (text=%d) — '
                             'JS required message in <noscript>', text_len)
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
        logger.info('[Fetch] arXiv wrapper %s -> %s', host, arxiv_url)
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
        logger.debug('[Fetch] GitLab blob -> raw: %s', raw_url[:120])
        return raw_url

    # Bitbucket src → raw
    m = _BITBUCKET_SRC_RE.match(clean)
    if m:
        raw_url = f'{m.group("base")}/raw/{m.group("rest")}'
        raw_url = raw_url.split('?')[0]
        logger.debug('[Fetch] Bitbucket src -> raw: %s', raw_url[:120])
        return raw_url

    return url


# ═══════════════════════════════════════════════════════
#  SSRF guard — block private / loopback / reserved targets
# ═══════════════════════════════════════════════════════

def _ip_is_blocked(ip_str: str) -> bool:
    """True if ``ip_str`` is a private / loopback / link-local / reserved address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _host_is_allowlisted(host: str) -> bool:
    """True if ``host`` matches an entry in ``allow_private_hosts``.

    Matches exactly, or as a parent-domain suffix so one entry covers a whole
    internal zone (``example-corp.com`` admits ``your-llm-gateway.example.com``). Suffix matching
    is anchored on a dot boundary, so ``evil-example-corp.com`` does NOT match
    ``example-corp.com``.
    """
    allow = getattr(get_config(), 'allow_private_hosts', None) or ()
    if not allow:
        return False
    host = host.strip().rstrip('.').lower()
    for entry in allow:
        entry = str(entry or '').strip().rstrip('.').lower().lstrip('.')
        if entry and (host == entry or host.endswith('.' + entry)):
            return True
    return False


def _host_is_safe(host: str) -> bool:
    """Return False if ``host`` resolves to (or literally is) a blocked address.

    Resolves the hostname and rejects the fetch if ANY resolved address is in a
    private / loopback / link-local / reserved range. This blocks SSRF to cloud
    metadata endpoints (169.254.169.254), localhost, and RFC-1918 networks.
    Called for the initial URL and re-checked on every redirect hop.
    """
    if not host:
        return False
    host = host.strip().strip('[]')  # strip IPv6 brackets
    # Literal IP?
    try:
        ipaddress.ip_address(host)
        return not _ip_is_blocked(host)
    except ValueError:
        pass
    # Explicitly allowlisted internal host — checked AFTER the literal-IP
    # branch so naming a host here can never launder a bare-IP target.
    if _host_is_allowlisted(host):
        logger.info('[Fetch] SSRF guard: host %s allowlisted (allow_private_hosts)', host)
        return True
    # Hostname → resolve all addresses; block if any is internal.
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Can't resolve — let the normal request path fail/log it.
        return True
    for info in infos:
        addr = info[4][0]
        if _ip_is_blocked(addr):
            logger.warning('[Fetch] SSRF guard: host %s resolves to blocked address %s', host, addr)
            return False
    return True


def _is_text_asset_ct(ct: str) -> bool:
    """True if a Content-Type denotes a TEXT-based file asset we can return as
    source directly (SVG, JSON, XML, YAML, CSS, JS, source code, …).

    Excludes ``text/html`` (that's the trafilatura extraction path) and
    ``text/plain`` (handled by its own dedicated branch upstream).
    """
    ct = (ct or '').lower()
    if 'html' in ct:
        return False
    if ct.startswith('text/'):  # text/css, text/markdown, text/csv, text/x-python, …
        return 'plain' not in ct
    return any(m in ct for m in (
        'image/svg', '+xml', '+json', 'application/json', 'application/xml',
        'application/javascript', 'application/x-yaml', 'application/x-yml',
        'application/toml',
    ))


# File extensions whose content is source/markup, NOT prose — fetched verbatim
# and must NOT be run through the article noise/relevance filter.
_TEXT_ASSET_EXTS = frozenset({
    '.svg', '.xml', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.csv', '.tsv', '.md', '.rst', '.css', '.scss', '.less',
    '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.py', '.rb', '.go', '.rs',
    '.java', '.kt', '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.php', '.swift',
    '.scala', '.sh', '.bash', '.zsh', '.sql', '.lua', '.pl', '.r',
    '.patch', '.diff', '.gradle', '.properties', '.tf',
})


def looks_like_text_asset(url: str) -> bool:
    """True if ``url``'s path ends in a known source/markup file extension.

    Lets a caller treat a fetched URL as a verbatim source file (skip the
    article content filter) rather than a prose web page.
    """
    try:
        ext = os.path.splitext(urlparse(url).path)[1].lower()
    except Exception:
        return False
    return ext in _TEXT_ASSET_EXTS


def _should_fetch(url, diag=None):
    """True when ``url`` may be fetched anonymously.

    ``diag`` is an optional dict the caller passes in to learn WHICH gate
    refused the URL: on a False return it receives ``reason`` (a stable
    machine token) and ``detail`` (a human sentence). Without it the function
    is byte-identical to its historical boolean-only behaviour.
    """
    def _refuse(reason, detail):
        if diag is not None:
            diag['reason'] = reason
            diag['detail'] = detail
        return False

    try:
        p = urlparse(url)
        # Reject local file paths that were mistakenly treated as URLs
        if not p.scheme or p.scheme not in ('http', 'https'):
            logger.debug('[Fetch] Skipping non-HTTP URL (scheme=%s): %.80s', p.scheme or 'none', url)
            return _refuse('bad_scheme',
                           'Not an HTTP(S) URL (scheme=%s).' % (p.scheme or 'none'))
        if not p.netloc:
            logger.debug('[Fetch] Skipping URL with no netloc: %.80s', url)
            return _refuse('bad_url', 'URL has no host component.')
        if any(s in p.netloc.lower() for s in get_config().skip_domains):
            return _refuse('skip_domain',
                           'Host %r is on the skip-domains list (JS/social app that '
                           'yields no extractable text).' % p.netloc)
        # ── SSRF guard: reject hosts that resolve to internal addresses ──
        if get_config().block_private_addresses and not _host_is_safe(p.hostname or ''):
            logger.warning('[Fetch] Skipped (SSRF guard, internal address): %s', url[:80])
            return _refuse('ssrf_blocked',
                           'Blocked by the SSRF guard: host %r resolves to a private / '
                           'loopback / reserved address. This is an internal host — add it '
                           'to allow_private_hosts to fetch it deliberately.'
                           % (p.hostname or ''))
        # Skip BINARY media (can't be extracted as text). ``.svg`` is text and
        # is handled by the text-asset branch in fetch_page_content, so it is
        # deliberately NOT in this list.
        if any(p.path.lower().endswith(e) for e in
               ('.jpg','.jpeg','.png','.gif','.mp4','.mp3','.zip','.tar','.gz','.exe')):
            return _refuse('binary_media',
                           'URL points at binary media that carries no extractable text.')
        # 域名级熔断检查
        if _circuit.is_open(url):
            # Expected + self-recovering: the circuit trip itself is already
            # logged at warning; per-URL skips at that level would spam.
            logger.debug('[Fetch] Skipped (circuit open) — %s', url[:80])
            return _refuse('circuit_open',
                           'Domain circuit-breaker is open after repeated failures; '
                           'it self-recovers after a cooldown.')
        return True
    except Exception as e:
        logger.warning('Exception in guard clause for URL filter: %s', e, exc_info=True)
        return _refuse('guard_error', 'URL guard raised %s: %s' % (type(e).__name__, e))
