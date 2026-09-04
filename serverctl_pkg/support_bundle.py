"""Build a bounded, sanitized Tofu support bundle without requiring the server.

Responsibility:
  - collect platform/version/config-name metadata and bounded log tails;
  - recursively redact credentials from structured and free-form data;
  - never import the application, open its database, or contact external services;
  - accept a read-only doctor report that may have probed the local manager and
    loopback health endpoint.

Entry point: ``build_support_bundle(project, doctor_report, lines=200)``.
Dependencies: Python standard library and the read-only doctor report supplied
by ``serverctl.py``.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tofu_dotenv import MAX_DOTENV_BYTES, parse_dotenv


SCHEMA = 'tofu.support-bundle/v1'
MAX_LOG_BYTES = 256 * 1024
MAX_TOTAL_LOG_BYTES = 512 * 1024
MAX_ENV_BYTES = MAX_DOTENV_BYTES
MAX_LOG_LINES = 1000
MAX_LOG_FILES = 12
REDACTED = '<redacted>'

_SENSITIVE_KEY_SUFFIXES = (
    'api_key', 'api_keys', 'apikey', 'authorization', 'cookie', 'credential',
    'credentials', 'passphrase', 'password', 'private_key', 'secret',
    'session_key', 'token',
)
_SENSITIVE_KEY_PARTS = {
    'authorization', 'bearer', 'cookie', 'credential', 'credentials', 'key',
    'passphrase', 'password', 'secret', 'token',
}
_SAFE_METADATA_KEYS = {'credentials_redacted'}
_OPERATIONAL_ENV_KEYS = {
    'BIND_HOST', 'FALLBACK_MODEL', 'FLASK_DEBUG', 'LLM_BASE_URL', 'LLM_MODEL',
    'PORT', 'TOFU_DATA_LAYOUT', 'TOFU_DEPLOYMENT_MODE', 'TOFU_PROCESS_ROLE',
    'TOFU_PROCESS_RSS_RECYCLE_MB', 'TOFU_PROCESS_RSS_RELIEF_MB',
    'TOFU_RUNTIME_STATE_BACKEND', 'TOFU_SQLITE_SNAPSHOT_DIR',
    'TOFU_STORAGE_RPC_CAPACITY', 'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB',
    'TOFU_STORAGE_SQLITE_READ_POOL', 'TOFU_TLS',
}

_RAW_SECRET_PATTERNS = (
    re.compile(r'(?i)\b(?:sk|tofu_admin)_[A-Za-z0-9._-]{8,}\b'),
    re.compile(r'(?i)\bsk-[A-Za-z0-9._-]{8,}\b'),
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(r'(?i)\b(?:github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9._-]{12,}\b'),
    re.compile(r'\bAIza[0-9A-Za-z_-]{20,}\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'),
)
_AUTH_HEADER_RE = re.compile(
    r'(?i)(\b(?:proxy-)?authorization\s*[:=]\s*'
    r'(?:bearer\s+|basic\s+)?)([^\s,;]+)')
_NAMED_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?keys?|access[_-]?token|client[_-]?secret|cookie|'
    r'password|private[_-]?key|refresh[_-]?token|session[_-]?key|token)'
    r'["\']?\s*[:=]\s*)(["\']?)([^\s,"\';}\]]+)(["\']?)')
_NAMED_SECRET_LIST_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?keys|credentials|tokens)["\']?\s*[:=]\s*)'
    r'(["\']?)([^\s;}\]]+)')
_QUERY_SECRET_RE = re.compile(
    r'(?i)([?&](?:api[_-]?key|access[_-]?token|key|token)=)([^&#\s]+)')
_URL_PASSWORD_RE = re.compile(
    r'(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)')
_ENV_ASSIGN_RE = re.compile(
    r'(?m)^(\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(.*)$')
_PEM_RE = re.compile(
    r'-----BEGIN [^-\n]*(?:PRIVATE KEY|SECRET)[^-\n]*-----.*?'
    r'-----END [^-\n]*(?:PRIVATE KEY|SECRET)[^-\n]*-----',
    re.DOTALL | re.IGNORECASE)


def _is_sensitive_key(name: object) -> bool:
    snake = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', str(name))
    normalized = re.sub(r'[^a-z0-9]+', '_', snake.lower()).strip('_')
    if normalized in _SAFE_METADATA_KEYS:
        return False
    return bool(set(normalized.split('_')) & _SENSITIVE_KEY_PARTS) or any(
        normalized == suffix or normalized.endswith('_' + suffix)
        for suffix in _SENSITIVE_KEY_SUFFIXES
    ) or normalized.startswith(('authorization_', 'cookie_', 'set_cookie_'))


def sanitize_text(value: str) -> str:
    """Redact common credential forms from an unstructured string."""
    text = str(value)
    text = _ENV_ASSIGN_RE.sub(
        lambda match: match.group(1) + REDACTED
        if _is_sensitive_key(match.group(2)) else match.group(0),
        text)
    text = _PEM_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(r'\1' + REDACTED, text)
    text = _NAMED_SECRET_LIST_RE.sub(r'\1\2' + REDACTED, text)
    text = _NAMED_SECRET_RE.sub(r'\1' + REDACTED, text)
    text = _QUERY_SECRET_RE.sub(r'\1' + REDACTED, text)
    text = _URL_PASSWORD_RE.sub(r'\1' + REDACTED + r'\3', text)
    for pattern in _RAW_SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def sanitize_value(value: Any, *, key: object = '') -> Any:
    """Recursively sanitize JSON-like data without changing its shape."""
    if _is_sensitive_key(key):
        return REDACTED if value not in (None, '', [], {}) else value
    if isinstance(value, dict):
        return {str(item_key): sanitize_value(item, key=item_key)
                for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _read_version(project: Path) -> str:
    try:
        return (project / 'VERSION').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


def _git_state(project: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ['git', *args], cwd=project, capture_output=True, text=True,
            timeout=2, check=False)
        return result.stdout.strip() if result.returncode == 0 else ''

    try:
        revision = run('rev-parse', '--short=12', 'HEAD')
        dirty = bool(run('status', '--porcelain', '--untracked-files=no'))
    except (OSError, subprocess.SubprocessError):
        revision, dirty = '', False
    return {'revision': revision or None, 'trackedFilesModified': dirty}


def _parse_env(project: Path) -> dict[str, Any]:
    path = project / '.env'
    try:
        if path.stat().st_size > MAX_ENV_BYTES:
            return {'path': str(path), 'error': f'file exceeds {MAX_ENV_BYTES} bytes'}
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return {'path': str(path), 'present': False, 'configuredKeys': [],
                'sensitiveKeysConfigured': [], 'operationalValues': {}}
    except OSError as exc:
        return {'path': str(path), 'present': True, 'error': sanitize_text(str(exc))}

    values = parse_dotenv('\n'.join(lines))
    sensitive = sorted(name for name, value in values.items()
                       if value and _is_sensitive_key(name))
    operational = {
        name: sanitize_text(values[name])
        for name in sorted(values)
        if name in _OPERATIONAL_ENV_KEYS and values[name]
    }
    return {
        'path': str(path),
        'present': True,
        'configuredKeys': sorted(values),
        'sensitiveKeysConfigured': sensitive,
        'operationalValues': operational,
    }


def _environment_marker(project: Path) -> dict[str, Any]:
    """Read only the documented interpreter-selection fields."""
    path = project / '.tofu_env.json'
    try:
        if path.stat().st_size > MAX_ENV_BYTES:
            return {'path': str(path), 'error': f'file exceeds {MAX_ENV_BYTES} bytes'}
        parsed = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(parsed, dict):
            raise ValueError('marker root is not an object')
        return {
            'path': str(path),
            'present': True,
            **{name: parsed.get(name) for name in (
                'backend', 'python', 'env_prefix', 'env_name', 'owned_by_tofu_install')
               if parsed.get(name) is not None},
        }
    except FileNotFoundError:
        return {'path': str(path), 'present': False}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {'path': str(path), 'present': True,
                'error': sanitize_text(str(exc))}


def _known_log_dirs(project: Path) -> list[Path]:
    candidates = [project / 'logs']
    explicit = os.environ.get('TOFU_DATA_DIR')
    if explicit:
        root = Path(os.path.expandvars(os.path.expanduser(explicit)))
        candidates.append((root.parent if root.name == 'data' else root) / 'logs')
    if sys.platform.startswith('win'):
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'Tofu'
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support' / 'Tofu'
    else:
        root = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'Tofu'
    candidates.append(root / 'logs')
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _safe_log_path(path: Path, allowed_dirs: list[Path]) -> Path | None:
    resolved = path.resolve(strict=False)
    fixed_names = {
        'app.log', 'cgroup_pressure.log', 'error.log', 'faulthandler.log',
        'incident.jsonl', 'postgresql.log', 'server-console.log', 'server-manager.log',
        'storage-postgresql.log', 'watchdog.log',
    }
    if resolved.name not in fixed_names \
            and not re.fullmatch(
                r'(?:install-\d{8}_\d{6}(?:-\d+)?|tofu_faulthandler_\d+)\.log',
                resolved.name):
        return None
    for allowed in allowed_dirs:
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    return None


def _tail_log(path: Path, *, lines: int, max_bytes: int = MAX_LOG_BYTES) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        with path.open('rb') as stream:
            byte_limit = max(1, min(MAX_LOG_BYTES, int(max_bytes)))
            start = max(0, size - byte_limit)
            stream.seek(start)
            raw = stream.read(byte_limit)
        if start and b'\n' in raw:
            raw = raw.split(b'\n', 1)[1]
        decoded = raw.decode('utf-8', 'replace').splitlines()[-lines:]
        return {
            'path': str(path),
            'available': True,
            'sourceBytes': size,
            'sourceTailBytesRead': len(raw),
            'tailLines': len(decoded),
            'truncated': start > 0 or len(raw.splitlines()) > lines,
            'content': sanitize_text('\n'.join(decoded)),
        }
    except FileNotFoundError:
        return {'path': str(path), 'available': False}
    except OSError as exc:
        return {'path': str(path), 'available': False,
                'error': sanitize_text(str(exc))}


def _collect_logs(project: Path, doctor_report: dict, *, lines: int) -> dict[str, Any]:
    allowed = _known_log_dirs(project)
    status = doctor_report.get('managerStatus') or {}
    requested = {
        'worker': status.get('workerLog') or project / 'logs' / 'server-console.log',
        'manager': status.get('managerLog') or project / 'logs' / 'server-manager.log',
        'errors': project / 'logs' / 'error.log',
        'incidents': project / 'logs' / 'incident.jsonl',
        'application': project / 'logs' / 'app.log',
        'postgresql': project / 'logs' / 'postgresql.log',
        'storage': project / 'logs' / 'storage-postgresql.log',
        'resourcePressure': project / 'logs' / 'cgroup_pressure.log',
        'legacyWatchdog': project / 'logs' / 'watchdog.log',
        'faulthandler': project / 'logs' / 'faulthandler.log',
    }
    # A fresh source checkout may place app/error logs under XDG. Prefer the
    # known alternative only when the in-tree file is absent.
    for role, filename in (('errors', 'error.log'), ('application', 'app.log')):
        if not Path(requested[role]).is_file():
            replacement = next((root / filename for root in allowed
                                if (root / filename).is_file()), None)
            if replacement is not None:
                requested[role] = replacement
    install_candidates = []
    for root in allowed:
        for path in root.glob('install-*.log'):
            safe_path = _safe_log_path(path, allowed)
            if safe_path is not None and safe_path.is_file():
                install_candidates.append(safe_path)
    if install_candidates:
        try:
            requested['installer'] = max(
                install_candidates, key=lambda path: path.stat().st_mtime)
        except OSError:
            pass
    worker_pid = status.get('pid')
    fault_pattern = (
        f'tofu_faulthandler_{worker_pid}.log'
        if isinstance(worker_pid, int) and worker_pid > 0
        else 'tofu_faulthandler_*.log')
    fault_candidates = []
    for root in allowed:
        for path in root.glob(fault_pattern):
            safe_path = _safe_log_path(path, allowed)
            if safe_path is not None and safe_path.is_file():
                fault_candidates.append(safe_path)
    if not fault_candidates and '*' not in fault_pattern:
        for root in allowed:
            for path in root.glob('tofu_faulthandler_*.log'):
                safe_path = _safe_log_path(path, allowed)
                if safe_path is not None and safe_path.is_file():
                    fault_candidates.append(safe_path)
    if fault_candidates:
        try:
            requested['faulthandler'] = max(
                fault_candidates, key=lambda path: path.stat().st_mtime)
        except OSError:
            pass
    bounded_requested = list(requested.items())[:MAX_LOG_FILES]
    safe_paths = {
        role: _safe_log_path(Path(raw_path), allowed)
        for role, raw_path in bounded_requested
    }
    available_count = sum(
        1 for path in safe_paths.values()
        if path is not None and path.is_file())
    per_file_bytes = min(
        MAX_LOG_BYTES,
        MAX_TOTAL_LOG_BYTES // max(1, available_count),
    )
    result: dict[str, Any] = {}
    for role, _raw_path in bounded_requested:
        safe_path = safe_paths[role]
        if safe_path is None:
            result[role] = {
                'available': False,
                'error': 'refused log path outside known Tofu log directories',
            }
        else:
            result[role] = _tail_log(
                safe_path, lines=lines, max_bytes=per_file_bytes)
    return result


def _disk_snapshot(project: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(project)
        return {'totalBytes': usage.total, 'usedBytes': usage.used,
                'freeBytes': usage.free}
    except OSError as exc:
        return {'error': sanitize_text(str(exc))}


def build_support_bundle(
        project: str | os.PathLike[str], doctor_report: dict, *,
        lines: int = 200, include_logs: bool = True) -> dict[str, Any]:
    """Return one safe, bounded JSON object suitable for a bug report."""
    root = Path(project).resolve()
    line_limit = max(1, min(MAX_LOG_LINES, int(lines)))
    bundle = {
        'schema': SCHEMA,
        'collectedAt': _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        'privacy': {
            'credentialsRedacted': True,
            'redactionIsBestEffort': True,
            'databaseOpened': False,
            'externalNetworkRequestsMade': False,
            'loopbackDiagnosticsMayRequestLocalServices': True,
            'conversationStorageRead': False,
            'hostMetadataMayIdentifyMachineOrUser': True,
            'logTailsIncluded': bool(include_logs),
            # Application/errors can legitimately quote bounded user input.
            # Credential redaction cannot identify arbitrary prose, so never
            # imply that a log-bearing artifact is conversation-content-free.
            'logTailsMayContainUserContent': bool(include_logs),
            'reviewBeforeSharing': True,
        },
        'limits': {'logLinesPerFile': line_limit,
                   'logBytesPerFile': MAX_LOG_BYTES,
                   'maxLogFiles': MAX_LOG_FILES,
                   'maxTotalLogBytes': MAX_TOTAL_LOG_BYTES},
        'tofu': {
            'version': _read_version(root),
            'projectPath': str(root),
            'git': _git_state(root),
        },
        'runtime': {
            'python': sys.version.splitlines()[0],
            'executable': sys.executable,
            'platform': platform.platform(),
            'machine': platform.machine(),
            'pid': os.getpid(),
        },
        'config': {
            'environment': _parse_env(root),
            'interpreterMarker': _environment_marker(root),
        },
        'disk': _disk_snapshot(root),
        'doctor': doctor_report,
        'logs': (_collect_logs(root, doctor_report, lines=line_limit)
                 if include_logs else {}),
    }
    return sanitize_value(bundle)


def write_support_bundle(path: str | os.PathLike[str], bundle: dict[str, Any]) -> Path:
    """Write mode-0600 JSON without overwriting an existing support bundle."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target
