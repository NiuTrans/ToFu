"""routes/common.py — Shared utilities, auth stubs, static pages,
log compress, pricing, error tracking, server config.

Split from the original monolithic common.py:
  routes/conversations.py  — Conversation CRUD
  routes/upload.py         — Image upload/serve, image gen, PDF parse
  routes/translate.py      — Translation (sync + async)
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from functools import wraps

import psycopg2
from flask import Blueprint, Response, abort, jsonify, make_response, request, send_from_directory

import lib as _lib  # module ref for hot-reload
from lib.config_dir import config_path as _config_path
from lib.log import get_logger
from lib.utils import safe_json as _safe_json

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Shared Utilities (imported by conversations.py, etc.)
# ══════════════════════════════════════════════════════

def _db_safe(fn):
    """Decorator that catches DB OperationalError and returns JSON 503."""
    _db_errors = (psycopg2.OperationalError,)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _db_errors as e:
            err_msg = str(e)
            if 'database is locked' in err_msg:
                logger.warning('[%s] DB locked during %s %s — returning 503: %s',
                               fn.__name__, request.method, request.path, e)
                return jsonify({
                    'error': 'database_busy',
                    'message': 'Database temporarily busy, please retry.',
                    'retryAfter': 2,
                }), 503
            logger.error('[%s] DB error during %s %s: %s',
                         fn.__name__, request.method, request.path, e, exc_info=True)
            raise
    return wrapper

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USER_ID = 1

common_bp = Blueprint('common', __name__)

# ── In-memory cache for conversation metadata ──
_meta_cache_lock = threading.Lock()
_meta_cache = {'data': None, 'etag': None, 'ts': 0, 'ttl': 120}
# ★ TTL set to 120s (was 5s). _invalidate_meta_cache() is called on every
# mutation (create/update/delete), so the TTL is a safety net, not the primary
# freshness mechanism. This eliminates redundant DB queries during idle periods
# and reduces round-trips through VS Code port forwarding.

def _invalidate_meta_cache():
    """Call after any conversation mutation (save / delete)."""
    with _meta_cache_lock:
        _meta_cache['ts'] = 0

def _refresh_meta_cache_if_stale(db):
    """Return (json_bytes, etag). Re-query DB only if TTL expired."""
    now = time.monotonic()
    with _meta_cache_lock:
        if _meta_cache['data'] is not None and (now - _meta_cache['ts']) < _meta_cache['ttl']:
            return _meta_cache['data'], _meta_cache['etag']

    rows = db.execute(
        '''SELECT id, title, created_at, updated_at, settings, msg_count
           FROM conversations WHERE user_id=? ORDER BY updated_at DESC''',
        (DEFAULT_USER_ID,)
    ).fetchall()
    convs = []
    for r in rows:
        settings = _safe_json(r['settings'], default=None, label='settings')
        convs.append({
            'id': r['id'], 'title': r['title'],
            'messageCount': r['msg_count'] or 0,
            'createdAt': r['created_at'], 'created_at': r['created_at'],
            'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
            'settings': settings,
        })
    payload = json.dumps(convs, ensure_ascii=False).encode('utf-8')
    etag = hashlib.md5(payload).hexdigest()[:16]

    with _meta_cache_lock:
        _meta_cache['data'] = payload
        _meta_cache['etag'] = etag
        _meta_cache['ts'] = time.monotonic()
    return payload, etag


# ══════════════════════════════════════════════════════
#  Auth Stubs (single-user)
# ══════════════════════════════════════════════════════

@common_bp.route('/api/me')
def me():
    return jsonify({'authenticated': True, 'username': 'default', 'displayName': 'User'})

@common_bp.route('/api/login', methods=['POST'])
def login():
    return jsonify({'ok': True})

@common_bp.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'ok': True})

@common_bp.route('/api/register', methods=['POST'])
def register():
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════
#  Log Compress (LLM-powered)
# ══════════════════════════════════════════════════════

@common_bp.route('/api/log/compress', methods=['POST'])
def log_compress():
    """Use a cheap LLM to intelligently compress verbose logs."""
    from lib.llm_dispatch import smart_chat as llm_chat

    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'No text provided'}), 400
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
        return jsonify({'compressed': content, 'usage': usage})
    except Exception as e:
        logger.error('[LogCompress] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════
#  Pricing
# ══════════════════════════════════════════════════════

@common_bp.route('/api/pricing', methods=['GET'])
@common_bp.route('/api/pricing/data', methods=['GET'])
def pricing_data():
    from lib.pricing import get_pricing_data
    return jsonify(get_pricing_data())

@common_bp.route('/api/pricing/refresh', methods=['POST'])
def pricing_refresh():
    from lib.pricing import get_pricing_data, refresh_pricing_async
    logger.info('[pricing_refresh] Triggered pricing data refresh')
    refresh_pricing_async()
    return jsonify(get_pricing_data())


# ══════════════════════════════════════════════════════
#  Dispatch Quota — 5-hour rolling request counts per model
# ══════════════════════════════════════════════════════

@common_bp.route('/api/dispatch/quota', methods=['GET'])
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
        return jsonify({'models': {}, 'total_requests_5h': 0, 'total_requests_all': 0})

    # Aggregate by model
    models = {}
    total_5h = 0
    total_all = 0
    for s in slots:
        m = s['model']
        r5h = s.get('requests_5h', 0)
        r_all = s.get('total_requests', 0)
        total_5h += r5h
        total_all += r_all
        if m not in models:
            models[m] = {
                'requests_5h': 0,
                'total_requests': 0,
                'slots': 0,
                'rpm_current': 0,
                'rpm_limit': 0,
                'avg_latency_ms': 0,
                'inflight': 0,
                'provider_id': s.get('provider_id', ''),
            }
        entry = models[m]
        entry['requests_5h'] += r5h
        entry['total_requests'] += r_all
        entry['slots'] += 1
        entry['rpm_current'] += s.get('rpm_current', 0)
        entry['rpm_limit'] += s.get('rpm_limit', 0)
        entry['inflight'] += s.get('inflight', 0)
        entry['avg_latency_ms'] = round(
            (entry['avg_latency_ms'] * (entry['slots'] - 1) + s.get('latency_ema_ms', 0))
            / entry['slots'], 1)

    return jsonify({
        'models': models,
        'total_requests_5h': total_5h,
        'total_requests_all': total_all,
    })

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


@common_bp.route('/')
def index_page():
    resp = send_from_directory(BASE_DIR, 'index.html')
    # ★ Brief cache for SPA shell — eliminates redundant re-fetches through
    #   VS Code port forwarding on page reloads. index.html changes are rare;
    #   a hard-refresh (Ctrl+Shift+R) bypasses this when needed.
    resp.headers['Cache-Control'] = 'private, max-age=60'
    return resp

@common_bp.route('/trading.html')
def trading_page():
    if not _lib.TRADING_ENABLED:
        abort(404)
    return send_from_directory(BASE_DIR, 'trading.html')

@common_bp.route('/api/features')
def features():
    return jsonify({'trading_enabled': _lib.TRADING_ENABLED})


@common_bp.route('/api/features', methods=['POST'])
def save_features():
    data = request.get_json(silent=True) or {}
    features_path = _config_path('features.json')
    existing = {}
    try:
        if os.path.isfile(features_path):
            with open(features_path) as f:
                existing = json.load(f)
    except Exception as e:
        logger.warning('[Features] Failed to read features.json: %s', e)

    changed = []
    if 'trading_enabled' in data:
        new_val = bool(data['trading_enabled'])
        old_val = existing.get('trading_enabled', None)
        existing['trading_enabled'] = new_val
        if old_val != new_val:
            changed.append('trading_enabled')
            logger.info('[Features] trading_enabled: %s → %s', old_val, new_val)
    try:
        os.makedirs(os.path.dirname(features_path), exist_ok=True)
        with open(features_path, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error('[Features] Failed to write features.json: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Hot-reload TRADING_ENABLED on the lib module
    if 'trading_enabled' in changed:
        _lib.TRADING_ENABLED = existing.get('trading_enabled', False)
        logger.info('[Features] Hot-reloaded TRADING_ENABLED → %s '
                    '(note: trading route registration requires restart)',
                    _lib.TRADING_ENABLED)

    return jsonify({'ok': True, 'saved': existing,
                    'needs_restart': False, 'changed': changed})


@common_bp.route('/api/client-error', methods=['POST'])
def client_error():
    data = request.get_json(silent=True) or {}
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
    logger.error('%s', ' | '.join(log_parts))
    return jsonify({'ok': True})

@common_bp.route('/api/health')
def health_check():
    from lib.database import pg_available
    from lib.version import __version__
    return jsonify({'ok': True, 'ts': int(time.time() * 1000), 'db_ok': pg_available, 'version': __version__})

@common_bp.route('/favicon.ico')
@common_bp.route('/favicon.svg')
def favicon():
    return Response(FAVICON_SVG, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})

