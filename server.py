#!/usr/bin/env python3
"""Tofu Server — Quart + Hypercorn (HTTP/2, ASGI).

App entry point. Uses:
  - Quart (async Flask from Pallets) as the application framework
  - Hypercorn as the ASGI server with HTTP/2 support
  - Auto-generated self-signed TLS for zero-config HTTP/2 in browsers

All existing Flask-style sync route handlers run unchanged in a thread pool.

Usage:
    python server.py                          # HTTPS + HTTP/2 (auto-cert)
    python server.py --no-tls                 # HTTP/1.1 only
    python server.py --certfile cert.pem --keyfile key.pem   # custom cert
"""

import asyncio
import os
import sys
import json
import logging
import time
import signal
import faulthandler

# ── Capture C-level fatal signals (SIGSEGV / SIGABRT / SIGFPE / SIGILL / SIGBUS) ──
# These fire on heap corruption (e.g. `munmap_chunk(): invalid pointer`) from
# native extensions like urllib3's response decompressor. Without this the
# abort prints to fd 2 only and we lose the Python stack of every thread.
# Writing to a dedicated file (instead of stderr) ensures the trace survives
# even when stderr is the controlling terminal of a process that's about
# to die. all_threads=True captures every Python thread, not just the
# crashing one — essential for diagnosing concurrent-fetch races.
#
# Dual-sink strategy: write to BOTH the FUSE-backed logs/ (durable across
# box restarts, but may be truncated by the very FUSE stall that caused the
# crash) AND a tmpfs mirror in /dev/shm (immune to FUSE stalls, but lost on
# box reboot). On crash, check /dev/shm first for the clean copy.
_fault_log = None
try:
    _FAULT_LOG_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'logs', 'faulthandler.log')
    os.makedirs(os.path.dirname(_FAULT_LOG_PATH), exist_ok=True)
    _fault_log = open(_FAULT_LOG_PATH, 'a', buffering=1)  # line-buffered
    _fault_log.write('\n=== faulthandler armed pid=%d at %s ===\n'
                     % (os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S')))
except OSError:
    pass

# Prefer tmpfs for the live faulthandler sink (survives FUSE stalls intact);
# fall back to the FUSE log, then stderr.
_fault_shm_log = None
try:
    _FAULT_SHM_PATH = '/dev/shm/tofu_faulthandler_%d.log' % os.getpid()
    _fault_shm_log = open(_FAULT_SHM_PATH, 'w', buffering=1)
    _fault_shm_log.write('=== faulthandler armed pid=%d at %s ===\n'
                         % (os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S')))
    faulthandler.enable(file=_fault_shm_log, all_threads=True)
except OSError:
    _fault_shm_log = None

if _fault_shm_log is None:
    # tmpfs unavailable — use the FUSE log (better than nothing)
    if _fault_log is not None:
        faulthandler.enable(file=_fault_log, all_threads=True)
    else:
        faulthandler.enable(all_threads=True)

# ── Pin mapped pages into RAM (FUSE SIGBUS mitigation) ──
# All .so files (C extensions, libpython, libc) are dlopen'd via mmap with
# demand-paged code segments. When those files live on a FUSE mount, a
# transient stall during a lazy page-in delivers SIGBUS (unrecoverable).
# MCL_CURRENT pins already-mapped pages; MCL_FUTURE pins every future mmap
# at load time, collapsing the dangerous demand-fault window to zero.
try:
    import ctypes as _ctypes
    _MCL_CURRENT, _MCL_FUTURE = 1, 2
    _libc = _ctypes.CDLL('libc.so.6', use_errno=True)
    if _libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) != 0:
        import errno as _errno
        _mlk_err = _ctypes.get_errno()
        # ENOMEM (12) = memlock rlimit too low — common in containers
        if _mlk_err == _errno.ENOMEM:
            os.write(2, b'[boot] mlockall skipped: memlock rlimit too low\n')
        else:
            os.write(2, (b'[boot] mlockall failed errno=%d\n' % _mlk_err))
    else:
        os.write(2, b'[boot] mlockall(MCL_CURRENT|MCL_FUTURE) OK '
                    b'\xe2\x80\x94 pages pinned\n')
except Exception as _mlk_exc:
    try:
        os.write(2, (b'[boot] mlockall unavailable: %s\n'
                     % str(_mlk_exc).encode(errors='replace')))
    except OSError:
        pass

# ── Record process start time (same as server.py) ──
_PROC_T0 = time.time()
try:
    os.write(2, b'\033[36m[boot +  0.0s]\033[0m \xf0\x9f\xab\xa7 Tofu '
                b'async bootstrap \xe2\x80\x94 importing core libraries\xe2\x80\xa6\n')
except OSError:
    pass

# ── Auto-activate conda env (reuse server.py logic) ──
# This must happen before any third-party imports.
_PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJ_DIR)

# ── Dev fallback: locate a local tofu-search checkout when it isn't
# pip-installed (production installs it via requirements.txt). Set
# TOFU_SEARCH_PATH to the repo root of a sibling tofu-search clone.
_TOFU_SEARCH_PATH = os.environ.get('TOFU_SEARCH_PATH', '')
if _TOFU_SEARCH_PATH and os.path.isdir(_TOFU_SEARCH_PATH):
    sys.path.insert(0, _TOFU_SEARCH_PATH)


def _tofu_maybe_reexec_into_env():
    """Re-exec into Tofu's conda env if not already there."""
    marker = os.path.join(_PROJ_DIR, '.tofu_env.json')
    if not os.path.isfile(marker):
        return
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return
    target_py = cfg.get('python') or ''
    env_prefix = cfg.get('env_prefix') or ''
    if not target_py or not os.access(target_py, os.X_OK):
        return
    try:
        same = os.path.realpath(target_py) == os.path.realpath(sys.executable)
    except OSError:
        same = (target_py == sys.executable)
    if same:
        return
    if os.environ.get('_TOFU_ENV_REEXEC') == '1':
        return
    if env_prefix and os.path.isdir(env_prefix):
        env_lib = os.path.join(env_prefix, 'lib')
        if os.path.isdir(env_lib):
            os.environ['LD_LIBRARY_PATH'] = (
                env_lib + os.pathsep + os.environ.get('LD_LIBRARY_PATH', ''))
        env_bin = os.path.join(env_prefix, 'bin')
        if os.path.isdir(env_bin):
            os.environ['PATH'] = env_bin + os.pathsep + os.environ.get('PATH', '')
        os.environ.setdefault('CONDA_PREFIX', env_prefix)
    os.environ['_TOFU_ENV_REEXEC'] = '1'
    try:
        os.execv(target_py, [target_py, *sys.argv])
    except OSError:
        os.environ.pop('_TOFU_ENV_REEXEC', None)


_tofu_maybe_reexec_into_env()

# ── .env loading ──
def _load_dotenv():
    env_path = os.path.join(_PROJ_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key not in os.environ:
                os.environ[key] = value

_load_dotenv()


# ═══════════════════════════════════════════════════════════════════════
#  Framework Compatibility Shim
# ═══════════════════════════════════════════════════════════════════════
# Quart is API-compatible with Flask but lives under `quart.*` imports.
# Our routes and lib/ code import from flask. We install a shim so that
# `from flask import *` resolves to Quart's equivalents at runtime.
# This is the official Quart migration approach.

def _install_flask_shim():
    """Make `from flask import X` resolve to Quart equivalents.

    Quart is a superset of Flask's API. This shim allows all existing
    route code to work without changing any import statements.

    Key difference: Quart makes send_from_directory, send_file, and
    make_response async. When sync route handlers (running in Quart's
    thread pool) call these, they get coroutine objects. We wrap them
    with sync-safe versions that detect this and await appropriately.
    """
    try:
        import quart
    except ImportError:
        sys.stderr.write(
            '\033[31m[server.py] ERROR: quart is not installed.\n'
            '  Install with: pip install quart hypercorn cryptography\033[0m\n')
        sys.exit(1)

    import asyncio
    import functools
    import inspect

    # Recover the GENUINE async helpers. If server.py is imported/exec'd
    # more than once in the same process (e.g. a test re-imports it via
    # importlib), ``quart.make_response`` etc. are already our sync-safe
    # wrappers from the first install. Capturing those as the "originals"
    # and wrapping them again would corrupt ``_orig_make_response_async``
    # (it would point at a sync-safe wrapper instead of the real async
    # ``quart.make_response``), so error handlers that
    # ``await _orig_make_response_async(...)`` would route through the
    # thread-bridge and deadlock. ``_sync_safe`` stashes the genuine async
    # function on ``.__wrapped__``; unwrap through it so a re-install
    # always starts from the real async helpers.
    def _genuine(fn):
        while getattr(fn, '_quart_async_wrapper', False):
            fn = getattr(fn, '__wrapped__', fn)
        return fn

    _orig_send_from_directory = _genuine(quart.send_from_directory)
    _orig_send_file = _genuine(quart.send_file)
    _orig_make_response = _genuine(quart.make_response)

    def _sync_safe(async_fn):
        """Wrap an async function to be callable from sync code in a thread."""
        @functools.wraps(async_fn)
        def wrapper(*args, **kwargs):
            coro = async_fn(*args, **kwargs)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # We're in a thread with an event loop running elsewhere.
                # Use the Quart-provided mechanism to run coroutines from
                # sync code within a request context.
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=30)
            else:
                return asyncio.run(coro)
        # Also make it awaitable for async callers
        wrapper._async = async_fn
        wrapper.__wrapped__ = async_fn
        # Mark it so Quart's ensure_async can detect the dual nature
        wrapper._quart_async_wrapper = True
        return wrapper

    # Replace in quart module so `from flask import send_from_directory`
    # gets the sync-safe version
    quart.send_from_directory = _sync_safe(_orig_send_from_directory)
    quart.send_file = _sync_safe(_orig_send_file)
    quart.make_response = _sync_safe(_orig_make_response)

    # Expose originals at module level for async code that needs to await directly
    global _orig_make_response_async
    _orig_make_response_async = _orig_make_response

    # ── Patch Request async methods/properties for sync route handlers ──
    # In Quart, get_json(), form, files, data, and json are async. Sync
    # route handlers (run in executor threads via run_sync) get coroutine
    # objects instead of values. Monkey-patch the Request class to run
    # the coroutine on the MAIN event loop — NOT a fresh child loop.
    #
    # The naive ``asyncio.run(coro)`` here is wrong: it spins up a new
    # loop in the worker thread, and the coroutine then awaits hypercorn's
    # request body Future, which lives on the main loop. Cross-loop
    # awaits never wake up — symptom: large POST bodies hang server-side
    # until the client times out, while small bodies (already inlined
    # into the ASGI scope before dispatch) work fine. Fix: schedule via
    # ``run_coroutine_threadsafe`` on the loop saved by
    # ``hub.set_loop`` at startup.
    from quart.wrappers import Request as _QuartRequest

    _orig_get_json = _QuartRequest.get_json

    def _run_coro_sync(coro):
        """Run a coroutine from a sync context (executor thread)."""
        if not inspect.iscoroutine(coro):
            return coro
        try:
            from lib.push import hub as _push_hub
            main_loop = getattr(_push_hub, '_loop', None)
        except Exception:
            main_loop = None
        if main_loop is not None and main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, main_loop)
            return future.result()
        return asyncio.run(coro)

    def _sync_safe_get_json(self, *args, **kwargs):
        return _run_coro_sync(_orig_get_json(self, *args, **kwargs))

    # Stash the genuine async original ON the wrapper so async handlers can
    # recover it regardless of how many times the shim is (re)installed or
    # which module object holds it (test harnesses sometimes exec server.py as
    # a second module). Always unwrap to the FIRST genuine coroutine fn.
    _genuine_get_json = getattr(_orig_get_json, '_genuine_async_get_json', _orig_get_json)
    _sync_safe_get_json._genuine_async_get_json = _genuine_get_json
    _QuartRequest.get_json = _sync_safe_get_json

    # Patch async properties: form, files, data, json
    _orig_form_prop = _QuartRequest.form
    _orig_files_prop = _QuartRequest.files
    _orig_data_prop = _QuartRequest.data

    def _make_sync_safe_property(orig_prop):
        _fget = orig_prop.fget
        @property
        def _prop(self):
            return _run_coro_sync(_fget(self))
        return _prop

    _QuartRequest.form = _make_sync_safe_property(_orig_form_prop)
    _QuartRequest.files = _make_sync_safe_property(_orig_files_prop)
    _QuartRequest.data = _make_sync_safe_property(_orig_data_prop)

    # json property delegates to the already-patched sync get_json
    @property
    def _json_prop(self):
        return self.get_json()
    _QuartRequest.json = _json_prop

    # Install the shim: make `import flask` resolve to quart
    sys.modules['flask'] = quart
    # Also shim sub-modules that code might import from
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        quart_sub = f'quart.{attr}'
        flask_sub = f'flask.{attr}'
        if quart_sub in sys.modules:
            sys.modules[flask_sub] = sys.modules[quart_sub]

    # Werkzeug exceptions are used directly in some places
    # Quart re-exports them, but ensure werkzeug is still importable
    import importlib.util
    if importlib.util.find_spec('werkzeug') is None:
        logging.getLogger(__name__).debug('werkzeug not importable; relying on quart re-exports')


_install_flask_shim()


# ── Now safe to import Quart (which the routes will see as 'flask') ──
import quart  # noqa: F401  — kept so quart.* monkeypatches in _install_flask_shim resolve
from quart import Quart, request

# ═══════════════════════════════════════════════════════════════════════
#  Logging (reuse server.py's architecture)
# ═══════════════════════════════════════════════════════════════════════

import mimetypes
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/json', '.json')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/ttf', '.ttf')
mimetypes.add_type('application/wasm', '.wasm')

