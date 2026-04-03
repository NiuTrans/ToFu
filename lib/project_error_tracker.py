"""lib/project_error_tracker.py — Universal error log scanning & resolution tracking.

The single error tracking module for the entire application.  Works with ANY
project — there is no app-specific fallback path.  When no project is
explicitly specified, the caller passes the application's own root directory.

Architecture:
  1. **Log Discovery** — Scans a project for log files by common conventions
     (logs/, log/, *.log, common framework log paths).
  2. **Multi-Format Parser** — Parses Python logging, Node.js/Winston, Java/logback,
     Go slog, Rails, syslog, and generic timestamp-level patterns.
  3. **Portable Resolutions** — Stores resolved fingerprints in
     `<project>/.chatui/error_resolutions.json` (no external DB dependency).
  4. **Fingerprinting** — Stable hashing of normalized error messages,
     stripping volatile parts (timestamps, IDs, IPs, paths, numbers).
  5. **Enrichment** — Adds fingerprint + resolved status to raw error entries.
  6. **Export** — JSON, Markdown, and CSV export for PM tool integration.

Usage:
    from lib.project_error_tracker import (
        scan_project_errors, get_unresolved_grouped, error_stats,
        mark_resolved, mark_unresolved, enrich_errors,
        export_for_pm, daily_digest, get_log_summary,
    )

    # Scan any project
    errors = scan_project_errors('/path/to/project')

    # Group unresolved errors
    grouped = get_unresolved_grouped('/path/to/project')

    # Mark a bug as fixed
    mark_resolved('/path/to/project', fingerprint='a3f7b2c1',
                  resolved_by='alice', notes='Fixed null check')

    # Export for PM tools
    csv_text = export_for_pm('/path/to/project', format='csv')
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════
#  Log File Discovery
# ══════════════════════════════════════════

# Common log directories (checked in order)
_LOG_DIR_CANDIDATES = [
    'logs', 'log', 'var/log', '.logs',
    'storage/logs',       # Laravel
    'tmp/log',            # some Ruby/Rails
    'build/logs',         # Java/Gradle
    'target/logs',        # Java/Maven
]

# Specific well-known log files
_KNOWN_LOG_FILES = [
    'error.log', 'errors.log', 'app.log', 'application.log',
    'server.log', 'debug.log', 'crash.log', 'stderr.log',
    'npm-debug.log', 'yarn-error.log', 'pip-log.txt',
    'nohup.out', 'output.log',
]

# Max log file size to scan (skip huge logs)
_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB

# Skip these directories when searching for logs
_SKIP_DIRS = {
    'node_modules', '.git', '__pycache__', '.venv', 'venv',
    '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build',
    '.next', '.nuxt', '.angular', 'vendor', 'bower_components',
}


def discover_log_files(project_path: str, max_depth: int = 3) -> list:
    """Find log files in a project directory.

    Searches common log directories and file patterns. Returns a list of
    dicts with {path, size, modified} sorted by modification time (newest first).

    Args:
        project_path: Absolute path to the project root.
        max_depth: Maximum directory depth to search.

    Returns:
        List of dicts: [{path, size, modified, relative_path}, ...]
    """
    results = []
    seen = set()
    root = Path(project_path)

    def _add_file(p: Path):
        """Add a log file to results if it meets criteria."""
        try:
            abs_str = str(p.resolve())
            if abs_str in seen:
                return
            seen.add(abs_str)
            stat = p.stat()
            if stat.st_size == 0:
                return
            if stat.st_size > _MAX_LOG_SIZE:
                logger.debug('[ErrorTracker] Skipping oversized log: %s (%d MB)',
                             p, stat.st_size // (1024 * 1024))
                return
            results.append({
                'path': abs_str,
                'relative_path': str(p.relative_to(root)),
                'size': stat.st_size,
                'modified': stat.st_mtime,
            })
        except (OSError, ValueError) as e:
            logger.debug('[ErrorTracker] Cannot stat %s: %s', p, e)

    # 1. Check known log directories
    for log_dir in _LOG_DIR_CANDIDATES:
        d = root / log_dir
        if d.is_dir():
            try:
                for f in d.iterdir():
                    if f.is_file() and f.suffix in ('.log', '.txt', '.out'):
                        _add_file(f)
                    elif f.is_file() and f.name in _KNOWN_LOG_FILES:
                        _add_file(f)
            except PermissionError:
                logger.debug('[ErrorTracker] Permission denied: %s', d)

    # 2. Check known log files at project root
    for name in _KNOWN_LOG_FILES:
        f = root / name
        if f.is_file():
            _add_file(f)

    # 3. Walk project for .log files (limited depth)
    try:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            # Compute depth
            rel = os.path.relpath(dirpath, str(root))
            depth = 0 if rel == '.' else rel.count(os.sep) + 1
            if depth >= max_depth:
                dirnames.clear()
                continue
            # Prune skip dirs
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            for fname in filenames:
                if fname.endswith('.log') or fname in _KNOWN_LOG_FILES:
                    _add_file(Path(dirpath) / fname)
    except PermissionError as _perm_err:
        import logging as _logging
        _logging.getLogger(__name__).debug('Log discovery permission denied: %s', _perm_err)

    # Sort by modification time, newest first
    results.sort(key=lambda x: x['modified'], reverse=True)
    return results


# ══════════════════════════════════════════
#  Multi-Format Log Parser
# ══════════════════════════════════════════

# Pattern matchers for different log formats, ordered by specificity
_LOG_PATTERNS = [
    # Python logging: '2025-01-15 10:30:45 [ERROR] module.name [Thread]: message'
    re.compile(
        r'^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.,]?\d*)\s*'
        r'\[(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]\s*'
        r'(?P<logger>\S+)\s*'
        r'(?:\[(?P<thread>[^\]]*)\]\s*:?\s*)?'
        r'(?P<message>.*)'
    ),
    # Java/logback: '2025-01-15 10:30:45.123 ERROR [main] c.e.MyClass - message'
    re.compile(
        r'^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.,]?\d*)\s+'
        r'(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+'
        r'\[(?P<thread>[^\]]*)\]\s+'
        r'(?P<logger>\S+)\s*[-–—]\s*'
        r'(?P<message>.*)'
    ),
    # Node.js/Winston/Pino: '{"level":"error","message":"...",...}' (JSON lines)
    # Handled separately in _try_parse_json_log

    # Go slog / structured: 'time=2025-01-15T10:30:45Z level=ERROR msg="..."'
    re.compile(
        r'^time=(?P<timestamp>\S+)\s+'
        r'level=(?P<level>\w+)\s+'
        r'msg="(?P<message>[^"]*)"'
        r'(?:\s+(?P<logger>\S+=\S+))?'
    ),
    # Rails/Ruby: 'E, [2025-01-15T10:30:45 #12345] ERROR -- : message'
    re.compile(
        r'^[A-Z],\s*\[(?P<timestamp>[^\]]+)\s+#\d+\]\s+'
        r'(?P<level>\w+)\s*--\s*(?P<logger>[^:]*):\s*'
        r'(?P<message>.*)'
    ),
    # Generic: 'YYYY-MM-DD HH:MM:SS LEVEL message' (catch-all)
    re.compile(
        r'^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.,]?\d*)\s+'
        r'(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|ERR|CRIT)\s+'
        r'(?P<message>.*)'
    ),
    # Syslog-style: 'Jan 15 10:30:45 hostname process[pid]: ERROR message'
    re.compile(
        r'^(?P<timestamp>\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+'
        r'\S+\s+'                   # hostname
        r'(?P<logger>\S+?)(?:\[\d+\])?:\s*'
        r'(?P<level>ERROR|FATAL|CRIT|ERR|CRITICAL|WARN|WARNING)\s*:?\s*'
        r'(?P<message>.*)'
    ),
]

# Error-level keywords to detect errors in unstructured logs
_ERROR_KEYWORDS = re.compile(
    r'\b(ERROR|CRITICAL|FATAL|CRIT|EXCEPTION|TRACEBACK|PANIC|'
    r'Traceback \(most recent call last\)|'
    r'Exception:|'
    r'error\[|'        # Rust-style error[E0001]
    r'FAILED|'
    r'Unhandled\s+rejection|'
    r'Segmentation\s+fault)\b',
    re.IGNORECASE
)

# Levels we consider "error" for filtering
_ERROR_LEVELS = {'ERROR', 'CRITICAL', 'FATAL', 'CRIT', 'ERR', 'SEVERE', 'PANIC'}
# Levels we include (warnings too — useful context)
_RELEVANT_LEVELS = _ERROR_LEVELS | {'WARNING', 'WARN'}


def _normalize_level(level: str) -> str:
    """Normalize level names across frameworks."""
    level = level.upper().strip()
    if level in ('ERR',):
        return 'ERROR'
    if level in ('CRIT', 'FATAL', 'PANIC'):
        return 'CRITICAL'
    if level == 'WARN':
        return 'WARNING'
    return level


def _try_parse_json_log(line: str) -> dict:
    """Try to parse a JSON-structured log line (Winston, Pino, Bunyan, etc.)."""
    if not line.startswith('{'):
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    # Detect level field (different libraries use different keys)
    level = None
    for key in ('level', 'severity', 'lvl', 'log.level'):
        if key in obj:
            raw = obj[key]
            # Pino uses numeric levels: 10=trace, 20=debug, 30=info, 40=warn, 50=error, 60=fatal
            if isinstance(raw, int):
                level = {10: 'DEBUG', 20: 'DEBUG', 30: 'INFO', 40: 'WARNING',
                         50: 'ERROR', 60: 'CRITICAL'}.get(raw, 'INFO')
            else:
                level = _normalize_level(str(raw))
            break

    if not level:
        return None

    # Extract message
    message = ''
    for key in ('message', 'msg', 'error', 'err'):
        if key in obj:
            message = str(obj[key])
            break

    # Extract timestamp
    timestamp = ''
    for key in ('timestamp', 'time', 'ts', '@timestamp', 'datetime'):
        if key in obj:
            ts_val = obj[key]
            if isinstance(ts_val, (int, float)):
                # Unix timestamp
                try:
                    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val))
                except (OSError, ValueError):
                    timestamp = str(ts_val)  # fallback to raw string — harmless for log parser
            else:
                timestamp = str(ts_val)[:26]
            break

    # Extract logger/source
    logger_name = ''
    for key in ('name', 'logger', 'source', 'component', 'module'):
        if key in obj:
            logger_name = str(obj[key])
            break

    return {
        'timestamp': timestamp,
        'level': level,
        'logger': logger_name,
        'thread': obj.get('thread', obj.get('pid', '')),
        'message': message,
        'raw': line,
    }


def parse_log_line(line: str) -> dict:
    """Parse a single log line from any supported format.

    Returns:
        dict with keys: timestamp, level, logger, thread, message, raw.
        Returns None if the line doesn't look like a log entry.
    """
    line = line.rstrip('\n\r')
    if not line.strip():
        return None

    # 1. Try JSON format first
    parsed = _try_parse_json_log(line)
    if parsed:
        return parsed

    # 2. Try structured formats
    for pattern in _LOG_PATTERNS:
        m = pattern.match(line)
        if m:
            d = m.groupdict()
            return {
                'timestamp': d.get('timestamp', ''),
                'level': _normalize_level(d.get('level', 'INFO')),
                'logger': (d.get('logger') or '').strip(),
                'thread': d.get('thread') or '',
                'message': (d.get('message') or '').strip(),
                'raw': line,
            }

    # 3. Unstructured but contains error keywords
    if _ERROR_KEYWORDS.search(line):
        return {
            'timestamp': '',
            'level': 'ERROR',
            'logger': '',
            'thread': '',
            'message': line.strip(),
            'raw': line,
        }

    return None


# ══════════════════════════════════════════
#  Error Fingerprinting
# ══════════════════════════════════════════

# Patterns to normalize before hashing — strip volatile parts.
# ORDER MATTERS: more specific patterns must come before generic ones
# (e.g. URLs before hex IDs, IPs before bare numbers).
_VOLATILE_PATTERNS = [
    # Request IDs: [rid:a3f7b2c1]
    (re.compile(r'\[rid:[0-9a-f]+\]\s*'), ''),
    # Timestamps embedded in messages: 2025-01-15 10:30:45.123Z
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.\d]*Z?'), '<TS>'),
    # URLs (before hex IDs — URLs contain hex-like substrings)
    (re.compile(r'https?://[^\s,\'"]+'), '<URL>'),
    # Memory addresses: 0x7fff5fbff8a0
    (re.compile(r'0x[0-9a-fA-F]+'), '<ADDR>'),
    # IP addresses: 10.0.0.1 (before generic numbers)
    (re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'), '<IP>'),
    # File paths: /path/to/file.py
    (re.compile(r'/[\w/\-\.]+\.\w+'), '<PATH>'),
    # UUIDs / hex IDs (8+ hex chars, not in a word context)
    (re.compile(r'\b[0-9a-f]{8,32}\b'), '<ID>'),
    # Numbers with optional unit suffixes: 30s, 60ms, 1024KB, 3.14, etc.
    (re.compile(r'\b\d+\.?\d*\s*(?:s|ms|us|ns|KB|MB|GB|TB|B|Hz)?\b'), '<N>'),
]


def compute_fingerprint(logger_name: str, message: str) -> str:
    """Compute a stable 8-char hex fingerprint for an error entry.

    Groups similar errors together by normalizing volatile parts
    (timestamps, IDs, numbers, paths) before hashing.

    Args:
        logger_name: The logger that produced the error (e.g. 'lib.llm_client').
        message: The error message text.

    Returns:
        An 8-character hex fingerprint string.
    """
    normalized = message
    for pattern, replacement in _VOLATILE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)

    # Collapse repeated whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    # Combine logger name + normalized message for the hash
    key = f'{logger_name}::{normalized}'
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]


# ══════════════════════════════════════════
#  Portable Resolution Store
# ══════════════════════════════════════════
# Stored as .chatui/error_resolutions.json inside each project.
# This makes it:
#   - Portable (move project, resolutions follow)
#   - Committable (optionally add to .gitignore or commit)
#   - No external DB dependency
#   - Works on any machine

_RESOLUTION_DIR = '.chatui'
_RESOLUTION_FILE = 'error_resolutions.json'


def _resolution_path(project_path: str) -> str:
    """Get the path to the resolution file for a project."""
    return os.path.join(project_path, _RESOLUTION_DIR, _RESOLUTION_FILE)


def get_project_resolutions(project_path: str) -> dict:
    """Load resolved fingerprints for a project.

    Returns:
        Dict mapping fingerprint → resolution record.
    """
    path = _resolution_path(project_path)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning('[ErrorTracker] Invalid resolutions file format: %s', path)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[ErrorTracker] Failed to read resolutions from %s: %s', path, e)
        return {}


def _save_project_resolutions(project_path: str, resolutions: dict):
    """Save resolved fingerprints for a project."""
    dir_path = os.path.join(project_path, _RESOLUTION_DIR)
    file_path = os.path.join(dir_path, _RESOLUTION_FILE)
    try:
        os.makedirs(dir_path, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(resolutions, f, ensure_ascii=False, indent=2)
        logger.debug('[ErrorTracker] Saved %d resolutions to %s',
                     len(resolutions), file_path)
    except OSError as e:
        logger.error('[ErrorTracker] Failed to write resolutions to %s: %s',
                     file_path, e, exc_info=True)


def mark_resolved(project_path: str, fingerprint: str,
                  resolved_by: str = '', ticket: str = '',
                  notes: str = '', logger_name: str = '',
                  sample_message: str = '') -> dict:
    """Mark an error fingerprint as resolved for a specific project.

    Args:
        project_path: Absolute path to the project.
        fingerprint: 8-char hex fingerprint.
        resolved_by: Who resolved it.
        ticket: Issue tracker reference.
        notes: Description of the fix.
        logger_name: Originating logger/module.
        sample_message: Sample error message for reference.

    Returns:
        The resolution record (includes 'fingerprint' key).
    """
    resolutions = get_project_resolutions(project_path)
    now = int(time.time())
    record = {
        'logger_name': logger_name,
        'sample_message': sample_message[:500],
        'resolved_by': resolved_by,
        'ticket': ticket,
        'notes': notes,
        'resolved_at': now,
        'updated_at': now,
    }
    # Preserve original resolved_at if updating
    if fingerprint in resolutions:
        record['resolved_at'] = resolutions[fingerprint].get('resolved_at', now)
    resolutions[fingerprint] = record
    _save_project_resolutions(project_path, resolutions)

    logger.info('[ErrorTracker] Marked resolved: fp=%s project=%s by=%s',
                fingerprint, os.path.basename(project_path), resolved_by)
    return {'fingerprint': fingerprint, **record}


def mark_unresolved(project_path: str, fingerprint: str, reason: str = '') -> bool:
    """Re-open a resolved error for a project.

    Returns:
        True if a resolution was removed.
    """
    resolutions = get_project_resolutions(project_path)
    if fingerprint not in resolutions:
        return False
    del resolutions[fingerprint]
    _save_project_resolutions(project_path, resolutions)
    logger.info('[ErrorTracker] Marked unresolved: fp=%s project=%s reason=%s',
                fingerprint, os.path.basename(project_path), reason)
    return True


# ══════════════════════════════════════════
#  Project Error Scanning
# ══════════════════════════════════════════

def scan_project_errors(project_path: str, n: int = 2000,
                        error_only: bool = True) -> list:
    """Scan a project's log files for error entries.

    Discovers log files, parses them, and returns error/warning entries
    sorted by timestamp (most recent first).

    Args:
        project_path: Absolute path to the project.
        n: Maximum number of lines to scan per log file (from tail).
        error_only: If True, only return ERROR/CRITICAL entries.
                    If False, also include WARNING.

    Returns:
        List of parsed error dicts with added 'source_file' key.
    """
    log_files = discover_log_files(project_path)
    if not log_files:
        logger.debug('[ErrorTracker] No log files found in %s', project_path)
        return []

    all_errors = []
    relevant_levels = _ERROR_LEVELS if error_only else _RELEVANT_LEVELS

    for log_info in log_files:
        file_path = log_info['path']
        rel_path = log_info['relative_path']
        try:
            with open(file_path, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

            tail = lines[-n:] if len(lines) > n else lines

            for line in tail:
                parsed = parse_log_line(line)
                if parsed and parsed['level'] in relevant_levels:
                    parsed['source_file'] = rel_path
                    all_errors.append(parsed)

        except (OSError, PermissionError) as e:
            logger.debug('[ErrorTracker] Cannot read %s: %s', file_path, e)

    # Sort by timestamp descending (newest first)
    all_errors.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
    return all_errors


def get_unresolved_grouped(project_path: str, n: int = 2000) -> list:
    """Get unresolved errors grouped by fingerprint with counts.

    Args:
        project_path: Absolute path to the project.
        n: Number of lines to scan per log file.

    Returns:
        List of grouped error dicts sorted by count descending.
    """
    errors = scan_project_errors(project_path, n=n)
    resolutions = get_project_resolutions(project_path)

    groups = {}
    for e in errors:
        fp = compute_fingerprint(e.get('logger', ''), e.get('message', ''))
        if fp in resolutions:
            continue  # Skip resolved
        if fp not in groups:
            groups[fp] = {
                'fingerprint': fp,
                'logger': e.get('logger', ''),
                'sample_message': e.get('message', '')[:300],
                'level': e.get('level', 'ERROR'),
                'source_file': e.get('source_file', ''),
                'count': 0,
                'first_seen': e.get('timestamp', ''),
                'last_seen': e.get('timestamp', ''),
            }
        groups[fp]['count'] += 1
        # Update first/last seen
        ts = e.get('timestamp', '')
        if ts and (not groups[fp]['first_seen'] or ts < groups[fp]['first_seen']):
            groups[fp]['first_seen'] = ts
        if ts and ts > groups[fp]['last_seen']:
            groups[fp]['last_seen'] = ts

    result = sorted(groups.values(), key=lambda g: g['count'], reverse=True)
    return result


def error_stats(project_path: str, n: int = 2000) -> dict:
    """Get error statistics for a project.

    Returns:
        Dict with total, resolved/unresolved counts, rate, top unresolved.
    """
    errors = scan_project_errors(project_path, n=n)
    resolutions = get_project_resolutions(project_path)

    total = len(errors)
    resolved_count = 0
    unresolved_count = 0
    all_fps = set()
    resolved_fps = set()
    unresolved_groups = {}

    for e in errors:
        fp = compute_fingerprint(e.get('logger', ''), e.get('message', ''))
        all_fps.add(fp)
        if fp in resolutions:
            resolved_count += 1
            resolved_fps.add(fp)
        else:
            unresolved_count += 1
            if fp not in unresolved_groups:
                unresolved_groups[fp] = {
                    'fingerprint': fp,
                    'logger': e.get('logger', ''),
                    'sample_message': e.get('message', '')[:200],
                    'count': 0,
                    'last_seen': '',
                }
            unresolved_groups[fp]['count'] += 1
            unresolved_groups[fp]['last_seen'] = e.get('timestamp', '')

    top_unresolved = sorted(
        unresolved_groups.values(), key=lambda g: g['count'], reverse=True
    )[:10]

    return {
        'total': total,
        'resolved_count': resolved_count,
        'unresolved_count': unresolved_count,
        'resolution_rate': round(resolved_count / total * 100, 1) if total > 0 else 0,
        'unique_fingerprints': len(all_fps),
        'unique_resolved': len(resolved_fps),
        'unique_unresolved': len(all_fps - resolved_fps),
        'top_unresolved': top_unresolved,
        'log_files_scanned': len(discover_log_files(project_path)),
    }


# ══════════════════════════════════════════
#  Enrichment & Export
# ══════════════════════════════════════════

def enrich_errors(project_path: str, errors: list) -> list:
    """Add fingerprint + resolved status to a list of parsed error entries.

    Mutates entries in-place, adding:
        - fingerprint: 8-char hex
        - resolved: bool
        - resolution: dict with resolved_by/ticket/notes (if resolved), else None

    Args:
        project_path: Absolute path to the project.
        errors: List of error dicts (from scan_project_errors or any parser
                that returns dicts with 'logger' and 'message' keys).

    Returns:
        The same list, mutated in-place with enrichment fields.
    """
    resolutions = get_project_resolutions(project_path)

    for entry in errors:
        fp = compute_fingerprint(
            entry.get('logger', ''),
            entry.get('message', ''),
        )
        entry['fingerprint'] = fp
        if fp in resolutions:
            entry['resolved'] = True
            entry['resolution'] = resolutions[fp]
        else:
            entry['resolved'] = False
            entry['resolution'] = None

    return errors


def get_resolutions_list(project_path: str) -> list:
    """Get all resolved fingerprints as a sorted list of dicts.

    Convenience wrapper around get_project_resolutions that returns a
    list (with 'fingerprint' key in each record) sorted by resolved_at
    descending.

    Args:
        project_path: Absolute path to the project.

    Returns:
        List of resolution dicts sorted by resolved_at descending.
    """
    raw = get_project_resolutions(project_path)
    result = [{'fingerprint': fp, **record} for fp, record in raw.items()]
    result.sort(key=lambda r: r.get('resolved_at', 0), reverse=True)
    return result


def daily_digest(project_path: str, hours: int = 24) -> dict:
    """Summarize errors from the last N hours.

    Scans project log files and filters to the specified time window.

    Args:
        project_path: Absolute path to the project.
        hours: Look-back window in hours (default 24).

    Returns:
        Dict with:
            - total_errors: count of ERROR/CRITICAL entries
            - total_critical: count of CRITICAL entries
            - by_logger: dict mapping logger names to error counts
            - recent_errors: last 50 error entries in the window
            - period_start: ISO timestamp (start of window)
            - period_end: ISO timestamp (now)
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

    errors = scan_project_errors(project_path, n=5000, error_only=False)

    # Filter to time window (errors are sorted newest-first)
    filtered = [e for e in errors
                if e.get('timestamp', '') >= cutoff_str]

    total_errors = sum(1 for e in filtered if e['level'] in _ERROR_LEVELS)
    total_critical = sum(1 for e in filtered if e['level'] == 'CRITICAL')

    by_logger = {}
    for e in filtered:
        if e['level'] in _ERROR_LEVELS:
            lg = e.get('logger', 'unknown') or 'unknown'
            by_logger[lg] = by_logger.get(lg, 0) + 1

    return {
        'total_errors': total_errors,
        'total_critical': total_critical,
        'by_logger': by_logger,
        'recent_errors': filtered[-50:],
        'period_start': cutoff.strftime('%Y-%m-%d %H:%M:%S'),
        'period_end': now.strftime('%Y-%m-%d %H:%M:%S'),
    }


