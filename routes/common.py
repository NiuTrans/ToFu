"""routes/common.py — Shared utilities, auth stubs, static pages,
log compress, pricing, error tracking, server config.

Split from the original monolithic common.py:
  routes/conversations.py  — Conversation CRUD
  routes/upload.py         — Image upload/serve, image gen, PDF parse
  routes/translate.py      — Translation (sync + async)
"""

import json
import os
import re
import threading
import time
from functools import wraps

import sqlite3
from flask import Blueprint, Response, make_response, request, send_from_directory

import lib as _lib  # module ref for hot-reload
from lib.css_bundler import (
    get_styles_link_tag as _get_styles_link_tag,
    get_settings_link_tag as _get_settings_link_tag,
)
from lib.settings_panels import inject_panels as _inject_settings_panels, panels_signature as _settings_panels_signature
from lib.js_bundler import (
    get_bundle_script_tag_nonblocking as _get_bundle_tag,
    get_feature_bundle_filename_nonblocking as _get_feature_bundle_filename,
    get_i18n_pack_tag as _get_i18n_pack_tag,
    get_i18n_pack_urls as _get_i18n_pack_urls,
)
from lib.log import get_logger
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_ok,
)
from lib.request_parser import parse_body

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Shared Utilities (imported by conversations.py, etc.)
# ══════════════════════════════════════════════════════

def _db_safe(fn):
    """Decorator that catches DB OperationalError and returns JSON 503.

    Dual-mode: emits an ``async def`` wrapper for coroutine handlers and a sync
    wrapper otherwise. A sync passthrough wrapper around an ``async def`` view
    would make ``asyncio.iscoroutinefunction(wrapper)`` False, so Quart would
    run it in the thread pool and try to serialize the returned coroutine
    OBJECT as the response (broken / never-awaited). See CLAUDE.md and the
    async-migration-dual-mode-decorators convention.
    """
    import asyncio
    _db_errors = (sqlite3.OperationalError,)

    def _handle(e):
        err_msg = str(e)
        if 'database is locked' in err_msg:
            logger.warning('[%s] DB locked during %s %s — returning 503: %s',
                           fn.__name__, request.method, request.path, e)
            return api_error(
                'database_busy', status=503,
                message='Database temporarily busy, please retry.',
                retryAfter=2)
        logger.error('[%s] DB error during %s %s: %s',
                     fn.__name__, request.method, request.path, e, exc_info=True)
        raise e

    if asyncio.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except _db_errors as e:
                return _handle(e)
        return async_wrapper

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _db_errors as e:
            return _handle(e)
    return wrapper

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USER_ID = 1

# ── Native mobile client (Android APK) download ──
# The Android client's SOURCE lives in-tree at android/ (merged 2026-08-05),
# but its CI (.github/workflows/build-android-apk.yml) publishes release APKs
# to the SEPARATE github.com/rangehow/tofu-android repo's /releases — NOT this
# repo. That is deliberate: /releases/latest/download/<asset> resolves to the
# NEWEST release, and on this repo any newer desktop-only release (v0.14.x,
# no APK asset) would shadow the deep link into a permanent 404. The filename
# here MUST equal the asset that workflow publishes. The coupling is guarded
# by tests/test_mobile_client_apk_url.py so it can't silently drift into a 404.
MOBILE_CLIENT_APK_ASSET = 'tofu-android.apk'
# Direct-download DEEP LINK, not the releases HTML page. GitHub's
# /releases/latest/download/<asset> is a stable redirect that always serves the
# newest release's asset and triggers a real download on tap — exactly what a
# phone needs. Before the first tagged release this 404s (honest "not published
# yet"); that is a better mobile outcome than landing on the /releases/latest
# page full of wrong-platform desktop installers. TOFU_MOBILE_CLIENT_URL
# overrides it (e.g. to pin a specific version's asset).
DEFAULT_MOBILE_CLIENT_URL = (
    'https://github.com/rangehow/tofu-android/releases/latest/download/'
    + MOBILE_CLIENT_APK_ASSET
)

common_bp = Blueprint('common', __name__)
# v1 blueprint for the JSON routes (page-serving carve-outs above stay on common_bp).
from routes.api_v1.common import api_v1_common_bp  # noqa: E402

# ── In-memory cache for conversation metadata ──
# The implementation moved to lib/conversations/meta_cache.py (2026-06) to
# break the lib→routes circular import: lib-layer mutators invalidate the
# cache directly from lib now. These aliases keep the legacy private names
# working for route modules that still import them from here.
from lib.conversations.meta_cache import (  # noqa: E402,F401  — re-exported for route modules (conversations.py, chat.py)
    invalidate_meta_cache as _invalidate_meta_cache,
    notify_conv_changed as _notify_conv_changed,
    refresh_meta_cache_if_stale as _refresh_meta_cache_if_stale,
)


