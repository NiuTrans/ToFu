#!/usr/bin/env python3
"""Tofu Server

Refactored: all route handlers live in routes/ package.
This file handles:
  - App creation & Flask configuration
  - Tunnel token authentication (TUNNEL_TOKEN env var)
  - Gateway prefix middleware (GatewayMiddleware)
  - Request lifecycle logging & method override middleware
  - Database initialisation & NFS warmup
  - Background worker orchestration (trading intel, autopilot)
  - Feishu Bot startup (optional, via FEISHU_APP_ID/SECRET)
  - Werkzeug SSE streaming monkey-patch for tunnel environments
"""

import os, io, sys, json, logging, time, hashlib

# ══════════════════════════════════════════
#  Auto-delegate to bootstrap.py on missing deps
# ══════════════════════════════════════════
# When server.py is launched directly (not via bootstrap.py) and a
# package import fails (e.g. flask not installed in a fresh conda env),
# automatically re-exec through bootstrap.py which provides LLM-guided
# dependency repair with a live status page in the browser.
#
# If bootstrap.py doesn't exist or we're already running under it,
# the guard is skipped and normal Python error handling applies.

_BOOTSTRAP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'bootstrap.py')

if (os.environ.get('_CHATUI_VIA_BOOTSTRAP') != '1'
        and os.path.isfile(_BOOTSTRAP_PATH)):

    def _bootstrap_excepthook(exc_type, exc_value, exc_tb):
        """Intercept ImportError at module level → delegate to bootstrap.py."""
        if issubclass(exc_type, ImportError):
            # Print the original traceback so the user sees what's missing
            import traceback as _tb
            sys.stderr.write('\n')
            _tb.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
            sys.stderr.write(
                '\n\033[33m[server.py] Missing dependency detected. '
                'Delegating to bootstrap.py for auto-repair…\033[0m\n\n')
            sys.stderr.flush()
            # os.execv replaces the current process entirely — no return
            os.execv(sys.executable, [sys.executable, _BOOTSTRAP_PATH])
        # Non-import errors: use Python's built-in default handler
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _bootstrap_excepthook


# ── Auto-load .env if present (enables migrate.py workflow) ──
def _load_dotenv():
    _dotenv_log = logging.getLogger('server.dotenv')
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_path):
        _dotenv_log.debug('.env not found at %s — skipping', env_path)
        return
    loaded_keys = []
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key not in os.environ:   # explicit env takes priority
                os.environ[key] = value
                loaded_keys.append(key)
    if loaded_keys:
        _dotenv_log.info('Loaded %d env var(s) from .env: %s', len(loaded_keys), ', '.join(loaded_keys))
    else:
        _dotenv_log.debug('.env found but no new vars loaded (all already set)')
_load_dotenv()

# ══════════════════════════════════════════
#  Proxy bypass — auto-managed by lib/proxy.py
# ══════════════════════════════════════════
# NO_PROXY / no_proxy is now auto-synced from the unified bypass domains
# list in lib/proxy.py (_sync_no_proxy), which combines:
#   - env PROXY_BYPASS_DOMAINS
#   - Settings UI bypass domains
#   - standard always-bypass entries (localhost, 127.0.0.1)
# The old manual merge here is no longer needed — lib/proxy.py import
# triggers _rebuild() + _sync_no_proxy() automatically.
from flask import Flask, request, make_response, redirect, jsonify
from flask_compress import Compress