BASE_DIR = _PROJ_DIR

# ── Logging setup (identical to server.py) ──
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

_LOG_FMT = '%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'
_formatter = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

_BIZ_PREFIXES = ('lib.', 'routes.', 'server')

class _BizOnly(logging.Filter):
    def filter(self, record):
        return record.name.startswith(_BIZ_PREFIXES)

class _VendorOnly(logging.Filter):
    def filter(self, record):
        return (not record.name.startswith(_BIZ_PREFIXES)
                and record.name != 'werkzeug'
                and record.name != 'hypercorn'
                and not record.name.startswith('hypercorn.'))

class _BizAndServerOnly(logging.Filter):
    def filter(self, record):
        return (record.name.startswith(_BIZ_PREFIXES)
                or record.name == 'hypercorn'
                or record.name.startswith('hypercorn.'))

class _AccessOnly(logging.Filter):
    def filter(self, record):
        return (record.name == 'hypercorn.access'
                or record.name == 'werkzeug')

class _QuietPollFilter(logging.Filter):
    _NOISY_PATHS = ('/api/chat/poll/', '/api/chat/stream/', '/api/browser/commands')
    def filter(self, record):
        msg = record.getMessage()
        if any(p in msg for p in self._NOISY_PATHS) and '200' in msg:
            return False
        return True

