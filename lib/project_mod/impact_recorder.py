"""Pytest plugin: record what each test file's run executed, read and listed.

Responsibility
    While tests run, record per test file: the repository code units that
    executed (function and method bodies by qualified name; module and class
    bodies as ``<module>``), the repository files opened by anything but the
    import system, the repository directories listed, whether it started a
    program whose reads are unobserved (anything but git run outside the
    repository) or forked, its collected and executed node ids and its failing
    node ids. Functions that run while repository modules are being imported
    are recorded per importing module: they build state (registries, tables)
    that every test using that module observes, whichever test file happened
    to trigger the import in a process. Landing verification turns the
    observations into content-addressed records
    (``lib/project_mod/impact_selection.py``) and later runs only the test
    files whose recorded dependencies changed.

How
    ``sys.monitoring`` (Python 3.12+) reports each code object's first
    execution; the callback returns ``DISABLE``, so recording costs one
    callback per function per test file. ``restart_events()`` re-arms the
    callbacks when the run moves to another test file and at every fixture
    setup. Fixture setup is attributed to the fixture, and a test file inherits
    the fixtures its tests use, so a session fixture set up once still counts
    for every file using it. While a test module is collected, other modules'
    bodies are not attributed to it (they run once per process, for whichever
    file imports them first). Calls made during an import stay armed, so every
    import sees the functions it runs. Reads and directory listings made by
    the import system are not recorded.

Use
    Inert unless ``TOFU_TEST_IMPACT_OUT`` names a directory;
    ``TOFU_TEST_IMPACT_ROOT`` is the checkout whose files are recorded.
    Verification copies this file next to the run (the repository under test
    need not contain it), prepends that directory to ``PYTHONPATH`` and passes
    ``-p tofu_test_impact_plugin``. Each process writes one JSON-lines file at
    session end; a process that dies writes nothing, and the files it ran stay
    unrecorded because their executed node ids are incomplete.

Dependencies: Python standard library and pytest only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

import pytest

_OUT = os.environ.get('TOFU_TEST_IMPACT_OUT', '')
_ROOT = os.path.realpath(os.environ.get('TOFU_TEST_IMPACT_ROOT') or os.getcwd())
_IGNORED_PREFIXES = ('.venv/', 'node_modules/', '.git/')
_SPAWN_EVENTS = frozenset({'subprocess.Popen', 'os.posix_spawn', 'os.exec', 'os.system',
                           'os.fork', 'os.forkpty', 'os.spawn'})
_LISTING_EVENTS = frozenset({'os.listdir', 'os.scandir'})
# sys.monitoring reserves 0, 1, 2 and 5 for debuggers, coverage, profilers and
# optimizers; 3 and 4 are free for other tools.
_TOOL_IDS = (3, 4)
_CO_OPTIMIZED = 0x0001  # set on function code objects, unset on module and class bodies
MODULE_UNIT = '<module>'


class _Bucket:
    __slots__ = ('units', 'data', 'dirs', 'opaque', 'failed', 'fixtures', 'collected', 'executed')

    def __init__(self):
        self.units: dict[str, set[str]] = {}
        self.data: set[str] = set()
        self.dirs: set[str] = set()
        self.opaque = False
        self.failed: set[str] = set()
        self.fixtures: set[str] = set()
        self.collected: set[str] = set()
        self.executed: set[str] = set()


_files: dict[str, _Bucket] = {}
_fixtures: dict[str, _Bucket] = {}
# importing module -> {file: function units executed while that module was being imported}
_import_time: dict[str, dict[str, set[str]]] = {}
# (bucket, path of the test module being collected or None)
_stack: list[tuple[_Bucket, str | None]] = []
_paths: dict = {}
_state: dict = {'recording': False, 'test': None, 'current': None}


def _relative(path) -> str | None:
    try:
        return _paths[path]
    except (KeyError, TypeError):
        pass
    relative = None
    if isinstance(path, str) and path and not path.startswith('<'):
        absolute = os.path.realpath(os.path.abspath(path))
        if absolute == _ROOT:
            relative = ''
        elif absolute.startswith(_ROOT + os.sep):
            candidate = absolute[len(_ROOT) + 1:]
            if not candidate.startswith(_IGNORED_PREFIXES) and '__pycache__' not in candidate:
                relative = candidate
    _paths[path] = relative
    return relative


def _importing_modules(frame) -> list[str]:
    """Repository modules whose bodies are executing at or below FRAME (imports in progress)."""
    modules = []
    while frame is not None:
        code = frame.f_code
        if code.co_name == '<module>':
            relative = _relative(code.co_filename)
            if relative:
                modules.append(relative)
        frame = frame.f_back
    return modules


def _on_start(code, _offset):
    # Runs inside the code under test: an exception here would surface there.
    try:
        relative = _relative(code.co_filename)
        if not relative:
            return sys.monitoring.DISABLE
        body = not code.co_flags & _CO_OPTIMIZED
        unit = MODULE_UNIT if body else code.co_qualname
        if _stack:
            bucket, collecting = _stack[-1]
            if not (body and collecting is not None and relative != collecting):
                bucket.units.setdefault(relative, set()).add(unit)
        if not body:
            importing = _importing_modules(sys._getframe(1))
            if importing:
                for module in importing:
                    _import_time.setdefault(module, {}).setdefault(relative, set()).add(unit)
                return None  # stays armed: every import must see the functions it runs
    except Exception:  # noqa: BLE001
        pass
    return sys.monitoring.DISABLE


def _by_import_system() -> bool:
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    filename = frame.f_code.co_filename if frame is not None else ''
    return filename.startswith('<frozen importlib') or (
        'importlib' + os.sep + '_bootstrap' in filename)


def _tokens(argv) -> list[str]:
    if isinstance(argv, (str, bytes)):
        return os.fsdecode(argv).split()
    return [os.fsdecode(token) for token in argv]


def _starts_unobserved_reader(event: str, args) -> bool:
    """Whether the event starts work whose code and reads this process cannot observe.

    Git run outside the repository (test fixtures build their own repositories)
    is transparent; git reading this repository, any other program and a fork
    are not.
    """
    if event == 'subprocess.Popen':
        executable, argv, cwd = args[0], args[1], args[2]
    elif event in ('os.posix_spawn', 'os.exec'):
        executable, argv, cwd = args[0], args[1], None
    elif event == 'os.spawn':
        executable, argv, cwd = args[1], args[2], None
    else:
        return True  # os.system, os.fork, os.forkpty
    tokens = _tokens(argv)
    program = os.fsdecode(executable) if executable else (tokens[0] if tokens else '')
    if os.path.basename(program) != 'git':
        return True
    directory = os.fsdecode(cwd) if cwd else os.getcwd()
    for flag, value in zip(tokens, tokens[1:]):
        if flag == '-C':
            directory = os.path.join(directory, value)
    return _relative(directory) is not None


def _audit(event, args):
    # An exception raised by an audit hook aborts the audited operation.
    try:
        if _stack:
            _observe(_stack[-1][0], event, args)
    except Exception:  # noqa: BLE001
        pass


def _observe(bucket: _Bucket, event, args) -> None:
    if event == 'open':
        path = args[0] if args else None
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str):
            relative = _relative(path)
            if relative and not _by_import_system():
                bucket.data.add(relative)
    elif event in _LISTING_EVENTS:
        path = args[0] if args else '.'
        if path is None:
            path = '.'
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str):
            relative = _relative(path)
            if relative is not None and not _by_import_system():
                bucket.dirs.add(relative)
    elif event in _SPAWN_EVENTS and _starts_unobserved_reader(event, args):
        bucket.opaque = True


def _file(nodeid: str) -> _Bucket:
    return _files.setdefault(nodeid.split('::', 1)[0], _Bucket())


def _fixture_key(fixturedef) -> str:
    return f'{fixturedef.baseid}::{fixturedef.argname}'


def pytest_configure(config):
    if not _OUT or _state['recording'] or not hasattr(sys, 'monitoring'):
        return
    monitoring = sys.monitoring
    tool = next((tool for tool in _TOOL_IDS if monitoring.get_tool(tool) is None), None)
    if tool is None:
        return
    monitoring.use_tool_id(tool, 'tofu-test-impact')
    monitoring.register_callback(tool, monitoring.events.PY_START, _on_start)
    monitoring.set_events(tool, monitoring.events.PY_START)
    sys.addaudithook(_audit)
    _state['recording'] = True


def pytest_collectstart(collector):
    if _state['recording'] and isinstance(collector, pytest.Module):
        path = collector.nodeid.split('::', 1)[0]
        _stack.append((_file(path), path))


def pytest_collectreport(report):
    if not _state['recording']:
        return
    if _stack and _stack[-1][1] is not None and report.nodeid == _stack[-1][1]:
        _stack.pop()
    if report.failed:
        _file(report.nodeid).failed.add(report.nodeid)


def pytest_collection_finish(session):
    if _state['recording']:
        for item in session.items:
            _file(item.nodeid).collected.add(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _state['recording']:
        yield
        return
    bucket = _file(item.nodeid)
    bucket.executed.add(item.nodeid)
    definitions = getattr(getattr(item, '_fixtureinfo', None), 'name2fixturedefs', {}) or {}
    for name in getattr(item, 'fixturenames', ()):
        chain = definitions.get(name) or ()
        if chain:
            bucket.fixtures.add(_fixture_key(chain[-1]))
    if _state['current'] is not bucket:
        sys.monitoring.restart_events()  # this file has not seen code other files disabled
        _state['current'] = bucket
    _stack.append((bucket, None))
    _state['test'] = bucket
    try:
        yield
    finally:
        _state['test'] = None
        if _stack and _stack[-1][0] is bucket:
            _stack.pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if not _state['recording']:
        yield
        return
    key = _fixture_key(fixturedef)
    bucket = _fixtures.setdefault(key, _Bucket())
    if _state['test'] is not None:
        _state['test'].fixtures.add(key)  # fixtures requested dynamically count too
    sys.monitoring.restart_events()
    _stack.append((bucket, None))
    try:
        yield
    finally:
        if _stack and _stack[-1][0] is bucket:
            _stack.pop()


def pytest_runtest_logreport(report):
    if _state['recording'] and report.failed:
        _file(report.nodeid).failed.add(report.nodeid)


def _serialize(bucket: _Bucket) -> dict:
    return {'units': {path: sorted(names) for path, names in bucket.units.items()},
            'data': sorted(bucket.data), 'dirs': sorted(bucket.dirs), 'opaque': bucket.opaque}


def pytest_sessionfinish(session, exitstatus):
    if not _OUT or not _state['recording']:
        return
    lines = [{'kind': 'file', 'test': test, **_serialize(bucket),
              'failed': sorted(bucket.failed), 'fixtures': sorted(bucket.fixtures),
              'collected': sorted(bucket.collected), 'executed': sorted(bucket.executed)}
             for test, bucket in _files.items()]
    lines += [{'kind': 'fixture', 'key': key, **_serialize(bucket)}
              for key, bucket in _fixtures.items()]
    lines += [{'kind': 'import-time', 'module': module,
               'units': {path: sorted(names) for path, names in executed.items()}}
              for module, executed in _import_time.items()]
    os.makedirs(_OUT, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=_OUT, prefix='.partial-')
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + '\n')
    os.replace(temporary, os.path.join(_OUT, f'{os.getpid()}-{uuid.uuid4().hex}.jsonl'))