# ── MIME type safety net (macOS / Windows compat) ──
# Python's mimetypes module reads system MIME databases which may be
# incomplete on some platforms (e.g., macOS without /etc/mime.types).
# Explicitly register critical types to ensure Flask serves static
# assets with correct Content-Type headers.
# IMPORTANT: call init() FIRST so the system DB is loaded, THEN add_type()
# to override with our known-good values.  Without init(), add_type() sets
# inited=True which prevents the system DB from ever loading — causing
# other extensions (e.g. .woff2, .ttf) to return None on macOS.
import mimetypes
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/json', '.json')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/ttf', '.ttf')
mimetypes.add_type('application/wasm', '.wasm')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════
#  Logging — structured, multi-file, noise-free
# ══════════════════════════════════════════
#
# Log files produced:
#   logs/app.log     — Business logic only (lib.*, routes.*, server)  INFO+
#   logs/access.log  — HTTP request log (werkzeug), with noisy polls filtered
#   logs/error.log   — WARNING/ERROR/CRITICAL from ALL sources
#   logs/vendor.log  — Third-party libraries (trafilatura, websockets …)
#   logs/audit.log   — Structured JSON audit trail (unchanged)
#
# Console: business INFO+ and access log (no vendor noise)
#
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# --- Central log format ---
_LOG_FMT = '%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'
_formatter = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

# ── Filters ──

_BIZ_PREFIXES = ('lib.', 'routes.', 'server')   # business code only (no werkzeug)

class _BizOnly(logging.Filter):
    """Pass only records from our own business code (lib.*, routes.*, server)."""
    def filter(self, record):
        return record.name.startswith(_BIZ_PREFIXES)

class _VendorOnly(logging.Filter):
    """Pass only records from third-party libraries (not biz, not werkzeug)."""
    def filter(self, record):
        return (not record.name.startswith(_BIZ_PREFIXES)
                and record.name != 'werkzeug')

class _WerkzeugOnly(logging.Filter):
    """Pass only werkzeug (HTTP access) records."""
    def filter(self, record):
        return record.name == 'werkzeug'

class _QuietPollFilter(logging.Filter):
    """Suppress noisy HTTP polling endpoints from werkzeug access log."""
    _NOISY_PATHS = ('/api/chat/poll/', '/api/chat/stream/', '/api/browser/commands')
    def filter(self, record):
        msg = record.getMessage()
        if any(p in msg for p in self._NOISY_PATHS) and '" 200' in msg:
            return False
        return True

class _BizAndAccessFilter(logging.Filter):
    """Pass business code + werkzeug (for console output)."""
    def filter(self, record):
        return (record.name.startswith(_BIZ_PREFIXES)
                or record.name == 'werkzeug')


# ── Handler 1: logs/app.log — business logic, INFO+ (daily rotation) ──
_app_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'app.log'),
    when='midnight', backupCount=30, encoding='utf-8')
_app_handler.setFormatter(_formatter)
_app_handler.setLevel(logging.INFO)
_app_handler.addFilter(_BizOnly())

# ── Handler 2: logs/access.log — HTTP requests (werkzeug), INFO+ ──
_access_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'access.log'),
    when='midnight', backupCount=14, encoding='utf-8')
_access_handler.setFormatter(_formatter)
_access_handler.setLevel(logging.INFO)
_access_handler.addFilter(_WerkzeugOnly())

# ── Handler 3: logs/error.log — all WARNING/ERROR/CRITICAL ──
_error_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'error.log'),
    maxBytes=5 * 1024 * 1024, backupCount=10, encoding='utf-8')
_error_handler.setFormatter(_formatter)
_error_handler.setLevel(logging.WARNING)

# ── Handler 4: logs/vendor.log — third-party libs, WARNING+ ──
_vendor_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'vendor.log'),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
_vendor_handler.setFormatter(_formatter)
_vendor_handler.setLevel(logging.WARNING)
_vendor_handler.addFilter(_VendorOnly())

# ── Handler 5: console — business + access, INFO+ ──
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_formatter)
_console_handler.setLevel(logging.INFO)
_console_handler.addFilter(_BizAndAccessFilter())

# ── Root logger: route everything through our handlers ──
logging.basicConfig(
    level=logging.INFO,
    handlers=[_app_handler, _access_handler, _error_handler,
              _vendor_handler, _console_handler],
)

# ── Suppress noisy third-party libs at source ──
# These libraries generate thousands of DEBUG/INFO lines per hour.
# Only let WARNING+ through to vendor.log.
_NOISY_LIBS = (
    'trafilatura', 'courlan', 'htmldate', 'justext',  # web scraping internals
    'urllib3', 'requests', 'charset_normalizer',        # HTTP internals
    'websockets', 'websockets.client',                  # Feishu WS heartbeats
    'PIL', 'pymupdf',                                   # media libraries
    'httpcore', 'httpx',                                 # async HTTP
)
for _lib in _NOISY_LIBS:
    logging.getLogger(_lib).setLevel(logging.WARNING)