def _request_user_id():
    """Resolve the effective user_id for the current request thread.

    Returns the authenticated ``AuthContext.user_id`` when a login-bound
    session is present, else falls back to ``DEFAULT_USER_ID = 1``. Callable
    from any route handler; safe outside a request context (returns the
    default without raising).

    pt_abae3a85a92440fd (2026-07-25): the standard helper for threading
    request-thread user_id into ``notify_conv_changed`` and adjacent
    seams. Owner-approved wire (DO IT NOW): route callers use this, and
    background threads read ``task['_userId']`` via ``task_user_id`` (from
    lib.tasks_pkg.manager._registry), both landing at the same
    notify_conv_changed signature already accepting ``user_id=``.

    NOTE: single-user default (empty AuthContext.user_id) is preserved
    byte-identically — c6d1bd71 already coerces ``user_id == DEFAULT_USER_ID``
    to unscoped for the snapshot projection.
    """
    try:
        from routes.api_v1.auth import current_auth
        ctx = current_auth()
    except Exception as _e:  # noqa: BLE001 — outside request context / test env
        logger.debug('request user id: failed (%s)', _e)
        return DEFAULT_USER_ID
    uid = getattr(ctx, 'user_id', '') if ctx is not None else ''
    if not uid:
        return DEFAULT_USER_ID
    # If it looks like a numeric string, coerce so downstream str/int
    # comparisons behave uniformly with existing DEFAULT_USER_ID=1 semantics.
    try:
        return int(uid) if str(uid).isdigit() else uid
    except (TypeError, ValueError) as _e:
        logger.debug('request user id: unexpected type/unparseable (%s)', _e)
        return uid


# ══════════════════════════════════════════════════════
#  Auth Stubs (single-user)
# ══════════════════════════════════════════════════════

# /api/{me,login,logout,register} stubs removed 2026-05-29 — use /api/v1/users/{me,login,logout,signup}.

# ══════════════════════════════════════════════════════
#  Log Compress (LLM-powered)
# ══════════════════════════════════════════════════════