_app_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'app.log'),
    when='midnight', backupCount=30, encoding='utf-8')
_app_handler.setFormatter(_formatter)
_app_handler.setLevel(logging.INFO)
_app_handler.addFilter(_BizOnly())

_access_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'access.log'),
    when='midnight', backupCount=14, encoding='utf-8')
_access_handler.setFormatter(_formatter)
_access_handler.setLevel(logging.INFO)
_access_handler.addFilter(_AccessOnly())

_error_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'error.log'),
    maxBytes=5 * 1024 * 1024, backupCount=10, encoding='utf-8')
_error_handler.setFormatter(_formatter)
_error_handler.setLevel(logging.WARNING)
_error_handler.addFilter(_BizAndServerOnly())

_vendor_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'vendor.log'),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
_vendor_handler.setFormatter(_formatter)
_vendor_handler.setLevel(logging.WARNING)
_vendor_handler.addFilter(_VendorOnly())

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_formatter)
_console_handler.setLevel(logging.WARNING)
_console_handler.addFilter(_BizAndServerOnly())

logging.basicConfig(
    level=logging.INFO,
    handlers=[_app_handler, _access_handler, _error_handler,
              _vendor_handler, _console_handler],
)

_NOISY_LIBS = (
    'courlan', 'htmldate', 'justext',
    'urllib3', 'requests', 'charset_normalizer',
    'websockets', 'websockets.client',
    'PIL', 'pymupdf',
    'httpcore', 'httpx',
)
for _lib_name in _NOISY_LIBS:
    logging.getLogger(_lib_name).setLevel(logging.WARNING)
logging.getLogger('trafilatura').setLevel(logging.ERROR)
for _sub in ('trafilatura.xml', 'trafilatura.core', 'trafilatura.htmlprocessing',
             'trafilatura.metadata'):
    logging.getLogger(_sub).setLevel(logging.ERROR)
logging.getLogger('hypercorn.access').addFilter(_QuietPollFilter())


# ── Crash visibility: route uncaught exceptions to the log files ──
# faulthandler (top of file) covers C-level fatal signals, but an uncaught
# *Python* exception in the main thread otherwise reaches only the default
# excepthook → stderr, never app.log / error.log. Install a hook that logs
# it at CRITICAL (with traceback) before delegating to whatever hook was
# already installed (e.g. the bootstrap-delegation hook that re-execs to
# bootstrap.py on ImportError) — so we add visibility without clobbering it.
_prev_excepthook = sys.excepthook

def _crash_excepthook(exc_type, exc_value, exc_tb):
    # Ctrl-C is a normal shutdown path, not a crash — don't scream about it.
    if not issubclass(exc_type, KeyboardInterrupt):
        try:
            logging.getLogger('server').critical(
                'Uncaught exception — process is terminating',
                exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass  # logging must never mask the original crash
    (_prev_excepthook or sys.__excepthook__)(exc_type, exc_value, exc_tb)

sys.excepthook = _crash_excepthook


# ── Boot progress ──
_BOOT_T0 = _PROC_T0
_boot_logger = logging.getLogger('server.boot')

def _boot(msg, *args):
    try:
        line = msg % args if args else msg
    except Exception:
        line = msg
    elapsed = time.time() - _BOOT_T0
    sys.stderr.write('\033[36m[boot +%5.1fs]\033[0m %s\n' % (elapsed, line))
    sys.stderr.flush()
    _boot_logger.info('[boot +%.1fs] %s', elapsed, line)


_boot('🫧 Tofu (async) starting up — loading core modules…')

from lib.database import close_db, init_db, warmup_db


# ═══════════════════════════════════════════════════════════════════════
#  Quart App
# ═══════════════════════════════════════════════════════════════════════

# Flask 3.1+ / newer Quart dropped PROVIDE_AUTOMATIC_OPTIONS from default
# config, but add_url_rule (called during __init__ for the static route)
# still reads it → KeyError.  Inject it into the class defaults before
# instantiation so it's present from the very first add_url_rule call.
_orig_default_config = Quart.default_config
if 'PROVIDE_AUTOMATIC_OPTIONS' not in _orig_default_config:
    Quart.default_config = {**_orig_default_config, 'PROVIDE_AUTOMATIC_OPTIONS': True}

app = Quart(__name__,
            static_folder=os.path.join(BASE_DIR, 'static'),
            static_url_path='/static')


# ── Flask secret key (reuse server.py logic) ──
def _load_or_create_flask_secret_key():
    from lib.config_dir import config_path as _cfg_path
    _env_key = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if _env_key:
        return _env_key
    _key_file = _cfg_path('flask_secret_key')
    try:
        if os.path.isfile(_key_file):
            with open(_key_file, 'r', encoding='utf-8') as _kf:
                _existing = _kf.read().strip()
            if _existing:
                return _existing
    except Exception:
        pass
    _new_key = os.urandom(32).hex()
    try:
        os.makedirs(os.path.dirname(_key_file), exist_ok=True)
        _flag = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            _fd = os.open(_key_file, _flag, 0o600)
            try:
                os.write(_fd, _new_key.encode('utf-8'))
            finally:
                os.close(_fd)
        except (AttributeError, OSError):
            with open(_key_file, 'w', encoding='utf-8') as _kf:
                _kf.write(_new_key)
    except Exception as e:
        logging.getLogger('server').warning('[FlaskSecret] Failed to persist: %s', e)
    return _new_key


app.secret_key = _load_or_create_flask_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
# ── Disable response/body timeouts for long-lived SSE streams ──
# Quart's defaults (60s) silently kill /api/chat/stream connections during
# long LLM responses, causing the UI to "stop updating without refresh".
# SSE clients keep their own keepalive (15s comment ping in chat_stream),
# so we set these to None to defer entirely to the SSE layer.
app.config['RESPONSE_TIMEOUT'] = None
app.config['BODY_TIMEOUT'] = None


# ── Compression (Quart-native, no flask-compress dependency) ──
# Quart does not use flask-compress. Instead, we add a simple
# after_request hook for gzip. For heavy production use, Hypercorn
# + a reverse proxy handle this better.
_COMPRESS_MIMETYPES = frozenset([
    'text/html', 'text/css', 'text/javascript',
    'application/javascript', 'application/json',
])
_COMPRESS_MIN_SIZE = 256

import gzip as _gzip

@app.after_request
async def _compress_response(response):
    """Simple gzip compression for eligible responses."""
    # Skip SSE (buffering breaks streaming), small responses, already encoded
    if (response.content_type
            and 'text/event-stream' in response.content_type):
        return response
    if response.content_encoding:
        return response
    accept_enc = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept_enc:
        return response
    mime = (response.content_type or '').split(';')[0].strip()
    if mime not in _COMPRESS_MIMETYPES:
        return response
    data = await response.get_data()
    if len(data) < _COMPRESS_MIN_SIZE:
        return response
    # gzip is CPU-bound; running it inline would block the event loop (and
    # every other connection / SSE keepalive) for the duration. Offload to
    # the sync executor so a multi-MB body doesn't stall the whole server.
    loop = asyncio.get_running_loop()
    compressed = await loop.run_in_executor(None, _gzip.compress, data, 6)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = len(compressed)
    response.headers.pop('Vary', None)
    response.headers['Vary'] = 'Accept-Encoding'
    return response


# ── Auth (legacy compat constants only) ──
# The active auth middleware lives in routes/api_v1/auth.py and is
# registered after blueprints are wired in. ``TUNNEL_TOKEN`` is kept
# only as a deprecated back-compat shim; new deployments mint API
# keys instead (see lib.api_keys.bootstrap_personal_key).
TUNNEL_TOKEN = os.environ.get('TUNNEL_TOKEN', '')
TUNNEL_COOKIE = '_tunnel_auth'
TUNNEL_COOKIE_MAX_AGE = 86400 * 30
if TUNNEL_TOKEN:
    logging.getLogger('server.auth').warning(
        '[Auth] TUNNEL_TOKEN is deprecated. Migrate to API keys '
        '(POST /api/v1/keys with admin scope). The shim remains '
        'for now but new code paths target the unified auth gate.')


# ── Method Override + CloudIDE JSON fix ──
@app.before_request
async def method_override():
    override = request.args.get('_method')
    if override:
        request.scope['method'] = override.upper()
    # CloudIDE sometimes double-encodes JSON bodies (sends a JSON string
    # whose value is itself a JSON object). Detect and unwrap in-place so
    # downstream ``request.get_json()`` returns the correct dict.
    ct = request.content_type or ''
    if request.method in ('POST', 'PUT') and 'json' in ct:
        raw = (await request.get_data()).decode('utf-8', errors='replace')
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, str):
                    corrected = json.dumps(json.loads(data)).encode('utf-8')
                    request._body = corrected
            except (json.JSONDecodeError, TypeError):
                pass