# ── Quiet polling endpoints on werkzeug access log ──
logging.getLogger('werkzeug').addFilter(_QuietPollFilter())


from lib.database import close_db, init_db, warmup_db


# ══════════════════════════════════════════
#  Flask App
# ══════════════════════════════════════════

app = Flask(__name__,
            static_folder=os.path.join(BASE_DIR, 'static'),
            static_url_path='/static')
app.secret_key = 'not-needed-single-user'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/javascript',
    'application/javascript', 'application/json',
    # ❌ Do NOT compress text/event-stream!  gzip internally buffers small
    # data chunks, which delays SSE event delivery — especially severe in
    # tunnel environments like VSCode port-forwarding.
]
app.config['COMPRESS_MIN_SIZE'] = 256  # only compress responses >256 bytes
Compress(app)
app.teardown_appcontext(close_db)


# ── Gateway Middleware (strip leading /gateway prefix) ──

class GatewayMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path.startswith('/gateway'):
            environ['PATH_INFO'] = path[len('/gateway'):]
            environ['SCRIPT_NAME'] = environ.get('SCRIPT_NAME', '') + '/gateway'
            logging.getLogger('server.gateway').debug(
                'GatewayMiddleware: stripped /gateway prefix → %s', environ['PATH_INFO'])
        return self.wsgi_app(environ, start_response)


app.wsgi_app = GatewayMiddleware(app.wsgi_app)


# ── Tunnel auth (protects public-facing tunnel access) ──

TUNNEL_TOKEN = os.environ.get('TUNNEL_TOKEN', '')  # set to enable auth
TUNNEL_COOKIE = '_tunnel_auth'
TUNNEL_COOKIE_MAX_AGE = 86400 * 30  # 30 days

@app.before_request
def tunnel_auth():
    """Simple token-based auth for public tunnel access.
    
    - If TUNNEL_TOKEN env is not set → auth disabled (pure LAN mode)
    - First visit: append ?token=<your_token> to any URL
    - Sets a cookie so subsequent requests are transparent
    - API calls can also use header: X-Tunnel-Token: <token>
    """
    _auth_log = logging.getLogger('server.auth')
    if not TUNNEL_TOKEN:
        return  # Auth disabled — LAN-only mode

    # Check cookie first (browser sessions)
    cookie_val = request.cookies.get(TUNNEL_COOKIE)
    expected = hashlib.sha256(TUNNEL_TOKEN.encode()).hexdigest()[:32]
    if cookie_val == expected:
        _auth_log.debug('Tunnel auth: valid cookie for %s', request.remote_addr)
        return  # ✅ Already authenticated
    elif cookie_val is not None:
        _auth_log.debug('Tunnel auth: cookie present but invalid for %s', request.remote_addr)

    # Check header (API / programmatic access)
    header_token = request.headers.get('X-Tunnel-Token', '')
    if header_token == TUNNEL_TOKEN:
        _auth_log.debug('Tunnel auth: valid header token for %s', request.remote_addr)
        return  # ✅ Valid header

    # From here on, all paths are rejections (except valid query token below).
    # Log the rejection attempt for audit / debugging.

    # Check query param (first-time login from browser)
    query_token = request.args.get('token', '')
    if query_token == TUNNEL_TOKEN:
        # Set cookie and redirect to clean URL (strip ?token=)
        from urllib.parse import urlencode, parse_qs, urlparse, urlunparse
        parsed = urlparse(request.url)
        params = parse_qs(parsed.query)
        params.pop('token', None)
        clean_query = urlencode(params, doseq=True)
        clean_url = urlunparse(parsed._replace(query=clean_query))
        resp = make_response(redirect(clean_url))
        resp.set_cookie(TUNNEL_COOKIE, expected,
                        max_age=TUNNEL_COOKIE_MAX_AGE,
                        httponly=True, samesite='Lax')
        _auth_log.info('Tunnel auth: token accepted, cookie set for %s', request.remote_addr)
        return resp

    # ❌ Not authenticated — return 401
    _auth_log.warning('Tunnel auth: 401 rejected %s %s from %s (UA: %s)',
                      request.method, request.path, request.remote_addr,
                      request.headers.get('User-Agent', '<none>'))
    return make_response(
        '<h2>🔒 Access Denied</h2>'
        '<p>Append <code>?token=YOUR_TOKEN</code> to the URL to authenticate.</p>',
        401
    )


