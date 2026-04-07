"""Project Co-Pilot configuration, constants and shared state."""

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

IGNORE_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    '.idea', '.vscode', '.next', 'dist', 'build', '.cache',
    '.tox', '.mypy_cache', '.pytest_cache', 'target', 'vendor',
    '.bundle', 'coverage', '.nyc_output', '.eggs', '.sass-cache',
    'bower_components', '.parcel-cache', '.turbo', '.vercel',
    '.output', '.nuxt', '.svelte-kit', '.angular', 'obj', 'bin',
    '.project_indexes',
}

BINARY_EXTENSIONS = {
    '.pyc', '.pyo', '.class', '.o', '.so', '.dll', '.exe', '.bin',
    '.dat', '.db', '.sqlite', '.sqlite3',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.ogg',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar', '.xz',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.min.js', '.min.css', '.map',
}

IGNORE_FILES = {
    '.DS_Store', 'Thumbs.db', 'desktop.ini',
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    'poetry.lock', 'Cargo.lock', 'composer.lock', 'Gemfile.lock',
}

MAX_FILE_SIZE    = 512 * 1024
MAX_SCAN_FILES   = 5000
MAX_TREE_ENTRIES = 500
MAX_READ_CHARS   = 100_000
MAX_GREP_RESULTS = 50
LINE_COUNT_LIMIT = 50_000        # ★ skip line counting for files above this
SESSIONS_DIR     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                '.project_sessions')

MAX_COMMAND_TIMEOUT = None      # ★ no timeout limit for run_command
MAX_COMMAND_OUTPUT  = 100_000   # ★ max chars of command output to return
SHELL_PREFIX = os.environ.get('SHELL_PREFIX', '')  # ★ e.g. 'source ~/.bashrc &&'

# ★ Dangerous command patterns to block
# Includes both Unix and Windows equivalents for cross-platform safety.
DANGEROUS_PATTERNS = [
    # ── Unix ──
    r'\brm\s+-rf\s+/',         # rm -rf /
    r'\bmkfs\b',               # format disk
    r'\bdd\s+.*of=/',          # dd overwrite
    r'>\s*/dev/sd',            # overwrite block device
    r'\b:(){ :\|:& };:',      # fork bomb
    r'\bshutdown\b',
    r'\breboot\b',
    r'\binit\s+0\b',
    # ── Windows ──
    r'\bformat\s+[a-zA-Z]:',  # format C:
    r'\brd\s+/s\s+/q\b',      # rd /s /q (recursive delete)
    r'\bdel\s+/s\s+/q\b',     # del /s /q (recursive delete)
    r'\bdiskpart\b',           # disk partitioning
]

CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.java', '.go', '.rs', '.c', '.cpp',
    '.h', '.rb', '.php', '.swift', '.kt', '.scala', '.vue',
    '.jsx', '.tsx', '.html', '.css', '.json', '.yaml', '.toml',
    '.md', '.txt', '.sh', '.dockerfile',
}

# ★ Data / bulk files — not binary but not worth reading in full
DATA_EXTENSIONS = {
    '.jsonl', '.ndjson', '.csv', '.tsv', '.parquet',
    '.log', '.logs', '.out', '.err',
    '.sql', '.dump',
    '.xml', '.xsd', '.dtd',
    '.arff', '.sav', '.rec', '.ftr', '.feather',
}
# ★ Max chars returned to LLM for data files in tool_read_file
MAX_DATA_FILE_PREVIEW = 2000


# ═══════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════

_lock = threading.RLock()
_state = {
    'path': None, 'tree': None,
    'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
    'languages': {}, 'scannedAt': 0,
    # ★ Async scanning
    'scanning': False, 'scanProgress': '', 'scanDetail': '',
    # ★ Modification history for undo (后悔药)
    'sessionId': None, 'modifications': [],
}

# ═══════════════════════════════════════════════════════
#  ★ Multi-Root Workspace Support
# ═══════════════════════════════════════════════════════
# Each root is stored as:  { name: { path, tree, fileCount, ... } }
# The _state above remains the "primary" root for backward compat.
# _roots dict stores *all* roots including the primary.

_roots = {}  # name → per-root state dict