# ── Request lifecycle logging ──
from lib.log import get_logger, set_req_id, req_id as _get_req_id
import uuid as _uuid

_lifecycle_log = get_logger('server.lifecycle')
_QUIET_PREFIXES = ('/api/browser/', '/api/desktop/', '/static/', '/api/task/')
_SLOW_THRESHOLD_S = 2.0

@app.before_request
async def _assign_req_id_and_log():
    rid = request.headers.get('X-Request-ID') or _uuid.uuid4().hex[:12]
    set_req_id(rid)
    request._start_time = time.time()
    path = request.path
    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)
    level = logging.DEBUG if is_quiet else logging.INFO
    _lifecycle_log.log(level, '[%s] → %s %s', rid, request.method, path)


@app.after_request
async def _log_response(response):
    elapsed = time.time() - getattr(request, '_start_time', time.time())
    rid = _get_req_id()
    path = request.full_path.rstrip('?')
    status = response.status_code
    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)

    if status >= 500:
        _lifecycle_log.error('[%s] ← %s %s %d (%.3fs)', rid, request.method, path, status, elapsed)
    elif status >= 400:
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
    return response


@app.teardown_request
async def _clear_req_id(exc):
    if exc:
        rid = _get_req_id()
        # Client disconnect mid-request (CancelledError during body read) is
        # benign — log at debug. Real handler exceptions are already logged
        # by _handle_uncaught with full context, so reaching teardown with
        # any other exception means the framework swallowed it; warn so it's
        # still visible without the alarming ERROR + traceback.
        if isinstance(exc, asyncio.CancelledError):
            _lifecycle_log.debug('[%s] Request teardown: client disconnected', rid)
        else:
            _lifecycle_log.warning('[%s] Request teardown with exception: %s', rid, exc)
    set_req_id(None)


# ── DB teardown ──
app.teardown_appcontext(close_db)


# ── Install the tofu-search bridge (LLM + browser + auth seams) ──
# Must run before any search/fetch call; idempotent, re-synced on config reload.
from lib.search_bridge import install_search_bridge
install_search_bridge()

# ── Register all Blueprints ──
from routes import register_all
register_all(app)


# ── Unified auth gate (single middleware) ──
# Replaces the legacy dual scheme (tunnel_auth + bearer_auth). One
# before_request hook resolves an AuthContext from any of:
#   - Authorization: Bearer / x-api-key header
#   - tofu_session cookie  (set on first browser visit via ?token=…)
#   - X-Tunnel-Token / TUNNEL_TOKEN  (deprecated back-compat shim)
# Public routes (static, /, /api/health, /api/v1/capabilities, etc.)
# bypass the gate — see _PUBLIC_EXACT in routes/api_v1/auth.py.
from routes.api_v1.auth import attach_rate_headers, auth_before_request
app.before_request(auth_before_request)
app.after_request(attach_rate_headers)


# ── First-boot personal key bootstrap ──
# Only relevant when the auth gate is in ``private`` or ``multi-user``
# mode. In ``open`` mode (the default for personal installs) no
# credential is required and minting a key would just confuse the
# operator. When in private/multi-user mode and the key store is
# empty AND no TUNNEL_TOKEN is configured, mint a personal admin key
# so the local UI and SDK "just work". The plaintext is printed once
# to stderr and persisted (0600) at data/config/.first_run_token.
# Disable with TOFU_AUTO_KEY=0.
_BOOTSTRAP_TOKEN = ''
try:
    from lib.auth_mode import get_mode as _get_auth_mode
    _AUTH_MODE = _get_auth_mode()
except Exception as _e:
    logging.getLogger('server.boot').warning(
        '[AuthMode] could not resolve mode: %s', _e)
    _AUTH_MODE = 'open'


def _bootstrap_personal_key_if_needed():
    global _BOOTSTRAP_TOKEN
    if (os.environ.get('TOFU_AUTO_KEY', '1') or '1').strip() == '0':
        return
    if _AUTH_MODE == 'open':
        return  # gate is open — no credential needed at all
    if TUNNEL_TOKEN:
        return  # legacy mode — user explicitly chose a shared secret
    try:
        from lib.api_keys import bootstrap_personal_key, has_any_key
    except Exception as _e:
        logging.getLogger('server.boot').warning(
            '[Auth] could not import bootstrap helpers: %s', _e)
        return
    if has_any_key():
        return
    plaintext = bootstrap_personal_key(name='personal')
    if plaintext:
        _BOOTSTRAP_TOKEN = plaintext


_bootstrap_personal_key_if_needed()


# ── Billing janitor: release stale credit reservations ──
# Spawns one daemon thread that sweeps the ledger every 5 minutes.
# A no-op if multi-user mode never gets used (the sweep just finds 0
# rows). Disabled with TOFU_BILLING_JANITOR=0.
try:
    from lib.billing.janitor import start_janitor as _start_billing_janitor
    _start_billing_janitor()
except Exception as _e:
    logging.getLogger('server.boot').warning(
        '[Billing] janitor failed to start: %s', _e)