@app.before_request
def method_override():
    _mo_log = logging.getLogger('server.method_override')
    override = request.args.get('_method')
    if override:
        original = request.environ['REQUEST_METHOD']
        request.environ['REQUEST_METHOD'] = override.upper()
        _mo_log.info('HTTP method overridden: %s → %s for %s', original, override.upper(), request.path)
    # CloudIDE nested JSON fix
    ct = request.content_type or ''
    if request.method in ('POST', 'PUT') and 'json' in ct:
        raw = request.get_data(as_text=True)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, str):
                    data = json.loads(data)
                    body_bytes = json.dumps(data).encode('utf-8')
                    request.environ['wsgi.input'] = io.BytesIO(body_bytes)
                    request.environ['CONTENT_LENGTH'] = str(len(body_bytes))
                    request.environ['CONTENT_TYPE'] = 'application/json'
                    _mo_log.debug('CloudIDE double-encoded JSON fix applied for %s %s', request.method, request.path)
            except (json.JSONDecodeError, TypeError) as e:
                _mo_log.warning('method_override JSON re-parse failed: %s', e, exc_info=True)


# ── Request lifecycle logging ──
# Assigns a unique request ID to every request and logs timing.
# Skip noisy polling & static-file endpoints.

from lib.log import get_logger, set_req_id, req_id as _get_req_id
import uuid as _uuid

_lifecycle_log = get_logger('server.lifecycle')

# Paths that are polled frequently — log at DEBUG only
_QUIET_PREFIXES = ('/api/browser/', '/api/desktop/', '/static/', '/api/task/')
_SLOW_THRESHOLD_S = 2.0  # warn if response takes longer

@app.before_request
def _assign_req_id_and_log():
    """Assign a unique request ID and log request entry."""
    rid = request.headers.get('X-Request-ID') or _uuid.uuid4().hex[:12]
    set_req_id(rid)
    request._start_time = time.time()

    path = request.path
    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)
    level = logging.DEBUG if is_quiet else logging.INFO
    _lifecycle_log.log(level, '[%s] → %s %s', rid, request.method, path)