def _make_root_state(abs_path):
    """Create a fresh per-root state dict."""
    return {
        'path': abs_path, 'tree': None,
        'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
        'languages': {}, 'scannedAt': 0,
        'scanning': False, 'scanProgress': '', 'scanDetail': '',
    }

def get_roots():
    """Return a snapshot of all workspace roots."""
    with _lock:
        return {name: dict(st) for name, st in _roots.items()}

def get_root_path(name):
    """Get the absolute path of a named root, or None."""
    with _lock:
        r = _roots.get(name)
        return r['path'] if r else None

def resolve_namespaced_path(rel_path):
    """
    Parse 'rootname:some/rel/path' → (abs_base, rel).
    If no ':' prefix, fall back to the primary root.
    Returns (base_path, relative_path) or raises ValueError.
    """
    with _lock:
        if ':' in rel_path and not os.path.isabs(rel_path.split(':')[0]):
            name, _, rest = rel_path.partition(':')
            r = _roots.get(name)
            if not r:
                # ★ Try case-insensitive match before failing
                for rn, rs in _roots.items():
                    if rn.lower() == name.lower():
                        logger.debug('[Config] Case-insensitive root match: %s → %s', name, rn)
                        return rs['path'], rest or '.'
                raise ValueError(f'Unknown workspace root: {name}  (available: {", ".join(_roots.keys()) or "none"})')
            logger.debug('[Config] Resolved namespaced path: %s → base=%s rel=%s', rel_path, r['path'], rest or '.')
            return r['path'], rest or '.'
        # Fallback: primary root
        primary = _state['path']
        if not primary:
            raise ValueError('No project path set')
        return primary, rel_path


class _ScanAborted(Exception):
    pass


def get_state():
    with _lock:
        s = dict(_state)
        # Include modification count for undo
        s['modificationsCount'] = len(_state.get('modifications', []))
        # ★ Always include extra roots so the frontend stays in sync
        extra = []
        primary = _state.get('path')
        for rn, rs in _roots.items():
            if rs['path'] != primary:
                extra.append({'path': rs['path'], 'name': rn,
                              'fileCount': rs['fileCount'],
                              'scanning': rs['scanning']})
        s['extraRoots'] = extra
        # ★ Cross-DC latency indicator
        try:
            from lib.cross_dc import get_cluster_for_path, get_latency_class, get_latency_s
            if primary:
                lat_class = get_latency_class(primary)
                if lat_class != 'unknown':
                    s['crossDC'] = {
                        'latencyClass': lat_class,
                        'cluster': get_cluster_for_path(primary),
                        'latencyMs': round(get_latency_s(primary) * 1000, 1) if get_latency_s(primary) else None,
                    }
        except Exception as e:
            logger.debug('[Config] cross_dc info unavailable: %s', e)
        return s


def get_project_path():
    with _lock:
        return _state['path']


# ═══════════════════════════════════════════════════════
#  ★ Recent Projects (server-side persistence)
# ═══════════════════════════════════════════════════════

def get_recent_projects():
    """Return list of recent projects sorted by last_used desc."""
    from lib.database import DOMAIN_SYSTEM, get_db
    rows = get_db(DOMAIN_SYSTEM).execute(
        'SELECT path, count, last_used FROM recent_projects ORDER BY last_used DESC LIMIT 20'
    ).fetchall()
    return [{'path': r['path'], 'count': r['count'], 'last_used': r['last_used']} for r in rows]


def save_recent_project(path):
    """Insert or update a recent project entry."""
    import time

    from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, get_db
    db = get_db(DOMAIN_SYSTEM)
    now = int(time.time())
    db_execute_with_retry(
        db,
        '''INSERT INTO recent_projects (path, "count", last_used) VALUES (?, 1, ?)
           ON CONFLICT(path) DO UPDATE SET "count" = recent_projects."count" + 1, last_used = EXCLUDED.last_used''',
        (path, now),
    )


def clear_recent_projects():
    """Delete all recent project entries."""
    from lib.database import DOMAIN_SYSTEM, get_db
    db = get_db(DOMAIN_SYSTEM)
    db.execute('DELETE FROM recent_projects')
    db.commit()


# ═══════════════════════════════════════════════════════
#  ★ Modification History (后悔药 / Undo)