@api_v1_common_bp.route('/api/v1/logs/compress', methods=['POST'])
def log_compress():
    """Use a cheap LLM to intelligently compress verbose logs."""
    from lib.llm_dispatch import smart_chat as llm_chat

    data = parse_body()
    text = (data.get('text') or '').strip()
    if not text:
        return api_bad_request('No text provided')
    if len(text) > 60000:
        text = text[:60000] + '\n... [truncated]'

    system_prompt = (
        "你是一个**日志压缩器**。你的唯一任务是把冗长的日志/终端输出压缩为更精简的版本，同时不丢失任何有意义的信息。\n\n"
        "## 压缩规则（按优先级）\n"
        "1. **合并重复**：同一条消息因多个 worker/rank/GPU/进程而重复多次 → 只保留一条有代表性的，在行尾标注 `  ×N`\n"
        "   - 如果不同 rank 的值不同（如耗时、端口），保留一条代表值即可\n"
        "2. **去除纯噪音**：以下类型的行直接删除——\n"
        "   - 空行、纯分隔线（===、---）\n"
        "   - 进度条、百分比下载（Downloading: 45%）\n"
        "   - DEBUG 级别的内部调试信息（插件列表、动态维度推断等），除非其内容含 ERROR/异常\n"
        "3. **保留所有有意义的信息**：\n"
        "   - 所有 ERROR、WARNING 完整保留\n"
        "   - INFO 级别的关键事件（模型加载完成、服务启动就绪、配置参数）保留\n"
        "   - 版本号、模型名、GPU 类型等环境信息保留\n"
        "   - 不同内容的行即使格式类似也要保留（比如 2 条不同的 WARNING）\n"
        "4. **去掉日志前缀时间戳**：如 `INFO 03-10 17:29:39` → 去掉 `INFO 03-10 17:29:39` 前缀，只保留消息内容。"
        "   但如果时间信息本身有意义（如计算耗时差），则保留。\n"
        "5. **格式要求**：\n"
        "   - 直接输出压缩后的纯文本，不要包裹在 ``` 代码块中\n"
        "   - 不要添加任何解释、总结、标题\n"
        "   - 保留原始行的文字内容（不改写措辞），只做删减和标注 ×N\n"
    )

    try:
        content, usage = llm_chat(
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': text},
            ],
            max_tokens=min(len(text) // 2 + 2000, 16000),
            temperature=0,
            capability='cheap',
            log_prefix='[LogCompress]',
        )
        content = content.strip()
        if content.startswith('```'):
            content = re.sub(r'^```[^\n]*\n', '', content)
            content = re.sub(r'\n```\s*$', '', content)
            content = content.strip()
        return api_ok({'compressed': content, 'usage': usage})
    except Exception as e:
        logger.error('[LogCompress] Error: %s', e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════
#  Pricing
# ══════════════════════════════════════════════════════

@api_v1_common_bp.route('/api/v1/pricing', methods=['GET'])
@api_v1_common_bp.route('/api/v1/pricing/data', methods=['GET'])
def pricing_data():
    from lib.pricing import get_pricing_data
    return api_ok(get_pricing_data())

@api_v1_common_bp.route('/api/v1/pricing/refresh', methods=['POST'])
def pricing_refresh():
    from lib.pricing import get_pricing_data, refresh_pricing_async
    logger.info('[pricing_refresh] Triggered pricing data refresh')
    refresh_pricing_async()
    return api_ok(get_pricing_data())


# ══════════════════════════════════════════════════════
#  Dispatch Quota — 5-hour rolling request counts per model
# ══════════════════════════════════════════════════════

@api_v1_common_bp.route('/api/v1/dispatch/quota', methods=['GET'])
def dispatch_quota():
    """Return 5-hour rolling request stats aggregated by model.

    Response format:
    {
      "models": {
        "gemini-2.5-pro": { "requests_5h": 42, "total_requests": 120, "slots": 2, ... },
        ...
      },
      "total_requests_5h": 128,
      "total_requests_all": 600
    }
    """
    try:
        from lib.llm_dispatch import get_dispatcher
        d = get_dispatcher()
        slots = d.get_slots_info()
    except Exception as e:
        logger.warning('[dispatch/quota] Failed to get dispatcher info: %s', e)
        return api_ok({'models': {}, 'total_requests_5h': 0, 'total_requests_all': 0})

    from lib.dispatch_stats import aggregate_quota_by_model
    return api_ok(aggregate_quota_by_model(slots))


# ══════════════════════════════════════════════════════
#  Dispatch Key Stats — today's success rate per API key
#  (auto-disable < 50%, manual override)
# ══════════════════════════════════════════════════════

@api_v1_common_bp.route('/api/v1/dispatch/endpoint-metrics', methods=['GET'])
def dispatch_endpoint_metrics():
    """Return per-endpoint live performance metrics aggregated from slot stats.

    Aggregates over all slots that share the same base_url (one or many
    models hosted on a self-hosted box). Frontend uses this to render
    persistent ttft/latency/throughput/success-rate without manual probing.

    Response:
    {
      "endpoints": {
        "<base_url>": {
          "slots": int,
          "models": [str, ...],
          "providers": [provider_id, ...],
          "rpm_current": int, "rpm_limit": int,
          "inflight": int,
          "total_requests": int, "total_errors": int,
          "success_rate": float|null,
          "ttft_ms": float|null,        # weighted avg across slots
          "latency_ms": float|null,
          "throughput_tps": float|null,
          "last_success_ts": float,     # epoch seconds
          "last_error_ts": float,
          "last_error_msg": str,
          "available": bool,            # any slot in the bucket usable now
          "consecutive_errors": int     # max across slots
        }, ...
      },
      "ts": float
    }
    """
    try:
        from lib.llm_dispatch import get_dispatcher
        d = get_dispatcher()
        slots = d.get_slots_info()
    except Exception as e:
        logger.warning('[dispatch/endpoint-metrics] Failed: %s', e, exc_info=True)
        return api_ok({'endpoints': {}, 'ts': time.time()})

    from lib.dispatch_stats import aggregate_endpoint_metrics
    return api_ok(aggregate_endpoint_metrics(slots))


@api_v1_common_bp.route('/api/v1/dispatch/model-health', methods=['GET'])
def dispatch_model_health():
    """Return per-(provider, wire-model) runtime health for the Settings
    model cards: success rate, error counts, consecutive-error streaks, and
    any ACTIVE cooldown (the error-rate throttling imposed after repeated
    failures) with its remaining seconds + reason.

    Response: ``{providers: {provider_id: {model: {...}}}, ts}`` — see
    ``lib.dispatch_stats.aggregate_model_health`` for the row shape.
    """
    try:
        from lib.llm_dispatch import get_dispatcher
        d = get_dispatcher()
        slots = d.get_slots_info()
    except Exception as e:
        logger.warning('[dispatch/model-health] Failed: %s', e, exc_info=True)
        return api_ok({'providers': {}, 'ts': time.time()})

    from lib.dispatch_stats import aggregate_model_health
    return api_ok(aggregate_model_health(slots))


@api_v1_common_bp.route('/api/v1/dispatch/key-stats', methods=['GET'])
def dispatch_key_stats():
    """Return today's success/failure counts per API key.

    Response:
    {
      "day": "2026-04-18",
      "min_attempts": 5,
      "min_success_rate": 0.5,
      "providers": {
        "<provider_id>": {
          "<key_name>": {
            "success": int, "failure": int, "total": int,
            "success_rate": float|null,
            "auto_disabled": bool, "override": bool|null, "enabled": bool,
            "last_error": str
          }, ...
        }, ...
      }
    }
    """
    try:
        from lib.key_stats import get_all_stats
        snapshot = get_all_stats()
    except Exception as e:
        logger.warning('[dispatch/key-stats] Failed: %s', e, exc_info=True)
        return api_ok({'day': '', 'providers': {},
                        'min_attempts': 5, 'min_success_rate': 0.5})

    from lib.dispatch_stats import group_key_stats_by_provider
    return api_ok(group_key_stats_by_provider(snapshot))


@api_v1_common_bp.route('/api/v1/dispatch/key-override', methods=['POST'])
def dispatch_key_override():
    """Manually toggle a key on/off for today.

    Body: { "provider_id": str, "key_name": str, "enabled": bool|null }
    If enabled is null, the override is cleared (revert to auto-disable logic).
    """
    data = parse_body()
    prov_id = (data.get('provider_id') or '').strip()
    key_name = (data.get('key_name') or '').strip()
    enabled = data.get('enabled', None)
    if not key_name:
        return api_bad_request('key_name required')
    try:
        from lib.key_stats import clear_key_override, set_key_override
        if enabled is None:
            row = clear_key_override(prov_id, key_name)
        else:
            row = set_key_override(prov_id, key_name, bool(enabled))
    except Exception as e:
        logger.error('[dispatch/key-override] Failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'provider_id': prov_id, 'key_name': key_name,
                    'row': row})