@app.after_request
def _log_response(response):
    """Log response status and duration for every request."""
    elapsed = time.time() - getattr(request, '_start_time', time.time())
    rid = _get_req_id()
    path = request.full_path.rstrip('?')  # include query string
    status = response.status_code

    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)

    if status >= 500:
        _lifecycle_log.error('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)
    elif status >= 400:
        # Suppress noisy Chrome/Safari DevTools probes and favicon
        if status == 404 and request.path.startswith('/.well-known/'):
            _lifecycle_log.debug('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)
        else:
            _lifecycle_log.warning('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)
    elif elapsed >= _SLOW_THRESHOLD_S and not is_quiet:
        _lifecycle_log.warning('[%s] ← %s %s %d SLOW (%.3fs)', rid, request.method, path, status, elapsed)
    elif not is_quiet:
        _lifecycle_log.info('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)
    else:
        _lifecycle_log.debug('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)

    response.headers['X-Request-ID'] = rid

    # ── Force correct MIME type for static assets ──
    # Fixes macOS/Windows environments where Python's mimetypes module
    # may return text/plain for .js files, causing browsers to silently
    # refuse script execution.
    if request.path.startswith('/static/'):
        if request.path.endswith('.js'):
            response.content_type = 'application/javascript; charset=utf-8'
        elif request.path.endswith('.css'):
            response.content_type = 'text/css; charset=utf-8'

    return response

@app.teardown_request
def _clear_req_id(exc):
    """Clear request ID after request is fully handled."""
    if exc:
        rid = _get_req_id()
        _lifecycle_log.error('[%s] Request teardown with exception', rid, exc_info=exc)
    set_req_id(None)


# ══════════════════════════════════════════
#  Register all Blueprints
# ══════════════════════════════════════════

from routes import register_all
register_all(app)


# ── Load persisted proxy config from server_config.json ──
try:
    from routes.config import _read_server_config, _write_server_config
    from lib.proxy import set_bypass_domains, set_proxy_config
    _saved_cfg = _read_server_config()

    # ── Migration: merge legacy proxy_config.no_proxy into proxy_bypass_domains ──
    _saved_pc = _saved_cfg.get('proxy_config', {})
    _legacy_no_proxy = _saved_pc.get('no_proxy', '')
    _migrated = False
    if _legacy_no_proxy:
        _existing_bypass = _saved_cfg.get('proxy_bypass_domains', [])
        _existing_set = set(d.lower().strip() for d in _existing_bypass)
        for _d in _legacy_no_proxy.split(','):
            _d = _d.strip()
            if _d and _d.lower() not in _existing_set and _d not in ('localhost', '127.0.0.1', '0.0.0.0'):
                _existing_bypass.append(_d)
                _existing_set.add(_d.lower())
                _migrated = True
        if _migrated:
            _saved_cfg['proxy_bypass_domains'] = _existing_bypass
            _saved_pc.pop('no_proxy', None)
            _saved_cfg['proxy_config'] = _saved_pc
            _write_server_config(_saved_cfg)
            _lifecycle_log.info('[Proxy] Migrated legacy no_proxy entries into proxy_bypass_domains: %s',
                               ', '.join(_existing_bypass))

    # Proxy address (http_proxy / https_proxy — no_proxy is now auto-managed)
    if _saved_pc and any(_saved_pc.get(k) for k in ('http_proxy', 'https_proxy')):
        set_proxy_config(
            http_proxy=_saved_pc.get('http_proxy', ''),
            https_proxy=_saved_pc.get('https_proxy', ''),
        )
        _lifecycle_log.info('Loaded proxy config from server_config.json: http=%s https=%s',
                           _saved_pc.get('http_proxy', '') or '(env)',
                           _saved_pc.get('https_proxy', '') or '(env)')

    # Bypass domains (feeds both proxies_for() and no_proxy env automatically)
    _saved_proxy = _saved_cfg.get('proxy_bypass_domains', [])
    if _saved_proxy:
        set_bypass_domains(_saved_proxy)
        _lifecycle_log.info('Loaded %d proxy bypass domains from server_config.json', len(_saved_proxy))
except Exception as _e:
    _lifecycle_log.warning('Failed to load proxy config: %s', _e)


# ── Global error handlers ──
# API routes (/api/*) always get JSON; browser routes get HTML.

def _is_api_request():
    """Check if the current request targets an API endpoint."""
    return request.path.startswith('/api/')

@app.errorhandler(404)
def _handle_404(exc):
    # Suppress noisy Chrome DevTools probe
    if request.path.startswith('/.well-known/'):
        _lifecycle_log.debug('404 (well-known probe): %s', request.path)
    else:
        _lifecycle_log.warning('404 Not Found: %s %s', request.method, request.path)
    if _is_api_request():
        return jsonify({'ok': False, 'error': 'Not Found: %s' % request.path}), 404
    return make_response(
        '<h2>404 — Not Found</h2>'
        '<p>The requested URL was not found on this server.</p>',
        404
    )

@app.errorhandler(413)
def _handle_413(exc):
    _lifecycle_log.warning('413 Payload Too Large: %s %s', request.method, request.path)
    max_mb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
    if _is_api_request():
        return jsonify({'ok': False, 'error': 'Payload too large (max %.0f MB)' % max_mb}), 413
    return make_response(
        '<h2>413 — Payload Too Large</h2>'
        f'<p>The uploaded file exceeds the maximum allowed size ({max_mb:.0f} MB).</p>',
        413
    )

@app.errorhandler(405)
def _handle_405(exc):
    _lifecycle_log.warning('405 Method Not Allowed: %s %s (allowed: %s)',
                           request.method, request.path,
                           exc.valid_methods if hasattr(exc, 'valid_methods') else 'unknown')
    if _is_api_request():
        return jsonify({'ok': False, 'error': 'Method Not Allowed',
                        'allowed': list(exc.valid_methods) if hasattr(exc, 'valid_methods') and exc.valid_methods else []}), 405
    return make_response(
        '<h2>405 — Method Not Allowed</h2>'
        '<p>The method <code>%s</code> is not allowed for this URL.</p>' % request.method,
        405
    )

@app.errorhandler(415)
def _handle_415(exc):
    _lifecycle_log.warning('415 Unsupported Media Type: %s %s (Content-Type: %s)',
                           request.method, request.path, request.content_type)
    if _is_api_request():
        return jsonify({'ok': False, 'error': 'Unsupported Media Type — send Content-Type: application/json'}), 415
    return make_response(
        '<h2>415 — Unsupported Media Type</h2>'
        '<p>The server does not support the media type transmitted in the request.</p>',
        415
    )

@app.errorhandler(500)
def _handle_500(exc):
    _lifecycle_log.error('500 Internal Server Error: %s %s', request.method, request.path, exc_info=exc)
    if _is_api_request():
        return jsonify({'ok': False, 'error': 'Internal Server Error'}), 500
    return make_response(
        '<h2>500 — Internal Server Error</h2>'
        '<p>Something went wrong. Check server logs for details.</p>',
        500
    )


# ── Static file cache (avoid re-transfer over tunnel) ──
# JS/CSS: short cache + must-revalidate so bug fixes propagate fast
# Images/fonts: long cache (rarely change)

@app.after_request
def add_cache_headers(response):
    if request.path.startswith('/static/'):
        # ── MIME type enforcement (macOS / cross-platform safety net) ──
        # Even with mimetypes.init() + add_type(), some macOS Python builds
        # serve .js as text/plain.  Browsers with strict MIME checking silently
        # refuse to execute such scripts, causing "init failed" errors.
        if request.path.endswith('.js'):
            response.content_type = 'text/javascript; charset=utf-8'
        elif request.path.endswith('.css'):
            response.content_type = 'text/css; charset=utf-8'
        # ★ Vendor files (highlight.js, marked, katex, fonts) essentially never change
        # ★ Bundle files have content hash in filename → safe for long cache
        if '/vendor/' in request.path or '/bundle-' in request.path:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'  # 1 year
        elif request.path.endswith(('.js', '.css')):
            # ★ If ?v= cache-bust parameter is present, the file is version-pinned
            #   and safe to cache aggressively. This eliminates revalidation round-trips
            #   through VS Code port forwarding tunnels.
            if 'v=' in request.query_string.decode('ascii', errors='ignore'):
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'  # 7 days
            else:
                response.headers['Cache-Control'] = 'public, max-age=300, must-revalidate'  # 5min + revalidate
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'  # 24h for images
    return response


# ══════════════════════════════════════════
#  Database init & background workers
# ══════════════════════════════════════════

_server_log = logging.getLogger('server')

# ── JS bundle (concatenate 16 app scripts → 1 file for faster page load) ──
try:
    from lib.js_bundler import build_bundle
    build_bundle()
except Exception as _bundle_err:
    _server_log.warning('JS bundle build failed (will serve individual files): %s', _bundle_err)

with app.app_context():
    try:
        _server_log.info('Initialising database (PostgreSQL)...')
        init_db()
        warmup_db()
        _server_log.info('Database ready (PostgreSQL).')
    except Exception as exc:
        _server_log.critical('Database init/warmup failed — server will start but DB operations will fail: %s', exc, exc_info=True)
        # Don't raise — let the server start so the UI can load (settings page,
        # API config, etc.).  Individual endpoints will fail with clear errors
        # when they try to access the DB.

    # Seed built-in strategies (trading feature only)
    from lib import TRADING_ENABLED
    if TRADING_ENABLED:
        try:
            _server_log.info('Seeding trading strategies & migrating intel categories...')
            from routes.trading_intel import seed_builtin_strategies, migrate_intel_categories
            seed_builtin_strategies()
            migrate_intel_categories()
            _server_log.info('Trading seed complete.')
        except Exception as exc:
            _server_log.critical('Trading seed/migration failed — trading features will be degraded: %s', exc, exc_info=True)

# ══════════════════════════════════════════
#  Eager import validation — surface ImportErrors at startup
# ══════════════════════════════════════════
# The task pipeline uses lazy imports (lib.tasks_pkg.__init__) so missing
# packages like trafilatura, pymupdf, etc. won't crash until the first
# user request — by which time bootstrap.py's sys.excepthook can no
# longer intercept.  Force-import the critical request path here so any
# missing dependency crashes at startup where bootstrap can auto-repair.

_CRITICAL_IMPORTS = [
    'lib.tasks_pkg.orchestrator',   # run_task (the main chat path)
    'lib.tasks_pkg.executor',       # tool dispatch
    'lib.fetch',                    # web fetching (trafilatura, etc.)
    'lib.search',                   # web search
    'lib.llm_client',               # LLM API client
]

_server_log.info('Validating critical imports...')
_import_failures = []
for _mod_name in _CRITICAL_IMPORTS:
    try:
        __import__(_mod_name)
    except ImportError as _ie:
        _import_failures.append((_mod_name, _ie))
        _server_log.error('Critical import failed: %s — %s', _mod_name, _ie)

if _import_failures:
    # Build a clear error message and raise so bootstrap.py can catch it
    _fail_msgs = [f'  {m}: {e}' for m, e in _import_failures]
    _msg = (
        'Critical dependencies are missing — the server cannot handle requests:\n'
        + '\n'.join(_fail_msgs)
    )
    _server_log.critical(_msg)
    raise ImportError(_msg)

_server_log.info('All critical imports validated successfully.')


def _start_background_workers():
    """Launch all background threads (trading workers gated by TRADING_ENABLED)."""
    from lib import TRADING_ENABLED
    if not TRADING_ENABLED:
        _server_log.info('TRADING_ENABLED is off — skipping background workers')
        return

    try:
        # Intel crawl worker
        _server_log.info('Starting intel crawl worker...')
        from routes.trading_intel import start_intel_worker
        start_intel_worker(app)

        # Autopilot worker
        _server_log.info('Starting autopilot worker...')
        from routes.trading_autopilot import start_autopilot_worker
        start_autopilot_worker()
        _server_log.info('Background workers started.')
    except Exception as e:
        # Intentionally not re-raised: server should remain functional
        # even if optional background workers fail to start.
        _server_log.error('Failed to start background workers: %s', e, exc_info=True)


# ══════════════════════════════════════════
#  Main
# ══════════════════════════════════════════

def _find_free_web_port(start=15000, end=15100):
    """Find an available TCP port for the Flask server.

    Scans sequentially from *start*. Returns the first port where
    nothing is listening on localhost.

    Args:
        start: First port to try (default 15000).
        end:   Exclusive upper bound.

    Returns:
        An available port number, or *start* if all are somehow busy.
    """
    import socket
    for p in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('localhost', p))
            s.close()
            if result != 0:          # connection refused → port is free
                return p
        except Exception as e:
            _server_log.debug('[Server] Port %d probe error (assuming free): %s', p, e)
            return p                 # any error → assume free
    return start


if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    preferred_port = int(os.environ.get('PORT', 15000))
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'

    # ── Auto-detect free port if preferred is occupied ──
    port = _find_free_web_port(start=preferred_port)
    if port != preferred_port:
        logging.getLogger('server').info(
            'Port %d is in use — auto-selected port %d', preferred_port, port
        )

    # ── Fix SSE streaming through VSCode port-forward ──
    #    1) HTTP/1.1 enables Transfer-Encoding: chunked so each yield
    #       is framed as a discrete chunk the proxy can forward immediately.
    #    2) Werkzeug hard-codes "Connection: close" in run_wsgi() which
    #       tells HTTP-aware proxies (like VSCode tunnel) the response is
    #       a one-shot — some interpret this as "buffer until socket close".
    #       We monkey-patch send_header to suppress that single header so
    #       HTTP/1.1 default keep-alive semantics apply and proxies stream
    #       each chunk as it arrives.
    #    NOTE: Werkzeug's run_wsgi() hard-codes "Connection: close".
    #          This monkey-patch remains necessary until that changes.
    #          Re-test after any Werkzeug upgrade.
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = 'HTTP/1.1'
    _orig_send_header = WSGIRequestHandler.send_header
    def _patched_send_header(self, keyword, value):
        # Drop Werkzeug's forced "Connection: close" — it causes VSCode
        # port-forward (and similar HTTP-aware proxies) to buffer the
        # entire streamed response instead of forwarding chunks in real-time.
        if keyword.lower() == 'connection' and value.lower() == 'close':
            return
        _orig_send_header(self, keyword, value)
    WSGIRequestHandler.send_header = _patched_send_header
    logging.getLogger('server').info('Applied Werkzeug SSE streaming fix (HTTP/1.1 + suppress Connection:close)')

    _start_background_workers()

    # ── DolphinFS keepalive (prevents FUSE mount from going stale) ──
    try:
        from lib.fs_keepalive import start_fs_keepalive
        start_fs_keepalive()
    except Exception as e:
        _server_log.warning('Failed to start FS keepalive: %s', e, exc_info=True)

    # ── Cross-datacenter DolphinFS detection ──
    try:
        from lib.cross_dc import init_cross_dc_detection
        init_cross_dc_detection()
    except Exception as e:
        _server_log.warning('Failed to start cross-DC detection: %s', e, exc_info=True)

    # ── Feishu Bot (optional, needs FEISHU_APP_ID + FEISHU_APP_SECRET) ──
    feishu_ok = False
    try:
        from lib.feishu import start_bot as start_feishu_bot, ENABLED as FEISHU_ENABLED
        if FEISHU_ENABLED:
            feishu_ok = start_feishu_bot()
            if not feishu_ok:
                _server_log.warning('Feishu Bot start_bot() returned False — bot did not start')
        else:
            _server_log.info('Feishu Bot disabled (FEISHU_APP_ID/FEISHU_APP_SECRET not set)')
    except Exception as e:
        _server_log.warning('Feishu Bot failed to start: %s', e, exc_info=True)

    from lib import TRADING_ENABLED as _trading_on
    from lib.version import __version__ as _ver
    _banner_lines = [
        '=' * 52,
        f'  🫧 Tofu Server  v{_ver}',
        f'  http://{host}:{port}',
    ]
    if _trading_on:
        _banner_lines.append('  Trading Advisor:  /trading.html')
        _banner_lines.append('  📡  Intel Crawler: auto every 2h')
        _banner_lines.append('  🤖  Autopilot: background scheduler active')
    else:
        _banner_lines.append('  💰  Trading Advisor: OFF (set TRADING_ENABLED=1)')
    if feishu_ok:
        _banner_lines.append('  💬  Feishu Bot: ON (WebSocket long-connection)')
    else:
        _banner_lines.append('  💬  Feishu Bot: OFF (set FEISHU_APP_ID & FEISHU_APP_SECRET)')
    _banner_lines.append('  ⏰  Proactive Agent Scheduler: active')
    if TUNNEL_TOKEN:
        _banner_lines.append('  🔒  Tunnel Auth: ON')
        _banner_lines.append('  🔑  First visit: http://HOST:PORT/?token=<TOKEN>')
    else:
        _banner_lines.append('  🔓  Tunnel Auth: OFF (set TUNNEL_TOKEN to enable)')
    _banner_lines.append('=' * 52)
    _server_log.info('Server starting\n%s', '\n'.join(_banner_lines))

    app.run(host=host, port=port, debug=debug_mode, threaded=True)