def export_for_pm(project_path: str, n: int = 2000, format: str = 'json') -> str:
    """Export unresolved errors in a format for PM tool ingestion.

    Supported formats:
        - 'json': JSON array of grouped unresolved errors
        - 'markdown': Markdown table for Slack/Teams/GitHub
        - 'csv': CSV for spreadsheet import

    Args:
        project_path: Absolute path to the project.
        n: Number of log lines to scan per file.
        format: Output format ('json', 'markdown', 'csv').

    Returns:
        Formatted string ready for consumption.
    """
    grouped = get_unresolved_grouped(project_path, n=n)

    if format == 'json':
        return json.dumps(grouped, ensure_ascii=False, indent=2)

    elif format == 'markdown':
        lines = [
            '# Unresolved Errors Report',
            '',
            f'**Total unique patterns:** {len(grouped)}',
            f'**Total occurrences:** {sum(g["count"] for g in grouped)}',
            '',
            '| # | Fingerprint | Logger | Count | Last Seen | Source | Sample Message |',
            '|---|-------------|--------|-------|-----------|--------|----------------|',
        ]
        for i, g in enumerate(grouped[:50], 1):
            msg = g['sample_message'][:80].replace('|', '\\|')
            source = g.get('source_file', '')
            lines.append(
                f'| {i} | `{g["fingerprint"]}` | {g["logger"]} '
                f'| {g["count"]} | {g["last_seen"]} | {source} | {msg} |'
            )
        return '\n'.join(lines)

    elif format == 'csv':
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['fingerprint', 'logger', 'level', 'count',
                         'first_seen', 'last_seen', 'source_file',
                         'sample_message'])
        for g in grouped:
            writer.writerow([
                g['fingerprint'], g['logger'], g['level'], g['count'],
                g['first_seen'], g['last_seen'],
                g.get('source_file', ''), g['sample_message'],
            ])
        return output.getvalue()

    else:
        logger.warning('[ErrorTracker] Unknown export format: %s', format)
        return json.dumps(grouped, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════
#  Bulk / Pattern-Based Resolution
# ══════════════════════════════════════════

def resolve_by_logger(project_path: str, logger_name: str,
                      resolved_by: str = '', ticket: str = '',
                      notes: str = '', n: int = 2000) -> int:
    """Resolve all errors from a specific logger/module in a project.

    Args:
        project_path: Absolute path to the project.
        logger_name: Logger prefix to match (e.g. 'lib.llm_client').
        resolved_by: Who resolved it.
        ticket: Issue tracker reference.
        notes: Description of the fix.
        n: Number of log lines to scan for fingerprints.

    Returns:
        Number of unique fingerprints resolved.
    """
    errors = scan_project_errors(project_path, n=n)
    count = 0
    seen = set()
    for e in errors:
        entry_logger = e.get('logger', '')
        if not entry_logger.startswith(logger_name):
            continue
        fp = compute_fingerprint(entry_logger, e.get('message', ''))
        if fp in seen:
            continue
        seen.add(fp)
        mark_resolved(
            project_path, fp,
            resolved_by=resolved_by, ticket=ticket, notes=notes,
            logger_name=entry_logger,
            sample_message=e.get('message', ''),
        )
        count += 1

    logger.info('[ErrorTracker] resolve_by_logger: %s → %d fingerprints resolved',
                logger_name, count)
    return count


def resolve_by_message_pattern(project_path: str, pattern: str,
                               resolved_by: str = '', ticket: str = '',
                               notes: str = '', n: int = 2000) -> int:
    """Resolve errors matching a message regex pattern in a project.

    Args:
        project_path: Absolute path to the project.
        pattern: Regex pattern to match against error messages.
        resolved_by: Who resolved it.
        ticket: Issue tracker reference.
        notes: Description of the fix.
        n: Number of log lines to scan.

    Returns:
        Number of unique fingerprints resolved.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.warning('[ErrorTracker] Invalid regex: %s — %s', pattern, e)
        return 0

    errors = scan_project_errors(project_path, n=n)
    count = 0
    seen = set()
    for e in errors:
        msg = e.get('message', '')
        if not regex.search(msg):
            continue
        fp = compute_fingerprint(e.get('logger', ''), msg)
        if fp in seen:
            continue
        seen.add(fp)
        mark_resolved(
            project_path, fp,
            resolved_by=resolved_by, ticket=ticket, notes=notes,
            logger_name=e.get('logger', ''),
            sample_message=msg,
        )
        count += 1

    logger.info('[ErrorTracker] resolve_by_pattern: "%s" → %d fingerprints resolved',
                pattern, count)
    return count


# ══════════════════════════════════════════
#  Human-Readable Summaries
# ══════════════════════════════════════════

def get_log_summary(project_path: str) -> str:
    """Get a human-readable summary of discovered log files.

    Useful for the LLM to understand what logs are available in a project.

    Args:
        project_path: Absolute path to the project.

    Returns:
        Multi-line string listing discovered log files with sizes and dates.
    """
    log_files = discover_log_files(project_path)
    if not log_files:
        return 'No log files found in this project.'

    lines = [f'Found {len(log_files)} log file(s):\n']
    for lf in log_files[:20]:  # Cap display at 20 files
        size_kb = lf['size'] / 1024
        mod_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(lf['modified']))
        if size_kb >= 1024:
            size_str = f'{size_kb / 1024:.1f} MB'
        else:
            size_str = f'{size_kb:.1f} KB'
        lines.append(f'  {lf["relative_path"]}  ({size_str}, modified {mod_time})')

    if len(log_files) > 20:
        lines.append(f'  ... and {len(log_files) - 20} more')

    return '\n'.join(lines)