# ── Static file cache headers ──
@app.after_request
async def add_cache_headers(response):
    if request.path.startswith('/static/'):
        if request.path.endswith('.js'):
            response.content_type = 'text/javascript; charset=utf-8'
        elif request.path.endswith('.css'):
            response.content_type = 'text/css; charset=utf-8'
        if '/vendor/' in request.path or '/bundle-' in request.path:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif request.path.endswith(('.js', '.css')):
            if 'v=' in request.query_string.decode('ascii', errors='ignore'):
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=300, must-revalidate'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


# ── Proxy config (reuse server.py logic) ──
try:
    from routes.config import _read_server_config
    from lib.proxy import set_bypass_domains, set_proxy_config
    _saved_cfg = _read_server_config()
    _saved_pc = _saved_cfg.get('proxy_config', {})
    if _saved_pc and any(_saved_pc.get(k) for k in ('http_proxy', 'https_proxy')):
        set_proxy_config(
            http_proxy=_saved_pc.get('http_proxy', ''),
            https_proxy=_saved_pc.get('https_proxy', ''),
        )
    _saved_proxy = _saved_cfg.get('proxy_bypass_domains', [])
    if _saved_proxy:
        set_bypass_domains(_saved_proxy)
except Exception as _e:
    _lifecycle_log.warning('Failed to load proxy config: %s', _e)


# ── Global error handlers ──
from lib.api_response import (
    api_internal_error,
    api_method_not_allowed,
    api_not_found,
    api_payload_too_large,
)


def _is_api_request():
    return request.path.startswith('/api/')


def _ws_safe_method_path():
    """Return (method, path) tolerating contexts where request is unavailable."""
    try:
        return request.method, request.path
    except RuntimeError:
        from quart import websocket as _ws
        try:
            return 'WS', _ws.path
        except RuntimeError:
            return '?', '?'


@app.errorhandler(404)
async def _handle_404(exc):
    if request.path.startswith('/.well-known/'):
        _lifecycle_log.debug('404 (well-known probe): %s', request.path)
    else:
        _lifecycle_log.warning('404 Not Found: %s %s', request.method, request.path)
    if _is_api_request():
        return api_not_found('Not Found: %s' % request.path)
    return await _orig_make_response_async(
        '<h2>404 — Not Found</h2><p>The requested URL was not found.</p>', 404)


@app.errorhandler(413)
async def _handle_413(exc):
    if _is_api_request():
        return api_payload_too_large(app.config['MAX_CONTENT_LENGTH'])
    return await _orig_make_response_async('<h2>413 — Payload Too Large</h2>', 413)


@app.errorhandler(405)
async def _handle_405(exc):
    if _is_api_request():
        return api_method_not_allowed()
    return await _orig_make_response_async('<h2>405 — Method Not Allowed</h2>', 405)


@app.errorhandler(500)
async def _handle_500(exc):
    rid = _get_req_id() or '-'
    method, path = _ws_safe_method_path()
    _lifecycle_log.error('500 ISE: [%s] %s %s', rid, method, path, exc_info=exc)
    if path.startswith('/api/'):
        return api_internal_error(exc, log_traceback=False)
    return await _orig_make_response_async(
        f'<h2>500</h2><p>Request ID: <code>{rid}</code></p>', 500)


@app.errorhandler(Exception)
async def _handle_uncaught(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    rid = _get_req_id() or '-'
    method, path = _ws_safe_method_path()
    _lifecycle_log.error('[%s] Uncaught: %s %s: %s', rid, method, path, exc, exc_info=True)
    if path.startswith('/api/'):
        return api_internal_error(exc, log_traceback=False)
    return await _orig_make_response_async(
        f'<h2>500</h2><p>Request ID: <code>{rid}</code></p>', 500)


# ═══════════════════════════════════════════════════════════════════════
#  Startup & Main
# ═══════════════════════════════════════════════════════════════════════

_server_log = logging.getLogger('server')

# ── JS bundle ──
try:
    from lib.js_bundler import build_bundle
    build_bundle()
except Exception as _bundle_err:
    _server_log.warning('JS bundle build failed: %s', _bundle_err)


def _init_database():
    """Initialize database (runs in app context)."""
    _boot('Initialising database…')
    init_db()
    warmup_db()
    try:
        from lib.database import heal_toast_corruption
        heal_toast_corruption()
    except Exception as e:
        _server_log.warning('TOAST auto-heal failed: %s', e)
    _boot('Database ready.')
    try:
        from lib.tasks_pkg import recover_stale_tasks_on_startup
        recover_stale_tasks_on_startup()
    except Exception as e:
        _server_log.warning('Stale task recovery failed: %s', e)

    # Resume swarm sub-agents that were mid-flight when the server stopped.
    # DB-backed round-level resume (see lib/swarm/persistence.py): rehydrates
    # each conversation-scoped session and re-spawns its unfinished agents
    # from their checkpointed message history.
    try:
        from lib.swarm.integration import rehydrate_swarms_on_startup
        rehydrate_swarms_on_startup()
    except Exception as e:
        _server_log.warning('Swarm rehydration failed: %s', e)


def _validate_imports():
    """Validate critical imports at startup."""
    _CRITICAL_IMPORTS = [
        'lib.tasks_pkg.orchestrator',
        'lib.tasks_pkg.executor',
        'tofu_search.fetch',
        'tofu_search.search',
        'lib.search_bridge',
        'lib.llm',
    ]
    _boot('Validating critical imports…')
    failures = []
    for mod_name in _CRITICAL_IMPORTS:
        _boot('  • importing %s', mod_name)
        try:
            __import__(mod_name)
        except ImportError as ie:
            failures.append((mod_name, ie))
            _server_log.error('Critical import failed: %s — %s', mod_name, ie)
    if failures:
        msgs = [f'  {m}: {e}' for m, e in failures]
        raise ImportError('Missing dependencies:\n' + '\n'.join(msgs))
    _boot('All critical imports validated.')

    # ── Eager-load heavy C extensions so mlockall pins their pages ──
    # These are the .so modules seen in past SIGBUS faulthandler dumps.
    # Loading them now (under mlockall MCL_FUTURE) ensures their code
    # pages are resident before any request arrives — the demand-fault
    # window that causes Bus errors on FUSE is eliminated.
    _NATIVE_PRELOADS = [
        'PIL._imaging',
        'lxml.etree',
        'greenlet._greenlet',
        'yaml._yaml',
        'numpy.core._multiarray_umath',
        'markupsafe._speedups',
        'charset_normalizer.md',
    ]
    # These are optional — may not be installed in all environments
    _NATIVE_PRELOADS_OPTIONAL = [
        'pymupdf._extra',
        'psycopg2._psycopg',
    ]
    _boot('Eager-loading native extensions (FUSE SIGBUS mitigation)…')
    for _mod in _NATIVE_PRELOADS:
        try:
            __import__(_mod)
        except ImportError as _ie:
            _server_log.warning('Native preload failed (required): %s — %s', _mod, _ie)
    for _mod in _NATIVE_PRELOADS_OPTIONAL:
        try:
            __import__(_mod)
        except ImportError:
            pass  # optional — not all deployments have these
    _boot('Native extensions preloaded.')


def _start_background_workers():
    """Launch optional background threads.

    Feature background workers (e.g. the trading intel + autopilot threads)
    now start via the ``tofu.startup`` entry-point group, run from
    ``routes.register_all`` after blueprints are mounted. Core no longer
    imports any optional feature here.
    """
    return


def _detect_reverse_proxy():
    """Detect whether we're running behind an HTTPS-terminating reverse proxy.

    Cloud IDEs / notebook platforms (VS Code port forwarding, GitHub
    Codespaces, Gitpod, JupyterHub-fronted environments like Meituan
    Codelab) expose the server on a public ``https://`` URL while talking
    to our backend over plain HTTP. If we also enable TLS on our side the
    proxy's plain-HTTP request hits our TLS listener and the connection is
    reset — the browser/proxy reports ``socket hang up``.

    Detection is signal-based, not host-name based, so it survives exports
    to any host: we look for env vars these platforms inject into the
    launch environment. Returns ``(behind_proxy: bool, proxy_name: str)``;
    ``proxy_name`` is ``''`` when nothing is detected.
    """
    # Ordered most-specific → most-generic so the friendliest name wins.
    if os.environ.get('VSCODE_PROXY_URI'):
        return True, 'VS Code'
    if os.environ.get('CODESPACES'):
        return True, 'Codespaces'
    if os.environ.get('GITPOD_WORKSPACE_URL'):
        return True, 'Gitpod'
    # JupyterHub-fronted platforms (Meituan Codelab, Binder, generic
    # JupyterHub) always terminate HTTPS at the hub and proxy plain HTTP
    # to single-user servers. JUPYTERHUB_* is set by the hub spawner;
    # JUPYTER_SERVER_URL / JPY_* cover bare notebook/lab proxying.
    if (os.environ.get('JUPYTERHUB_USER')
            or os.environ.get('JUPYTERHUB_SERVICE_PREFIX')
            or os.environ.get('JUPYTERHUB_API_URL')):
        return True, 'JupyterHub'
    # Codelab-specific belt-and-suspenders: the hub injects CODELAB_API_URL
    # even in shells where JUPYTERHUB_* was not exported.
    if os.environ.get('CODELAB_API_URL'):
        return True, 'Codelab'
    return False, ''


def _find_free_port(start=15000, end=15100):
    import socket
    for p in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('localhost', p))
            s.close()
            if result != 0:
                return p
        except Exception:
            return p
    return start