# ══════════════════════════════════════════════════════
#  Static Pages & Favicon
# ══════════════════════════════════════════════════════

FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<defs><linearGradient id="t" x1="0" y1="0" x2=".5" y2="1"><stop offset="0%" stop-color="#fef8ec"/><stop offset="100%" stop-color="#fdf2d7"/></linearGradient>
<linearGradient id="f" x1="0" y1="0" x2=".2" y2="1"><stop offset="0%" stop-color="#fdf4dc"/><stop offset="100%" stop-color="#f5e8c8"/></linearGradient>
<linearGradient id="r" x1="0" y1="0" x2="1" y2=".7"><stop offset="0%" stop-color="#ecdcc0"/><stop offset="100%" stop-color="#dcc8a4"/></linearGradient></defs>
<path d="M15.3 4.6 L6.4 9.6 L16.3 16 L26.2 10.5Z" fill="url(#t)"/>
<path d="M6.4 9.6 L6.1 21.1 L17.2 27.2 L16.3 16Z" fill="url(#f)"/>
<path d="M16.3 16 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" fill="url(#r)"/>
<path d="M15.3 4.6 L6.4 9.6 L6.1 21.1 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" stroke="#1a1520" stroke-width=".6" stroke-linejoin="round" fill="none"/>
<rect x="7.8" y="14.2" width="2.6" height="3.3" rx=".3" fill="#1a1520"/><rect x="9.2" y="14.5" width=".9" height="1.2" rx=".2" fill="white" opacity=".9"/>
<rect x="13.1" y="16.5" width="2.6" height="3.8" rx=".3" fill="#1a1520"/><rect x="14.5" y="16.9" width=".9" height="1.3" rx=".2" fill="white" opacity=".9"/>
<path d="M10.1 20.1 Q12 21.6 13.9 20.1" stroke="#1a1520" stroke-width=".5" fill="none" stroke-linecap="round" opacity=".45"/>
<ellipse cx="8" cy="18.4" rx="1" ry=".7" fill="#ffaaa2" opacity=".5"/><ellipse cx="15" cy="21.3" rx="1.1" ry=".7" fill="#feaca5" opacity=".5"/>
</svg>'''


# ── Cached bundled index.html (avoids re-reading + regex on every page load) ──
# Keyed by (bundle_tag, styles_link_tag, html_mtime) so any of those changing
# triggers a re-render of the cached HTML.
_bundled_index_cache = {'tag': None, 'styles_tag': None, 'feature': None, 'html': None, 'mtime': 0, 'panels': None, 'lang': None}

# ── UI language as a SERVER-VISIBLE signal (Epic-E sub-part 1, owner-approved) ──
# The UI language has always lived in localStorage['tofu_ui_lang'], which the
# server cannot read. That made a per-language bundle impossible: an eagerly
# shipped single-language pack must be chosen at serve time by a server with no
# way to choose (see tests/test_i18n_split_blocked_on_lang_signal.py).
#
# Owner picked option A: mirror the language into a cookie so the server CAN
# choose. localStorage stays AUTHORITATIVE for the client; the cookie is a
# write-through mirror maintained by i18n.js (on boot and in setLanguage). The
# server only ever READS it, and treats anything unrecognised as the default —
# a hostile or stale cookie can therefore only ever select a real language,
# never inject a filename.
_UI_LANG_COOKIE = 'tofu_ui_lang'
_UI_LANGS = ('zh', 'en')
_UI_LANG_DEFAULT = 'zh'


def request_ui_lang():
    """Resolve the UI language for the current request from its cookie.

    Returns one of ``_UI_LANGS``, defaulting to ``_UI_LANG_DEFAULT``. Safe to
    call outside a request context. The whitelist is the security boundary:
    the value reaches a bundle filename, so it must never be attacker-shaped.
    """
    try:
        raw = (request.cookies.get(_UI_LANG_COOKIE) or '').strip().lower()
    except Exception as e:  # noqa: BLE001 — no request context (tests, workers)
        logger.debug('[Index] ui-lang cookie unavailable: %s', e)
        return _UI_LANG_DEFAULT
    return raw if raw in _UI_LANGS else _UI_LANG_DEFAULT

# Regex: match contiguous block of app script tags + interleaved HTML comments/blank lines.
# Two important details:
#   1. Path char class includes '/' so subdirectory scripts (e.g. static/js/ui/popups.js,
#      static/js/settings/branding.js, static/js/main/main_input_handling.js) are also
#      stripped — otherwise they'd survive the bundle replacement and load as duplicate
#      top-level <script> tags, causing "Identifier has already been declared" SyntaxErrors
#      (see CLAUDE.md §3.2.1).
#   2. The comment alternative `<!--[^<]*?-->` is body-restricted to chars that are NOT
#      `<`, which lets us safely span 1-2 line subpackage comments (e.g. between
#      core.js and core/folders.js) WITHOUT backtracking across whole HTML regions
#      that happen to contain other comments. Without this carve-out the regex either
#      fires multiple times (.*? doesn't cross \n → bundle injected per-block →
#      duplicate-IIFE crash) or matches enormous spans ([\s\S]*? backtracks across
#      <head> meta tags etc).
# The SINGLE source of truth for "which static/js/<path> is an app script the
# bundle replaces". Both the strip regex below AND the is_stripped_app_script()
# predicate consume this ONE sub-pattern, so a test can drive the predicate to
# assert manifest↔regex parity without re-typing (and thus drifting from) the
# pattern. The `(?!bundle-)` negative lookahead excludes the built bundle tag
# itself (bundle-<hash>.js) so re-serving the cached HTML doesn't strip its own
# injected tag. See CLAUDE.md §3.2.1 and tests/test_bundle_manifest_parity.py.
_APP_SCRIPT_SRC_SUBPATTERN = r'static/js/(?!bundle-)[\w./-]+\.js'

# Predicate form of the sub-pattern above (anchored at start). True iff the
# given <script src> value is an app script _APP_SCRIPTS_RE would strip — i.e.
# a file the bundle MUST rebuild. Used by the parity test to close the
# regex↔manifest edge structurally (never a re-typed copy of the pattern).
_APP_SCRIPT_SRC_RE = re.compile(_APP_SCRIPT_SRC_SUBPATTERN)


def is_stripped_app_script(src_attr):
    """Return True iff ``src_attr`` names an app script the bundle replaces.

    ``src_attr`` is a ``<script>`` ``src`` value (e.g.
    ``"static/js/foo.js?v=1"``). Returns True for every app script the
    ``_APP_SCRIPTS_RE`` strip pass removes from the served HTML, and False for
    the built ``bundle-<hash>.js`` tag (and anything not under ``static/js/``).
    Shares ``_APP_SCRIPT_SRC_SUBPATTERN`` with the strip regex so the two can
    never diverge.

    Args:
        src_attr: The raw ``src`` attribute value of a ``<script>`` tag.

    Returns:
        True if this src would be stripped as an app script, else False.
    """
    return bool(_APP_SCRIPT_SRC_RE.match(src_attr or ''))


_APP_SCRIPTS_RE = re.compile(
    r'(?:(?:<!--[^<]*?-->\n)|(?:<script defer src="' + _APP_SCRIPT_SRC_SUBPATTERN + r'[^"]*"[^>]*></script>\n)|(?:\s*\n))*'
    r'<script defer src="' + _APP_SCRIPT_SRC_SUBPATTERN + r'[^"]*"[^>]*></script>\n'
    r'(?:(?:<!--[^<]*?-->\n)|(?:<script defer src="' + _APP_SCRIPT_SRC_SUBPATTERN + r'[^"]*"[^>]*></script>\n)|(?:\s*\n))*'
)

# Regex: match the app stylesheet `<link>` tag (with whatever ?v=… is in the
# file) so we can swap it for a content-hashed version computed at request
# time. Vendor stylesheets (static/vendor/...) are intentionally NOT matched
# because they're versioned by their vendor URL and rarely change.
_APP_STYLES_RE = re.compile(
    r'<link rel="stylesheet" href="static/styles\.css(?:\?[^"]*)?">'
)

# Same treatment for the settings-specific stylesheet (static/settings.css),
# extracted from styles.css so a settings page's styles live near its markup.
_SETTINGS_STYLES_RE = re.compile(
    r'<link rel="stylesheet" href="static/settings\.css(?:\?[^"]*)?">'
)

def _serve_raw_index_with_panels():
    """Dev-fallback response: raw index.html (individual <script> tags) but
    WITH settings-panel fragments spliced in. Used when bundling/injection
    fails — the settings panels must still appear, so we can't just
    ``send_from_directory`` the marker-only file. Falls back to the bare file
    only if even reading it fails."""
    html_path = os.path.join(BASE_DIR, 'index.html')
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = _inject_settings_panels(f.read())
        resp = make_response(html)
        resp.content_type = 'text/html; charset=utf-8'
    except Exception as e:
        logger.warning('[Index] raw-index panel splice failed, serving bare file: %s', e)
        resp = send_from_directory(BASE_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'private, no-cache'
    return resp


@common_bp.route('/')
def index_page():
    bundle_tag = _get_bundle_tag()
    styles_tag = _get_styles_link_tag()
    ui_lang = request_ui_lang()
    if not bundle_tag:
        # Bundling failed — serve original index.html with individual scripts
        # (panels still spliced in so the settings modal isn't crippled).
        return _serve_raw_index_with_panels()

    # Deferred feature bundle (lazy-loaded by static/js/feature-loader.js). We
    # expose its hashed URL to the page as window.__FEATURE_BUNDLE_SRC__ via a
    # tiny inline <script> injected right before the core bundle tag. None when
    # there is nothing to defer (dev fallback / empty deferred manifest) — the
    # loader then no-ops and the deferred entry-point stubs are never installed.
    feature_name = None
    try:
        feature_name = _get_feature_bundle_filename()
    except Exception as _e_feat:
        logger.debug('[Index] feature bundle unavailable: %s', _e_feat)
    if feature_name:
        feature_tag = ('<script>window.__FEATURE_BUNDLE_SRC__='
                       f'"static/js/{feature_name}";</script>\n')
    else:
        feature_tag = ''

    # i18n single-language pack (Epic-E sub-part 1 slice 2). When the core
    # bundle excludes i18n.js, its per-language replacement MUST be injected
    # BEFORE the bundle tag (both are defer; document order decides), or t()
    # is undefined for every module. The pack URLs let setLanguage() fetch
    # the other language on demand. Both are None in the dual-bundle
    # fallback — inject nothing then, the dictionary is already in the bundle.
    pack_tag = _get_i18n_pack_tag(ui_lang) or ''
    pack_urls = _get_i18n_pack_urls()
    i18n_tag = (pack_tag + '\n') if pack_tag else ''
    if pack_urls:
        i18n_tag += ('<script>window.__I18N_PACK_URLS__='
                     + json.dumps(pack_urls) + ';</script>\n')

    # Use cached version if bundle tag, styles tag, AND index.html mtime
    # are all unchanged. Any of those changing → rebuild the served HTML.
    html_path = os.path.join(BASE_DIR, 'index.html')
    try:
        html_mtime = os.path.getmtime(html_path)
    except OSError as _e_audit:
        logger.debug('[common] index_page caught %s: %s', type(_e_audit).__name__, _e_audit)
        html_mtime = 0
    panels_sig = _settings_panels_signature()
    if (_bundled_index_cache['tag'] == bundle_tag
            and _bundled_index_cache['styles_tag'] == styles_tag
            and _bundled_index_cache['feature'] == feature_tag
            and _bundled_index_cache['mtime'] == html_mtime
            and _bundled_index_cache['panels'] == panels_sig
            and _bundled_index_cache['lang'] == ui_lang
            and _bundled_index_cache.get('i18n') == i18n_tag
            and _bundled_index_cache['html']):
        resp = make_response(_bundled_index_cache['html'])
        resp.content_type = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'private, no-cache'
        return resp

    # Read index.html and rewrite both:
    #   1. The contiguous block of <script defer src="static/js/*.js"> tags
    #      → single bundle tag with content-hashed filename.
    #   2. The single <link rel="stylesheet" href="static/styles.css">
    #      → hashed minified file static/styles-<hash>.css (lib/css_bundler.py).
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()

        # Pack tag FIRST (t() must be defined before the bundle executes),
        # feature_tag prepended so window.__FEATURE_BUNDLE_SRC__ is defined
        # BEFORE the core bundle (which contains feature-loader.js) executes.
        html = _APP_SCRIPTS_RE.sub(i18n_tag + feature_tag + bundle_tag + '\n', html)
        html = _APP_STYLES_RE.sub(styles_tag, html)
        html = _SETTINGS_STYLES_RE.sub(_get_settings_link_tag(), html)
        # Splice decoupled settings-panel fragments back in at their markers
        # (lib/settings_panels). Runs on the SAME rewrite pass; a missing
        # fragment leaves its marker visible + logs an error (never a silent
        # vanished tab).
        html = _inject_settings_panels(html)

        _bundled_index_cache['tag'] = bundle_tag
        _bundled_index_cache['styles_tag'] = styles_tag
        _bundled_index_cache['feature'] = feature_tag
        _bundled_index_cache['panels'] = panels_sig
        _bundled_index_cache['lang'] = ui_lang
        _bundled_index_cache['i18n'] = i18n_tag
        _bundled_index_cache['html'] = html
        _bundled_index_cache['mtime'] = html_mtime

        resp = make_response(html)
        resp.content_type = 'text/html; charset=utf-8'
    except Exception as e:
        logger.warning('[Index] Bundle injection failed, serving original: %s', e)
        return _serve_raw_index_with_panels()

    resp.headers['Cache-Control'] = 'private, no-cache'
    return resp

@common_bp.route('/login')
@common_bp.route('/login/')
@common_bp.route('/signup')
@common_bp.route('/signup/')
def login_signup_page():
    """Customer login / signup HTML.

    Same file serves both — the page picks login vs signup based on
    ``#signup`` URL fragment (no server-side branching needed). Lives
    next to ``static/dashboard.html``; no bundle dependency.
    """
    return send_from_directory(os.path.join(BASE_DIR, 'static'),
                                'login.html')


@common_bp.route('/dashboard')
@common_bp.route('/dashboard/')
def dashboard_page():
    """Customer-facing relay dashboard.

    Lightweight standalone HTML — wallet balance, redeem-code form,
    API-key issuance, usage chart, base-URL snippet, account panel.
    Served from the same Quart app as the chat UI but lives in a
    separate file (``static/dashboard.html``) so a relay operator can
    expose only ``/dashboard`` to customers via the reverse proxy
    while the chat UI stays admin-only.

    The page itself is plain HTML (no bundle dependency); all data
    comes from ``/api/v1/billing/*`` and ``/api/v1/keys`` over fetch.
    """
    return send_from_directory(os.path.join(BASE_DIR, 'static'),
                                'dashboard.html')


@common_bp.route('/admin')
@common_bp.route('/admin/')
def admin_page():
    """Relay-operator admin console (multi-user mode).

    Standalone HTML (``static/admin.html``) that reuses the dashboard
    shell and hosts the relay-admin panels (users / pricing / redeem
    codes / payments) previously embedded as hidden Settings tabs.

    The page itself is ALWAYS served — there is no server-side gate on
    the route, so it can never 401 a browser (per the project's "never
    trap a frontend user" rule). Authorization is decided client-side
    by ``static/js/relay-admin.js`` (mode must be ``multi-user`` and the
    principal must hold the ``admin`` scope) AND enforced server-side by
    every ``/api/v1/users`` / ``/api/v1/billing`` endpoint it calls. A
    non-admin sees only the "需要管理员权限" notice and cannot mutate
    anything.

    Settings (in ``index.html``) is therefore pure single-user config;
    managing OTHER users lives here, parallel to the customer-facing
    ``/dashboard``.
    """
    return send_from_directory(os.path.join(BASE_DIR, 'static'),
                                'admin.html')

@api_v1_common_bp.route('/api/v1/features')
def features():
    out = {
        'pptx_translate_enabled': getattr(_lib, 'PPTX_TRANSLATE_ENABLED', False),
        'cache_extended_ttl': getattr(_lib, 'CACHE_EXTENDED_TTL', False),
        'debug_mode': getattr(_lib, 'DEBUG_MODE', False),
        'optimizer_enabled': getattr(_lib, 'OPTIMIZER_ENABLED', True),
        'artifacts_enabled': getattr(_lib, 'ARTIFACTS_ENABLED', True),
    }
    # Registered plugin flags (e.g. trading_enabled) added dynamically.
    try:
        from lib.feature_registry import registered_flags
        for f in registered_flags():
            out[f.json_key] = bool(getattr(_lib, f.env_key, f.default))
    except Exception as e:
        logger.debug('[features] plugin flags unavailable: %s', e)
    return api_ok(out)


@api_v1_common_bp.route('/api/v1/features', methods=['POST'])
def save_features():
    from lib.features_store import apply_feature_updates
    result = apply_feature_updates(parse_body())
    if result.get('error'):
        return api_internal_error('internal_error')
    return api_ok(result)
@common_bp.route('/api/client-error', methods=['POST'])
def client_error():
    data = parse_body()
    message = (data.get('message') or 'unknown client error')[:2000]
    url = (data.get('url') or '')[:500]
    conv_count = data.get('conversationCount', '?')
    extra = data.get('extra')
    log_parts = ['[CLIENT-ERROR] %s' % message, 'url=%s' % url, 'convs=%s' % conv_count]
    if extra:
        if isinstance(extra, dict):
            if extra.get('source'):
                log_parts.append('source=%s:%s:%s' % (extra['source'], extra.get('line', '?'), extra.get('col', '?')))
            if extra.get('stack'):
                log_parts.append('stack=%s' % extra['stack'][:500])
        else:
            log_parts.append('extra=%s' % str(extra)[:500])
    # Respect the client-side severity so we don't spam error.log with
    # things the frontend only flagged as a warning (e.g. orphan-task
    # recovery, polling fallback, sync 409 conflicts).
    _msg_lower = message.lower()
    if '[debuglog][warn]' in _msg_lower or '[debuglog][info]' in _msg_lower:
        logger.warning('%s', ' | '.join(log_parts))
    else:
        logger.error('%s', ' | '.join(log_parts))
    return api_ok()
# ── DB liveness probe, DECOUPLED from /api/health (pt_afbaf3d7 ②) ─────────
# /api/health is the frontend's offline ARBITER (backend_offline_monitor: two
# failed probes → the red "backend offline" banner). It used to run
# ``SELECT 1`` INLINE, so a PG-on-FUSE stall (measured 4–7s Slow queries in
# error.log) pushed the health answer past the frontend's 3–4s probe budget —
# the banner went up while the process was perfectly alive. Liveness must
# never wait on disk I/O: ``db_responsive`` is refreshed by a daemon thread on
# a TTL and served from cache. The ONE bounded wait is the cold-start join, so
# the install-time runtime probe (healthcheck.py --runtime) still gets a real
# verdict on a healthy box without re-opening the stall window (2s ≪ 3s/4s).
_db_probe_cache = {'at': 0.0, 'responsive': None, 'error': '', 'ever': False}
_db_probe_lock = threading.Lock()
_DB_PROBE_TTL_S = 10.0
_DB_PROBE_COLD_JOIN_S = 2.0


def _refresh_db_probe():
    """Daemon-thread body: the ONE place a health-driven SELECT 1 runs."""
    try:
        from lib.database import get_thread_db
        get_thread_db().execute('SELECT 1').fetchone()
        _db_probe_cache['responsive'] = True
        _db_probe_cache['error'] = ''
    except Exception as e:
        _db_probe_cache['responsive'] = False
        _db_probe_cache['error'] = str(e)[:200]
        logger.warning('[Health] background DB probe failed: %s', e)
    finally:
        _db_probe_cache['ever'] = True
        _db_probe_cache['at'] = time.time()


def _db_responsive_for_health():
    """Kick a background refresh when the cache is stale; never blocks beyond
    the one-time cold-start join."""
    spawn = False
    with _db_probe_lock:
        if time.time() - _db_probe_cache['at'] >= _DB_PROBE_TTL_S:
            # Mark refresh-in-progress BEFORE spawning so a concurrent health
            # request doesn't spawn a second probe thread.
            _db_probe_cache['at'] = time.time()
            spawn = True
    if not spawn:
        return
    t = threading.Thread(target=_refresh_db_probe, daemon=True,
                         name='health-db-probe')
    t.start()
    if not _db_probe_cache['ever']:
        # Cold start only: bound the wait so the very first verdict is REAL.
        t.join(timeout=_DB_PROBE_COLD_JOIN_S)


@common_bp.route('/api/health')
def health_check():
    from lib.database import _BACKEND, db_available
    from lib.version import __version__
    result = {'ok': True, 'ts': int(time.time() * 1000), 'db_ok': db_available, 'version': __version__}

    # ── Per-process boot identity (robust restart verification) ──
    # The restart button re-execs in place (os.execv keeps the same PID +
    # start-time), so "health answered ok" cannot prove a NEW process replied.
    # bootId is a fresh uuid minted at module import — a re-exec re-imports and
    # gets a new one, while a lingering old process keeps its old one. The
    # restart client captures the pre-restart bootId and only succeeds when this
    # differs. cacheFixGen surfaces the loaded (in-memory) cache-fix version so
    # a stale-code restart is visible, not silently green. Best-effort.
    try:
        from lib import boot_identity as _bi
        result['pid'] = _bi.PID
        result['bootId'] = _bi.BOOT_ID
        result['cacheFixGen'] = _bi.cache_fix_gen()
        # Source-tree fingerprint (HEAD + uncommitted tracked edits) so the
        # restart client can prove the NEW process loaded the code the operator
        # edited — not just that SOME new process answered. None on a
        # non-git deploy; the client then falls back to the bootId-only rule.
        result['codeFingerprint'] = _bi.code_fingerprint()
    except Exception as _bi_e:
        logger.debug('[Health] boot identity unavailable: %s', _bi_e)

    # Native mobile-client download URL, surfaced in the Settings footer.
    # Defaults to a DIRECT APK deep link (see DEFAULT_MOBILE_CLIENT_URL) so a
    # phone tap downloads the app rather than landing on a wrong-platform
    # releases page; TOFU_MOBILE_CLIENT_URL overrides.
    _mobile_url = (os.environ.get('TOFU_MOBILE_CLIENT_URL') or '').strip() \
        or DEFAULT_MOBILE_CLIENT_URL
    result['mobile_client_url'] = _mobile_url

    # Report the active backend ('pg' or 'sqlite') — NOT a hardcoded value,
    # which previously mislabeled every PostgreSQL deployment as sqlite.
    result['db_engine'] = 'postgresql' if _BACKEND == 'pg' else 'sqlite'

    # DB connectivity — served from the background-probe cache (see above).
    # Deliberately does NOT flip result['ok']: `ok` reports PROCESS liveness
    # (what the offline arbiter consumes); a stalled DB degrades
    # db_responsive, never the liveness verdict.
    if db_available:
        _db_responsive_for_health()
        if _db_probe_cache['responsive'] is not None:
            result['db_responsive'] = _db_probe_cache['responsive']
            if _db_probe_cache['error']:
                result['db_error'] = _db_probe_cache['error']

    try:
        from lib.cross_dc import get_status
        cross_dc = get_status()
        if cross_dc.get('clusters'):
            result['cross_dc'] = cross_dc
    except Exception as e:
        logger.debug('[Health] cross_dc status unavailable: %s', e)
    return api_ok(result)

@common_bp.route('/favicon.ico')
@common_bp.route('/favicon.svg')
def favicon():
    return Response(FAVICON_SVG, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})

