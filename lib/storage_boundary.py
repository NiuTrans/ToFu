"""Static production cutover gate for exclusive Sidecar ownership."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import json
import socket
import subprocess


_DRIVERS = frozenset({'sqlite3', 'psycopg', 'psycopg2'})
_BANNED_CALLS = frozenset({
    'get_thread_db', 'pooled_db', 'write_transaction',
    'db_execute_with_retry', 'allocate_scoped_sequence',
    'lock_scoped_sequence',
})
_TRANSACTION_METHODS = frozenset({'cursor', 'commit', 'rollback'})
_DATABASE_RECEIVER_NAMES = frozenset({
    'conn', 'connection', 'connections', 'cur', 'cursor', 'database', 'db',
    'session', 'transaction', 'tx', 'unit_of_work', 'uow',
})
_SQL_PREFIXES = (
    'select ', 'insert ', 'update ', 'delete ', 'create ', 'alter ',
    'drop ', 'pragma ', 'begin ', 'commit ', 'rollback ', 'vacuum ',
)
_BOUNDARY_CACHE_VERSION = 22
_BOUNDARY_CACHE_NAME = '.tofu-storage-boundary-cache.json'
_BOUNDARY_SEARCH_PATTERN = (
    r'\b(?:sqlite3|psycopg|psycopg2|get_thread_db|pooled_db|'
    r'write_transaction|db_execute_with_retry|allocate_scoped_sequence|'
    r'lock_scoped_sequence|cursor|commit|rollback|connect|execute|'
    r'executemany)\b|tofu.{0,64}schema')

@dataclass(frozen=True, slots=True)
class BoundaryViolation:
    path: str
    line: int
    capability: str

    def as_dict(self) -> dict[str, object]:
        return {
            'path': self.path, 'line': self.line,
            'capability': self.capability,
        }


def _source_roots(project_root: Path) -> list[Path]:
    # The cutover gate certifies the Web process, not offline migration and
    # backup utilities.  Those scripts are intentionally allowed to open the
    # source authority during a controlled maintenance window; including them
    # here made a server startup fail because of code that cannot be imported
    # by the server at all.  Their separate migration tests still enforce
    # explicit invocation and rollback semantics.
    roots = [project_root / 'lib', project_root / 'routes']
    server = project_root / 'server.py'
    return [*roots, server]


def _python_files(project_root: Path):
    # BeeGFS directory metadata walks are materially slower than reading the
    # repository index.  Include tracked and non-ignored untracked files; a
    # source package without Git falls back to a pruned local walk.
    try:
        indexed = subprocess.run(
            ['git', 'ls-files', '-co', '--exclude-standard', '--',
             'lib', 'routes', 'scripts', 'server.py'],
            cwd=project_root, text=True, capture_output=True,
            check=True, timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        indexed = []
        for root in _source_roots(project_root):
            if root.is_file():
                indexed.append(root.relative_to(project_root).as_posix())
                continue
            if not root.exists():
                continue
            for directory, names, files in os.walk(root):
                names[:] = [name for name in names if not name.startswith('.')]
                base = Path(directory)
                indexed.extend(
                    (base / name).relative_to(project_root).as_posix()
                    for name in files if name.endswith('.py'))
    all_files = sorted(set(indexed))
    candidates = _candidate_python_files(project_root)
    if candidates is not None:
        # Every capability recognized by the AST scanner contains one of the
        # lexical markers above. Avoid constructing ASTs for the thousands of
        # unrelated Python files on high-latency project filesystems. The
        # conservative fallback below keeps the old full scan when ripgrep is
        # unavailable or returns an infrastructure error.
        all_files = [relative for relative in all_files
                     if relative in candidates]
    for relative in all_files:
        if not relative.endswith('.py'):
            continue
        if relative.startswith('lib/storage_sidecar/'):
            continue
        yield project_root / relative


def _candidate_python_files(project_root: Path) -> set[str] | None:
    search_paths = [str(path.relative_to(project_root))
                    for path in _source_roots(project_root) if path.exists()]
    if not search_paths:
        return set()
    try:
        result = subprocess.run(
            ['rg', '-l', '--no-messages', '--glob', '*.py', '-e',
             _BOUNDARY_SEARCH_PATTERN, *search_paths],
            cwd=project_root, text=True, capture_output=True,
            check=False, timeout=10,
)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    candidates = set()
    for raw in result.stdout.splitlines():
        path = Path(raw.strip())
        try:
            relative = (path.resolve().relative_to(project_root.resolve())
                        if path.is_absolute() else path.as_posix())
        except ValueError:
            continue
        candidates.add(Path(relative).as_posix())
    return candidates


def _boundary_cache_path(project_root: Path) -> Path:
    return project_root / 'data' / _BOUNDARY_CACHE_NAME


def _file_signature(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        'mtime_ns': int(stat.st_mtime_ns),
        'ctime_ns': int(stat.st_ctime_ns),
        'size': int(stat.st_size),
    }


def _read_boundary_cache(project_root: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(
            _boundary_cache_path(project_root).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get('version') != _BOUNDARY_CACHE_VERSION:
        return {}
    files = payload.get('files')
    return files if isinstance(files, dict) else {}


def _write_boundary_cache(project_root: Path,
                          entries: dict[str, dict[str, object]]) -> None:
    path = _boundary_cache_path(project_root)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({
            'version': _BOUNDARY_CACHE_VERSION,
            'files': entries,
        }, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _cached_violations(value: object) -> list[BoundaryViolation] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, dict):
            return None
        try:
            result.append(BoundaryViolation(
                str(item['path']), int(item['line']), str(item['capability'])))
        except (KeyError, TypeError, ValueError):
            return None
    return result


def _scan_python_file(path: Path, relative: str) -> list[BoundaryViolation]:
    try:
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=relative)
    except (OSError, SyntaxError) as exc:
        return [BoundaryViolation(
            relative, int(getattr(exc, 'lineno', 0) or 0),
            'unscannable_python')]

    violations: list[BoundaryViolation] = []

    def database_shaped_receiver(node: ast.AST) -> bool:
        """Recognize DB-handle receivers without reserving common verbs.

        ``cursor``, ``commit`` and ``rollback`` are ordinary domain method
        names too. Treating every attribute with one of those names as a
        database capability made unrelated application services capable of
        blocking production startup. Driver imports, connection factories,
        banned DB helpers and literal SQL calls are detected independently;
        this receiver check retains the transaction-method signal for
        conventional DB handle names and attribute chains.
        """
        identifiers: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            identifiers.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            identifiers.append(current.id)
        elif isinstance(current, ast.Subscript):
            return database_shaped_receiver(current.value)

        for raw_identifier in identifiers:
            identifier = raw_identifier.strip('_').lower()
            if identifier in _DATABASE_RECEIVER_NAMES:
                return True
            if identifier.startswith((
                    'db_', 'database_', 'conn_', 'connection_',
                    'cursor_', 'transaction_')):
                return True
            if identifier.endswith((
                    '_db', '_database', '_conn', '_connection',
                    '_cursor', '_transaction')):
                return True
        return False

    socket_names = {'socket'}
    for imported in ast.walk(tree):
        if isinstance(imported, ast.ImportFrom) and imported.module == 'socket':
            for alias in imported.names:
                socket_names.add(alias.asname or alias.name)
        if (isinstance(imported, ast.Assign)
                and isinstance(imported.value, ast.Call)
                and isinstance(imported.value.func, ast.Attribute)
                and imported.value.func.attr == 'socket'):
            for target in imported.targets:
                if isinstance(target, ast.Name):
                    socket_names.add(target.id)

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in _BANNED_CALLS:
                violations.append(BoundaryViolation(
                    relative, node.lineno, function.id))
            elif isinstance(function, ast.Attribute):
                if (function.attr in _TRANSACTION_METHODS
                        and database_shaped_receiver(function.value)):
                    violations.append(BoundaryViolation(
                        relative, node.lineno,
                        f'direct_{function.attr}'))
                elif function.attr in {'connect', 'execute', 'executemany'}:
                    if (function.attr == 'connect'
                            and isinstance(function.value, ast.Name)
                            and function.value.id in socket_names):
                        self.generic_visit(node)
                        return
                    first = _literal_text(node.args[0]) if node.args else None
                    if (function.attr == 'connect'
                            or first is not None
                            and first.lstrip().lower().startswith(_SQL_PREFIXES)):
                        violations.append(BoundaryViolation(
                            relative, node.lineno,
                            f'direct_{function.attr}'))
            self.generic_visit(node)

    Visitor().visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] in _DRIVERS:
                    violations.append(BoundaryViolation(
                        relative, node.lineno, 'database_driver_import'))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module.split('.')[0] in _DRIVERS:
                violations.append(BoundaryViolation(
                    relative, node.lineno, 'database_driver_import'))
    # Keep the scanner's own literal out of the plugin callback inventory.
    if ('tofu' + '.schema') in source:
        violations.append(BoundaryViolation(
            relative, 0, 'executable_plugin_schema_callback'))
    return violations


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_storage_boundary(project_root: Path) -> list[BoundaryViolation]:
    """Return every forbidden in-process storage capability in production."""
    root = project_root.resolve()
    violations: list[BoundaryViolation] = []
    cached = _read_boundary_cache(root)
    next_cache: dict[str, dict[str, object]] = {}
    for path in _python_files(root):
        relative = path.relative_to(root).as_posix()
        signature = _file_signature(path)
        old = cached.get(relative)
        old_signature = old.get('signature') if isinstance(old, dict) else None
        old_violations = _cached_violations(
            old.get('violations') if isinstance(old, dict) else None)
        if signature is not None and old_signature == signature and old_violations is not None:
            file_violations = old_violations
        else:
            file_violations = _scan_python_file(path, relative)
        violations.extend(file_violations)
        if signature is not None:
            next_cache[relative] = {
                'signature': signature,
                'violations': [item.as_dict() for item in file_violations],
            }
    if next_cache != cached:
        _write_boundary_cache(root, next_cache)
    return sorted(
        set(violations), key=lambda item: (item.path, item.line, item.capability))


def boundary_report(project_root: Path) -> dict[str, object]:
    violations = scan_storage_boundary(project_root)
    files = sorted({item.path for item in violations})
    return {
        'ready': not violations,
        'violation_count': len(violations),
        'file_count': len(files),
        'files': files,
        'violations': [item.as_dict() for item in violations],
    }


_STRICT_INVENTORY_ROOTS = ('lib', 'routes', 'scripts', 'server.py', 'tools')


def strict_inventory(project_root: Path) -> dict[str, object]:
    """Measure every direct storage capability outside the Sidecar.

    The inventory is a ratchet for offline operator tools and any accidental
    application ownership that the live boundary would reject.

    Tracked and non-ignored untracked files are read from Git's index rather
    than by walking the workspace. This makes a newly added migration fail the
    ratchet before its first commit while keeping high-latency filesystem work
    bounded. Sidecar internals legitimately own drivers and are exempt.
    """
    root = project_root.resolve()
    try:
        indexed = subprocess.run(
            ['git', 'ls-files', '-co', '--exclude-standard', '--',
             *_STRICT_INVENTORY_ROOTS],
            cwd=root, text=True, capture_output=True,
            check=True, timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        indexed = []
        for relative_root in _STRICT_INVENTORY_ROOTS:
            base = root / relative_root
            if base.is_file():
                indexed.append(relative_root)
                continue
            if not base.exists():
                continue
            for directory, names, files in os.walk(base):
                names[:] = [n for n in names if not n.startswith('.')]
                indexed.extend(
                    str((Path(directory) / name).relative_to(root))
                    for name in files if name.endswith('.py'))
    per_file: dict[str, list[dict[str, object]]] = {}
    for relative in sorted(set(indexed)):
        if not relative.endswith('.py'):
            continue
        if relative.startswith(('lib/storage_sidecar/', 'lib/storage/')):
            continue
        source_path = root / relative
        # Cached index entries remain visible until a deletion is staged. A
        # missing file is retired surface, not an unscannable capability.
        if not source_path.is_file():
            continue
        file_violations = _scan_python_file(source_path, relative)
        if file_violations:
            per_file[relative] = [
                item.as_dict() for item in file_violations]
    total = sum(len(items) for items in per_file.values())
    counts = {path: len(items) for path, items in per_file.items()}
    return {
        'total': total,
        'file_count': len(per_file),
        'files': counts,
        'violations': per_file,
    }


def _owner_record(path: Path, *, kind: str) -> dict[str, object]:
    record: dict[str, object] = {
        'kind': kind, 'path': path.name, 'present': path.is_file(),
        'active': False, 'pid': None, 'host': None, 'status': 'absent',
    }
    if not record['present']:
        return record
    try:
        if kind == 'web':
            raw = path.read_text(encoding='utf-8').splitlines()[0].strip()
            pid_text, separator, host = raw.partition('@')
            if not separator or not pid_text.isdigit():
                raise ValueError('invalid Web owner stamp')
            pid = int(pid_text)
            status = 'running'
        else:
            document = json.loads(path.read_text(encoding='utf-8'))
            pid = int(document['pid'])
            host = str(document.get('host') or '')
            status = str(document.get('status') or 'unknown')
        record.update(pid=pid, host=host or None, status=status)
        if status != 'running':
            return record
        if host and host != socket.gethostname():
            record['active'] = True
            record['status'] = 'running_foreign_host'
            return record
        try:
            os.kill(pid, 0)
        except OSError:
            record['status'] = 'stale'
        else:
            record['active'] = True
    except (OSError, ValueError, KeyError, TypeError, IndexError,
            json.JSONDecodeError):
        record['status'] = 'unverifiable'
        record['active'] = True
    return record


def cutover_report(project_root: Path) -> dict[str, object]:
    root = project_root.resolve()
    static = boundary_report(root)
    data_dir = root / 'data'
    owners = [
        _owner_record(data_dir / '.server.lock', kind='web'),
        _owner_record(data_dir / '.storage-sidecar-lease.json', kind='sidecar'),
    ]
    quiescent = not any(owner['active'] for owner in owners)
    return {
        'ready': bool(static['ready'] and quiescent),
        'static_boundary': static,
        'ownership': {'quiescent': quiescent, 'owners': owners},
    }


def require_exclusive_sidecar_boundary(project_root: Path) -> None:
    report = boundary_report(project_root)
    if report['ready']:
        return
    files = report['files']
    preview = ', '.join(files[:8])
    suffix = '' if len(files) <= 8 else f', +{len(files) - 8} more'
    raise RuntimeError(
        'storage cutover refused: direct database ownership remains in '
        f"{report['file_count']} production files "
        f"({report['violation_count']} capabilities): {preview}{suffix}")


__all__ = [
    'BoundaryViolation', 'boundary_report', 'cutover_report',
    'require_exclusive_sidecar_boundary', 'scan_storage_boundary',
    'strict_inventory',
]