def _wait_port_free(host, port, timeout=10.0):
    """Block until ``host:port`` can be bound, or ``timeout`` seconds elapse.

    Uses a real ``bind()`` probe rather than ``connect_ex`` so the server's
    OWN lingering listener — which is briefly still present right after an
    in-place re-exec restart — is correctly WAITED OUT instead of being
    mistaken for a foreign process (the connect-probe would see it as "in
    use" and silently shift the port). Returns True once the port is bindable.

    Args:
        host: Bind host (``0.0.0.0`` / ``::`` normalized to all-interfaces).
        port: Port to wait for.
        timeout: Max seconds to wait before giving up.

    Returns:
        True if the port became bindable within ``timeout``, else False.
    """
    import socket
    import time as _t
    bind_host = '' if host in ('', '0.0.0.0', '::') else host
    deadline = _t.time() + timeout
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, port))
            return True
        except OSError as e:
            if _t.time() >= deadline:
                _server_log.debug('[Port] %s:%d still busy after %.1fs wait: %s',
                                  bind_host or '*', port, timeout, e)
                return False
            _t.sleep(0.25)
        finally:
            s.close()


def _ensure_tls_certs(certfile='', keyfile=''):
    """Ensure TLS certificates exist for HTTP/2 support.

    Browsers only negotiate HTTP/2 over TLS (ALPN). Without certs the
    server falls back to HTTP/1.1 and we lose the multiplexing benefit.

    Strategy:
      1. If user provides --certfile/--keyfile, use those.
      2. If certs already exist at data/certs/tofu.{pem,key}, reuse them.
      3. Otherwise, auto-generate via the `cryptography` library (pure Python).

    Disable with TOFU_TLS=0 or --no-tls.

    Returns:
        (certfile_path, keyfile_path) or (None, None) if TLS unavailable.
    """
    _tls_log = logging.getLogger('server.tls')

    if certfile and keyfile:
        if os.path.isfile(certfile) and os.path.isfile(keyfile):
            _tls_log.info('[TLS] Using provided certs: %s, %s', certfile, keyfile)
            return certfile, keyfile
        _tls_log.warning('[TLS] Provided cert/key files not found: %s, %s', certfile, keyfile)

    cert_dir = os.path.join(BASE_DIR, 'data', 'certs')
    cert_path = os.path.join(cert_dir, 'tofu.pem')
    key_path = os.path.join(cert_dir, 'tofu.key')

    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        _tls_log.info('[TLS] Reusing existing self-signed certs at %s', cert_dir)
        return cert_path, key_path

    _boot('Generating self-signed TLS certificate for HTTP/2…')
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        import ipaddress
        import socket

        os.makedirs(cert_dir, exist_ok=True)
        hostname = socket.gethostname()

        # Generate RSA 2048-bit key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Build X.509 certificate — valid 10 years, SAN for local access
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f'Tofu Server ({hostname})'),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName('localhost'),
                    x509.DNSName(hostname),
                    x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
                    x509.IPAddress(ipaddress.IPv4Address('0.0.0.0')),
                ]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        # Write key (mode 0600)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(_fd, key_pem)
        finally:
            os.close(_fd)

        # Write cert
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        with open(cert_path, 'wb') as f:
            f.write(cert_pem)

        _tls_log.info('[TLS] Generated self-signed cert at %s (valid 10 years)', cert_dir)
        _boot('TLS certificate ready (self-signed, valid 10 years).')
        return cert_path, key_path
    except ImportError:
        _tls_log.warning('[TLS] cryptography library not installed — '
                         'falling back to HTTP/1.1. Install: pip install cryptography')
        return None, None
    except Exception as e:
        _tls_log.warning('[TLS] Certificate generation failed: %s — falling back to HTTP/1.1', e)
        return None, None


if __name__ == '__main__':
    try:
        from hypercorn.config import Config as HypercornConfig
        from hypercorn.asyncio import serve as hypercorn_serve
    except ImportError:
        sys.stderr.write(
            '\033[31m[server.py] ERROR: hypercorn is not installed.\n'
            '  Install with: pip install hypercorn\033[0m\n')
        sys.exit(1)

    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description='Tofu Async Server')
    # Default to loopback. Networked exposure is an explicit choice via
    # --host 0.0.0.0 / BIND_HOST=0.0.0.0. Personal use stays effortless;
    # accidental LAN exposure stops being the default.
    parser.add_argument('--host', default=os.environ.get('BIND_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 15000)))
    parser.add_argument('--certfile', default=os.environ.get('TLS_CERTFILE', ''))
    parser.add_argument('--keyfile', default=os.environ.get('TLS_KEYFILE', ''))
    parser.add_argument('--no-tls', action='store_true',
                        help='Disable TLS (HTTP/1.1 only, no HTTP/2 in browsers)')
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()

    host = args.host

    # ── Instance lock — prevent multiple servers on the same project dir ──
    import socket as _sock_mod
    _lock_dir = os.path.join(BASE_DIR, 'data')
    os.makedirs(_lock_dir, exist_ok=True)
    _lock_path = os.path.join(_lock_dir, '.server.lock')
    _instance_lock_fd = None

    def _acquire_instance_lock():
        global _instance_lock_fd
        try:
            if not os.path.exists(_lock_path):
                open(_lock_path, 'a').close()
            _instance_lock_fd = open(_lock_path, 'r+')
        except Exception:
            return True
        try:
            import fcntl
            fcntl.flock(_instance_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            _instance_lock_fd.close()
            _instance_lock_fd = None
            return False
        try:
            _instance_lock_fd.seek(0)
            _instance_lock_fd.truncate()
            _instance_lock_fd.write(f'{os.getpid()}@{_sock_mod.gethostname()}\n')
            _instance_lock_fd.flush()
        except Exception:
            pass
        return True

    if not _acquire_instance_lock():
        _skip = (os.environ.get('TOFU_SKIP_LOCK', '') or '').strip()
        if _skip != '1':
            _server_log.critical('Another server instance is already running from this project directory.\n'
                                 '  Set TOFU_SKIP_LOCK=1 to force start.')
            sys.exit(1)
        _server_log.warning('[Lock] TOFU_SKIP_LOCK=1 — bypassing instance lock')

    _boot('Instance lock acquired (PID=%d)', os.getpid())

    # ── SIGTERM → graceful shutdown ──
    # Set a shutdown flag instead of calling sys.exit(0) from the signal
    # handler. sys.exit raises SystemExit in the main thread, which aborts
    # Hypercorn mid-serve and skips connection draining (graceful_timeout).
    # The flag is consumed by an asyncio.Event created inside the serving
    # loop (see _serve) and handed to hypercorn_serve(shutdown_trigger=…)
    # so in-flight requests / SSE streams drain cleanly.
    import threading as _threading
    _shutdown_requested = _threading.Event()
    from lib.compat import safe_signal
    def _signal_shutdown(signum, frame):
        _server_log.info('[Server] Received signal %s — shutting down…', signum)
        _shutdown_requested.set()
    # Passing a custom shutdown_trigger to hypercorn_serve suppresses
    # Hypercorn's own signal handlers, so we own BOTH signals here and
    # funnel them into the same graceful-drain flag.
    safe_signal(signal.SIGTERM, _signal_shutdown)
    safe_signal(signal.SIGINT, _signal_shutdown)

    # ── PG shutdown hook ──
    try:
        import atexit as _atexit
        from lib.database._core import stop_local_pg_if_owned
        _atexit.register(stop_local_pg_if_owned)
    except Exception as _e:
        _server_log.warning('[Server] PG shutdown hook failed: %s', _e)

    # On an in-place restart (re-exec), the previous image's listener may
    # still be draining on the original port for a fraction of a second.
    # _deferred_reexec stamps the port it was serving into _TOFU_REEXEC_PORT;
    # honor it by WAITING for that exact port to free up rather than letting
    # the connect-probe mistake our own lingering socket for a foreign one
    # and shift to the next port (15000 → 15001 → …).
    _reexec_port_env = (os.environ.get('_TOFU_REEXEC_PORT', '') or '').strip()
    os.environ.pop('_TOFU_REEXEC_PORT', None)
    if _reexec_port_env:
        try:
            port = int(_reexec_port_env)
        except (ValueError, TypeError):
            port = args.port
        if _wait_port_free(host, port):
            _server_log.info('[Restart] Reclaimed original port %d', port)
        else:
            _server_log.warning('[Restart] Port %d still busy after wait — '
                                 'falling back to probe', port)
            port = _find_free_port(start=port)
            if port != args.port:
                _server_log.info('Port %d in use — using %d', args.port, port)
    else:
        port = _find_free_port(start=args.port)
        if port != args.port:
            _server_log.info('Port %d in use — using %d', args.port, port)

    # Record the port we actually bound so an in-place restart (re-exec)
    # can reclaim it instead of re-probing. Read by _deferred_reexec in
    # routes/api_v1/update.py.
    os.environ['_TOFU_RUNTIME_PORT'] = str(port)

    # ── TLS / HTTP/2 setup ──
    from lib.env_compat import getenv_compat
    _force_tls = (getenv_compat('TOFU_TLS') or '').strip() == '1'
    _force_no_tls = (args.no_tls
                     or (getenv_compat('TOFU_TLS') or '').strip() == '0')
    # Auto-detect cloud-IDE / notebook reverse-proxy environments.
    # These proxies provide their own HTTPS+HTTP/2 on the public URL and
    # connect to our backend over plain HTTP. Adding TLS on our side causes
    # "socket hang up" because the proxy doesn't expect a TLS handshake.
    _behind_proxy, _proxy_name = _detect_reverse_proxy()
    _vscode_proxy = os.environ.get('VSCODE_PROXY_URI', '')

    if _force_no_tls:
        _tls_cert, _tls_key = None, None
        _boot('TLS disabled (--no-tls or TOFU_TLS=0).')
    elif _behind_proxy and not _force_tls:
        _tls_cert, _tls_key = None, None
        _boot('TLS auto-disabled — %s proxy detected (provides its own HTTPS). '
              'Force with TOFU_TLS=1.', _proxy_name or 'cloud IDE')
    else:
        _tls_cert, _tls_key = _ensure_tls_certs(args.certfile, args.keyfile)

    # ── Init DB + validate imports in app context ──
    async def _startup():
        async with app.app_context():
            _init_database()
            _validate_imports()
            _start_background_workers()

            # ── MCP auto-connect ──
            _boot('Configuring MCP auto-connect…')
            mcp_config = {}
            try:
                from lib.mcp.client import get_bridge
                from lib.mcp.config import load_mcp_config
                mcp_config = load_mcp_config()
                if mcp_config:
                    enabled = sum(1 for c in mcp_config.values() if c.get('enabled', True))
                    if enabled > 0:
                        import threading
                        def _mcp_auto():
                            try:
                                bridge = get_bridge()
                                result = bridge.connect_all()
                                total = sum(len(v) for v in result.values())
                                _server_log.info('[MCP] Auto-connect: %d servers, %d tools', len(result), total)
                            except Exception as e:
                                _server_log.error('[MCP] Auto-connect failed: %s', e, exc_info=True)
                        threading.Thread(target=_mcp_auto, name='mcp-auto-connect', daemon=True).start()
            except Exception as e:
                _server_log.warning('[MCP] Auto-connect setup failed: %s', e)

            # ── Local health checker ──
            try:
                from lib.llm_dispatch.health_local import start_local_health_checker
                start_local_health_checker()
            except Exception as e:
                _server_log.warning('[HealthLocal] Failed: %s', e)

            # ── FS keepalive ──
            try:
                from lib.fs_keepalive import start_fs_keepalive
                start_fs_keepalive()
            except Exception as e:
                _server_log.warning('FS keepalive failed: %s', e)

            # ── Cross-DC detection ──
            try:
                from lib.cross_dc import init_cross_dc_detection
                init_cross_dc_detection()
            except Exception as e:
                _server_log.warning('Cross-DC detection failed: %s', e)

            # ── Feishu Bot ──
            feishu_ok = False
            try:
                from lib.feishu import start_bot as start_feishu_bot, ENABLED as FEISHU_ENABLED
                if FEISHU_ENABLED:
                    feishu_ok = start_feishu_bot()
            except Exception as e:
                _server_log.warning('Feishu Bot failed: %s', e)

            return mcp_config, feishu_ok

    mcp_config, feishu_ok = asyncio.run(_startup())

    # ── Banner ──
    from lib.version import __version__ as _ver
    _mcp_count = len(mcp_config)
    _has_tls = bool(_tls_cert and _tls_key)
    _proto = 'https' if _has_tls else 'http'
    if _has_tls:
        _h2_status = 'HTTP/2 + HTTP/1.1 (TLS, auto-cert)'
    elif _behind_proxy:
        _h2_status = 'HTTP/1.1 (proxy provides HTTP/2)'
    elif _force_no_tls:
        _h2_status = 'HTTP/1.1 only (TLS disabled)'
    else:
        _h2_status = 'HTTP/1.1 (TLS unavailable — pip install cryptography)'
    _banner_lines = [
        '=' * 56,
        f'  🫧 Tofu Server  v{_ver}  [ASYNC]',
    ]
    if _behind_proxy and _vscode_proxy:
        _public_url = _vscode_proxy.replace('{{port}}', str(port))
        _banner_lines.append(f'  {_public_url}')
    _banner_lines.extend([
        f'  {_proto}://{host}:{port}',
        f'  Protocol: {_h2_status}',
        '  Server: Hypercorn (ASGI)',
    ])
    if _has_tls and not args.certfile:
        _banner_lines.append('  🔐  Self-signed cert (accept once in browser)')
    if feishu_ok:
        _banner_lines.append('  💬  Feishu Bot: ON')
    if _mcp_count > 0:
        _banner_lines.append(f'  🔌  MCP Apps: {_mcp_count} server(s)')
    if TUNNEL_TOKEN:
        _banner_lines.append('  🔒  Tunnel Auth: ON (deprecated — prefer API keys)')
    # Auth mode banner. Always show so the operator knows whether the
    # API surface is gated. Loud warning if open + non-loopback bind.
    if _AUTH_MODE == 'open':
        _banner_lines.append('  🔓  Auth: OPEN — no token required')
        if host not in ('127.0.0.1', 'localhost', '::1'):
            _banner_lines.append(
                f'  ⚠️   Bound to {host}: API is reachable on the LAN '
                'WITHOUT auth.')
            _banner_lines.append(
                '      Switch to private mode in Settings → API Keys, '
                'or set TOFU_AUTH_MODE=private.')
    elif _AUTH_MODE == 'private':
        _banner_lines.append('  🔒  Auth: PRIVATE — Bearer token required')
    elif _AUTH_MODE == 'multi-user':
        _banner_lines.append('  👥  Auth: MULTI-USER — Bearer token required')
    if _BOOTSTRAP_TOKEN:
        _banner_lines.append('  🔑  Personal admin key minted (first boot)')
        _banner_lines.append(f'      Token: {_BOOTSTRAP_TOKEN}')
        _banner_lines.append(
            f'      Open: {_proto}://{host}:{port}/?token={_BOOTSTRAP_TOKEN}')
        _banner_lines.append(
            '      Saved to data/config/.first_run_token (chmod 0600)')
        _banner_lines.append(
            '      (auto-cleared when this bootstrap key is revoked)')
    _banner_lines.append('  ⏱  Boot time: %.1fs' % (time.time() - _BOOT_T0))
    _banner_lines.append('=' * 56)
    _banner = '\n'.join(_banner_lines)
    _server_log.info('Server starting\n%s', _banner)
    _boot('Ready — handing off to Hypercorn.')
    sys.stderr.write('\n' + _banner + '\n\n')
    sys.stderr.flush()

    # ── Configure Hypercorn ──
    hconfig = HypercornConfig()
    hconfig.bind = [f'{host}:{port}']
    hconfig.accesslog = logging.getLogger('hypercorn.access')
    hconfig.errorlog = logging.getLogger('hypercorn.error')
    # SSE streams can last minutes (long LLM responses with tool use).
    # Default keep_alive_timeout=5s is fine (it's idle-between-requests),
    # but we increase it to avoid edge cases where a proxy holds a
    # connection just past the threshold. Graceful timeout for shutdown.
    hconfig.keep_alive_timeout = 600
    hconfig.graceful_timeout = 10

    # ── Listen backlog ──
    # Hypercorn's default (100) is small: if the event loop briefly stalls
    # (CPU starvation from sibling processes, a burst of slow handlers), the
    # kernel accept queue fills and further connections are dropped/reset —
    # the page "goes dark" even though the process is alive and the port is
    # bound. A larger backlog lets transient stalls QUEUE instead of failing,
    # so the browser reconnect succeeds once the loop catches up. The kernel
    # still caps this at net.core.somaxconn. Override via TOFU_LISTEN_BACKLOG.
    try:
        _listen_backlog = int(os.environ.get('TOFU_LISTEN_BACKLOG', '0') or '0')
    except (ValueError, TypeError):
        _listen_backlog = 0
    if _listen_backlog <= 0:
        _listen_backlog = 1024
    hconfig.backlog = _listen_backlog

    if _has_tls:
        hconfig.certfile = _tls_cert
        hconfig.keyfile = _tls_key

    # ── Run ──
    async def _serve():
        loop = asyncio.get_running_loop()

        # Uncaught exceptions in coroutines / callbacks never reach
        # sys.excepthook — asyncio routes them here. Default behavior only
        # logs to the root 'asyncio' logger at ERROR; funnel through our
        # 'server' logger at ERROR with the traceback so they land in
        # error.log with full context.
        def _loop_exception_handler(_loop, ctx):
            msg = ctx.get('message') or 'Unhandled exception in event loop'
            exc = ctx.get('exception')
            _server_log.error('[asyncio] %s', msg,
                              exc_info=exc if exc else False)
        loop.set_exception_handler(_loop_exception_handler)

        # ── Size the default executor ──
        # Every sync route handler runs in this loop's default executor via
        # Quart's run_sync. Python's default ThreadPoolExecutor is capped at
        # min(32, os.cpu_count()+4) — too small once long-lived sync handlers
        # (chat_send, upload, PDF parse) and per-stream poll storms coexist:
        # the pool saturates and new requests queue behind it. Size it
        # explicitly. Override via TOFU_SYNC_WORKERS.
        from concurrent.futures import ThreadPoolExecutor
        try:
            _sync_workers = int(os.environ.get('TOFU_SYNC_WORKERS', '0') or '0')
        except (ValueError, TypeError):
            _sync_workers = 0
        if _sync_workers <= 0:
            _sync_workers = min(128, (os.cpu_count() or 4) * 8)
        _executor = ThreadPoolExecutor(max_workers=_sync_workers,
                                       thread_name_prefix='tofu-sync')
        loop.set_default_executor(_executor)
        _server_log.info('[Server] Sync route executor sized to %d threads', _sync_workers)

        from lib.push import hub as _push_hub
        _push_hub.set_loop(loop)

        # Bridge the SIGTERM threading.Event to an async trigger Hypercorn
        # awaits. When set, Hypercorn stops accepting new connections and
        # drains in-flight ones within graceful_timeout. Poll cheaply (the
        # signal handler can't touch loop state directly from a thread).
        async def _shutdown_trigger():
            while not _shutdown_requested.is_set():
                await asyncio.sleep(0.25)

        await hypercorn_serve(app, hconfig, shutdown_trigger=_shutdown_trigger)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        _server_log.info('[Server] Received SIGINT — shutting down…')
