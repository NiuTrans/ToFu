"""Landing verification: a merged result must not newly fail tests that pass on trunk.

Responsibility
    The CI candidate check (``check``) and the linear checkpoint's gate
    attribution (``lib/linear_git_checkpoint.py``) ask this module whether a
    merged tree newly fails tests. ``scripts/git_land.py`` runs no tests (owner
    decision 2026-11-08); it only merges in ``merge_checkout``. Only the test
    files the merged tree can affect run on it. A test file with a recorded run (``impact_selection.py``) runs when
    code it executed, a file it read or a directory it listed changed, or when
    it failed and the current branch differs from its recorded run. A test
    file without a record runs when the repository's static selector
    (``scripts/test_select.py``) chooses it for the change. Every run is
    recorded, so the next verification runs fewer. A test that fails on the
    merged tree, passes on the current branch tip and fails again when rerun is
    a regression (``regressed``). An explicit recorded failure can
    identify a preexisting failure when its inputs still match the tip.
    A record without that failure never proves the case currently passes.
    Tests failing at the tip as
    well, and failures that do not reproduce, are reported and never block.
    Missing evidence (no selector, a runner that cannot start on either side)
    is reported as unavailable and does not block: only a reproduced
    regression is attributable to a merge.

Entry points
    ``merge_checkout(top, head)``: the private checkout git_land merges in and
        recorded runs test in, the warm one (Python bytecode kept) when no
        other user holds it, else a temporary one, so no user waits.
    ``verify_merge(top, head, source, merged_root, tree)`` -> ``Verdict``;
        ``execute=False`` stops after selection and returns ``planned`` with
        the test files verification would run.
    ``record_tests(top, paths)``, also ``python3 -m
        lib.project_mod.landing_verification record [PATH ...]``: record runs at
        the current branch tip ahead of later verification.
    ``record_pending(top)`` (``record --pending``): the resumable form — only
        files whose evidence is missing or stale run, one top-level scope at a
        time, and each scope is saved before the next starts, so an
        interrupted warm-up no longer discards hours of records.
    ``warm_checkout_path(top)``.
    ``verification_checkout(top, revision)``: a private checkout of REVISION
        sharing the receiving checkout's installed dependencies; the linear
        checkpoint's gate runs in it (``lib/linear_git_checkpoint.py``).

Dependencies: the git executable, the merged tree's ``scripts/test_select.py``
(test selection and the inner-loop pytest command), the receiving checkout's
project interpreter (``.venv/bin/python3``), ``impact_selection`` and ``lanes``
(the private local root holding the warm checkout and the records).
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from lib.project_mod import impact_selection
from lib.project_mod.process_group import escaped_descendant_groups, retire_process_group

_SUMMARY_HEADER = re.compile(r'^=+ short test summary info =+$')
_SUMMARY_LINE = re.compile(r'^(?:FAILED|ERROR) (\S.*)$')
_EVIDENCE_LINES = 60
_PLUGIN_MODULE = 'tofu_test_impact_plugin'
_RUN_FLAGS = ('-rfE', '--continue-on-collection-errors')
# Only these are inputs to a test run: who the user is, which host it runs on,
# and the scenario controls a test or a landing sets for itself. The launcher's
# application configuration, credentials and shell metadata are not inputs —
# they were what split the evidence into one partition per conversation.
_INHERITED = ('HOME', 'USER', 'LOGNAME', 'HOSTNAME', 'TOFU_TEST_TOFUDB_BIN',
              'TOFU_STORAGE_TEST_POSTGRES', 'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',
              'TOFU_PERSONAL_STORAGE_BACKEND', 'TOFU_CHECKPOINT_SQL_TEST_LIBRARY',
              'TOFU_CHECKPOINT_SQL_TEST_SHA256', 'TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
_SCENARIO_PREFIXES = ('LANDING_', 'EXERCISE_', 'TOFU_TEST_SCENARIO_')
# Ignored state the warm checkout keeps between landings: caches their tools
# validate against sources, and the dependency links.
_WARM_KEEP = ('__pycache__', '/.venv', '/node_modules')


@dataclasses.dataclass(frozen=True)
class Verdict:
    status: str  # skipped | passed | regressed | unavailable | planned
    reason: str = ''
    head: str = ''
    trunk_commits: int = 0
    selected: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    preexisting: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    evidence: str = ''
    skipped_files: int = 0
    unrecorded_files: int = 0
    seconds: float = 0.0
    recorded: int = 0
    record_error: str = ''


@dataclasses.dataclass(frozen=True)
class _Run:
    usable: bool
    failed: tuple[str, ...]
    output: str
    observations: tuple = ()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=str(root),
                          capture_output=True, text=True, env=env)


def _names(result: subprocess.CompletedProcess) -> list[str]:
    return [name for name in result.stdout.split('\0') if name]


def _load_selector(root: Path):
    script = root / 'scripts' / 'test_select.py'
    if not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f'landing_test_select_{uuid.uuid4().hex}', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refs(record) -> set[str]:
    if isinstance(record, dict):
        return set(record.get('refs') or ())
    return set(record or ())


def _project_interpreter(top: Path) -> str:
    candidate = top / '.venv' / 'bin' / 'python3'
    return str(candidate) if os.access(candidate, os.X_OK) else sys.executable


def _bridge(top: Path, root: Path) -> None:
    """Link the receiving checkout's installed dependencies, as worktrees do."""
    for name in ('node_modules', '.venv'):
        source, target = top / name, root / name
        if source.exists() and not os.path.lexists(target):
            os.symlink(source, target)


def _failed_ids(output: str, observations=()) -> tuple[str, ...]:
    failed, in_summary = [], False
    for line in output.splitlines():
        if _SUMMARY_HEADER.match(line):
            in_summary = True
            continue
        if in_summary:
            match = _SUMMARY_LINE.match(line)
            if match:
                failed.append(match.group(1).split(' - ', 1)[0].strip())
    recorded = {case for line in observations
                if line.get('kind') in ('file', 'selection')
                for case in line.get('failed') or ()}
    collected, executed = _execution_coverage(observations)
    known = recorded | collected | executed
    if known:
        # Background output can splice into pytest's human summary. Receipts
        # retain exact identities; console-only worker failures still count
        # when their identity belongs to this collection.
        failed = [case for case in failed if case in known]
    return tuple(dict.fromkeys([*sorted(recorded), *failed]))


@contextlib.contextmanager
def _termination_unwinds():
    """While tests run, SIGTERM unwinds the landing instead of orphaning them."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    def _unwind(signum, _frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, _unwind)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run(python: str, root: Path, selector, tests: list[str], log: Path) -> _Run:
    """Run TESTS in ROOT with the impact recorder; its observations ride on the result."""
    recorder = log.with_suffix('.impact')
    plugin_dir = recorder / 'plugin'
    plugin_dir.mkdir(parents=True)
    manifest = _git(root, 'ls-files', '--cached', '-z')
    paths = _names(manifest) if manifest.returncode == 0 else None
    (plugin_dir / f'{_PLUGIN_MODULE}.py').write_text(impact_selection.plugin_source(paths),
                                                   encoding='utf-8')
    argv = [python, *selector.build_pytest_command(list(tests), None)[1:],
            *_RUN_FLAGS, '-p', _PLUGIN_MODULE]
    env = _test_environment()
    env.update(PYTHONPATH=os.pathsep.join([str(plugin_dir), str(root)]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               TOFU_TEST_IMPACT_OUT=str(recorder / 'out'), TOFU_TEST_IMPACT_ROOT=str(root))
    with open(log, 'w', encoding='utf-8') as handle:
        print(f'[verification] {len(tests)} selection(s) in {root}; log: {log}',
              file=sys.stderr, flush=True)
        # The test run owns its session, so an interrupted landing retires all of it.
        process = subprocess.Popen(argv, cwd=str(root), env=env, stdout=handle,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            with _termination_unwinds():
                returncode = process.wait()
        except BaseException:
            retire_process_group(
                process.pid, wait_leader=lambda seconds: process.wait(seconds),
                escaped_groups=escaped_descendant_groups(process.pid, process.pid))
            raise
    output = log.read_text(encoding='utf-8', errors='replace')
    observations = tuple(_observations(recorder / 'out'))
    failed = _failed_ids(output, observations)
    errors = sorted({line['observation_error'] for line in observations
                     if line.get('observation_error')})
    if errors:
        output += '\nDependency observation unavailable: ' + ', '.join(errors) + '\n'
    collected, executed = _execution_coverage(observations)
    requested = {test for test in tests if '::' in test}
    missing = requested - (collected & executed)
    if missing:
        output += '\nRequested cases lack collection/execution receipts:\n' + '\n'.join(sorted(missing))
        return _Run(False, failed, output, observations)
    if returncode in (0, 5):
        return _Run(True, (), output, observations)
    if returncode == 1 and failed:
        return _Run(True, failed, output, observations)
    return _Run(False, failed, output, observations)


def _execution_coverage(observations) -> tuple[set[str], set[str]]:
    """Union worker receipts without treating a partial selection as a whole-file run."""
    collected, executed = set(), set()
    for line in observations:
        if line.get('kind') in ('file', 'selection'):
            collected.update(line.get('collected') or ())
            executed.update(line.get('executed') or ())
    return collected, executed


def _check_snapshot(root: Path, tree: str, run: _Run) -> _Run:
    """A run that rewrites or adds source cannot attest to its input snapshot."""
    difference = _git(root, 'diff', '--quiet', '--no-ext-diff', '--no-textconv', tree, '--')
    untracked = _git(root, 'ls-files', '--others', '--exclude-standard',
                     '--exclude=__pycache__/', '-z')
    if difference.returncode == 0 and untracked.returncode == 0 and not untracked.stdout:
        return run
    collected, executed = _execution_coverage(run.observations)
    receipts = ({'kind': 'selection', 'collected': sorted(collected), 'executed': sorted(executed)},)
    return _Run(False, run.failed, run.output + '\nVerification snapshot changed or could not '
                'be checked after execution; no dependency evidence was published.\n', receipts)


def _observations(directory: Path) -> list[dict]:
    lines = []
    for path in sorted(directory.glob('*.jsonl')) if directory.is_dir() else ():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                lines.append(json.loads(line))
            except ValueError:
                continue
    return lines


def _tail(output: str) -> str:
    return '\n'.join(output.splitlines()[-_EVIDENCE_LINES:])


@contextlib.contextmanager
def verification_checkout(top, revision: str):
    """A detached private checkout of REVISION with the receiving checkout's dependencies."""
    top = Path(top)
    with _checkout(top, revision) as root:
        _bridge(top, root)
        yield root


@contextlib.contextmanager
def _checkout(top: Path, revision: str):
    with tempfile.TemporaryDirectory(prefix='git-land-tip-') as parent:
        root = Path(parent) / 'tip'
        added = _git(top, 'worktree', 'add', '--detach', str(root), revision)
        if added.returncode:
            raise OSError(f'git worktree add failed: {added.stderr.strip()[:400]}')
        try:
            yield root
        finally:
            _git(top, 'worktree', 'remove', '--force', str(root))


def _tip_failures(top: Path, head: str, python: str, selector, merged: _Run,
                  present: list[str], logs: Path, common: dict) -> Verdict | set[str]:
    """The tests failing at the current branch tip, or the verdict the tip decides."""
    with _warm_slot(top, head, 'tip-checkout') as tip:
        if not merged.usable:
            at_tip = _check_snapshot(tip, head,
                _run(python, tip, selector, list(common['selected']), logs / 'tip.log'))
            if at_tip.usable:
                return Verdict('regressed', 'the selected tests cannot run on the merged '
                               'result but run on the current branch tip',
                               evidence=_tail(merged.output), **common)
            return Verdict('unavailable', 'the selected tests cannot run on the merged '
                           'result or on the current branch tip',
                           evidence=_tail(merged.output), **common)
        at_tip = _check_snapshot(tip, head, _run(python, tip, selector, present, logs / 'tip.log'))
        if not at_tip.usable and any('::' in test for test in present):
            # A case may be new/renamed even when its file existed at the tip.
            # Collect the existing files before deciding this is a new failure;
            # pytest's empty/partial collection is never proof that a case passed.
            files = sorted({test.split('::', 1)[0] for test in present})
            at_tip = _check_snapshot(tip, head, _run(python, tip, selector, files, logs / 'tip-files.log'))
        if not at_tip.usable:
            return Verdict('unavailable', 'the failing tests cannot run on the current '
                           'branch tip for comparison',
                           evidence=_tail(at_tip.output), **common)
        return set(at_tip.failed)


# ─── Warm checkout and records ─────────────────────────────────────────────

def _cache_dir(top: Path) -> Path:
    """The private directory for the warm checkout and test records (reconstructible).

    On the repository's fast local root when placement selects one, else in
    its Git directory, never in a checkout someone works in.
    """
    from lib.project_mod import lanes
    repo = lanes.repository(top)
    local = lanes.local_root(repo)
    directory = local / '.verification' if local else repo.common / 'tofu-verification'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def warm_checkout_path(top) -> Path:
    return _cache_dir(Path(top)) / 'checkout'


def _test_environment() -> dict[str, str]:
    """The test environment and the evidence-key input are one fixed description.

    Verification is launched by a conversation's ``make test`` or ``record``,
    the CI candidate check in the execution host, or another agent's tool call,
    and each launcher contributes its own credentials, routing, shell and
    session variables. Inheriting them partitioned the records by launcher, so
    a run recorded by one launcher could never answer another. Only the user's
    identity, the declared test fixtures, scenario controls and the host's tools
    are inherited; the launcher's environment is the caller's business, never
    the suite's input. Tests pin
    their own runtime configuration in ``tests/conftest.py``, whose code is a
    dependency of every record, and _run adds the recorder's variables.
    """
    interpreter = os.path.realpath(sys.executable)
    bins = [os.path.dirname(os.path.abspath(sys.executable)), os.path.dirname(interpreter)]
    home = os.environ.get('HOME') or os.path.expanduser('~')
    from lib.tools.process_environment import apply_tool_paths
    env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    env.update({name: value for name, value in os.environ.items()
                if name.startswith(_SCENARIO_PREFIXES)})
    env['PATH'] = os.pathsep.join([*bins, '/usr/local/sbin', '/usr/local/bin',
                                   '/usr/sbin', '/usr/bin', '/sbin', '/bin'])
    apply_tool_paths(env, cache_root=os.path.join(home, '.cache'))
    env.update(LANG='C.UTF-8', LC_ALL='C.UTF-8', TZ='UTC', VIRTUAL_ENV=os.path.dirname(bins[0]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    return env


def _environment_key(python: str) -> str:
    """Bind records to the interpreter, distributions and observation semantics."""
    probe = ('import importlib.metadata as m, sys\nprint(sys.version)\n'
             'print(sorted(f"{meta[\'Name\']}=={meta[\'Version\']}" '
             'for meta in (d.metadata for d in m.distributions())))')
    listed = subprocess.run([python, '-I', '-c', probe], capture_output=True, text=True)
    if listed.returncode:
        raise OSError('the test interpreter could not identify its installed distributions')
    context = json.dumps(_test_environment(), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(f'{os.path.realpath(python)}\n{listed.returncode}\n'
                          f'{listed.stdout}\n{context}\n{_record_semantics()}\n'
                          f'{impact_selection.plugin_source()}'.encode('utf-8')).hexdigest()[:20]


def _record_semantics() -> str:
    """Hash the code that decides what a record means, not the orchestration around it.

    An edit to selection, retries or the warm checkout cannot change how an
    observed run became a dependency set, so it must not throw away the
    records every other verification is reusing. The recording owner is hashed in
    full; here only the dependency-building helpers, the runner's own flags
    and the plugin's module name participate. Anything that starts producing
    records elsewhere in this module has to join that list (a test pins the
    call sites of ``records_from_observations``).
    """
    tree = ast.parse(Path(__file__).read_text(encoding='utf-8'))
    names = {'_refs', '_global_inputs', '_remember'}
    parts = [ast.dump(node) for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    parts.append(repr((_RUN_FLAGS, _PLUGIN_MODULE)))
    parts.append(Path(impact_selection.__file__).read_text(encoding='utf-8'))
    return '\n'.join(parts)


def _store(top: Path, python: str, cache: Path | None = None) -> impact_selection.ImpactStore | None:
    try:
        return impact_selection.ImpactStore(cache if cache is not None else _cache_dir(top),
                                             _environment_key(python))
    except (OSError, impact_selection.ImpactCacheError):
        return None  # records are a cache: without them every file is selected statically


def _records(store) -> dict:
    try:
        return store.records() if store is not None else {}
    except impact_selection.ImpactCacheError:
        return {}


def invalidate_bytecode(root: Path, revision: str, previous: str | None = None) -> None:
    """Drop only changed source caches before reusing a private checkout.

    Timestamp pycs accept equal source size and integer-second mtime; different
    Git blobs can satisfy both. Compare content before replacing sources, or
    compare the before/after trees when a merge has already materialized them.
    """
    revisions = (previous, revision) if previous else (revision,)
    changed = _git(root, 'diff', '--name-only', '--no-renames', '-z', *revisions, '--', '*.py')
    if changed.returncode:
        raise OSError(f'cannot validate warm bytecode: {changed.stderr.strip()}')
    for name in _names(changed):
        source = root / name
        for cache in (source.parent / '__pycache__').glob('*.pyc'):
            if cache.name.startswith(source.stem + '.'):
                cache.unlink(missing_ok=True)
        source.with_suffix('.pyc').unlink(missing_ok=True)


def _reset_warm(top: Path, warm: Path, head: str) -> bool:
    if not (warm / '.git').is_file():
        return False
    common = _git(warm, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    expected = _git(top, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if common.returncode or common.stdout.strip() != expected.stdout.strip():
        return False
    invalidate_bytecode(warm, head)
    # Exactly a fresh checkout's contents, plus the caches named in _WARM_KEEP.
    return (_git(warm, 'reset', '-q', '--hard', head).returncode == 0
            and _git(warm, 'clean', '-ffdxq',
                     *(f'--exclude={pattern}' for pattern in _WARM_KEEP)).returncode == 0)


@contextlib.contextmanager
def _warm_slot(top: Path, head: str, name: str, cache: Path | None = None):
    """A warm private checkout at HEAD, or a temporary one when its slot is busy.

    Two slots exist (merge and tip) so a failure comparison never evicts the
    merge checkout's bytecode; a busy slot never makes a landing wait.
    """
    top = Path(top)
    cache = cache if cache is not None else _cache_dir(top)
    lock = open(cache / f'{name}.lock', 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        with _checkout(top, head) as scratch:
            _bridge(top, scratch)
            yield scratch
        return
    try:
        warm = cache / name
        if not _reset_warm(top, warm, head):
            _git(top, 'worktree', 'remove', '--force', str(warm))
            shutil.rmtree(warm, ignore_errors=True)
            added = _git(top, 'worktree', 'add', '--force', '--detach', str(warm), head)
            if added.returncode:
                raise OSError(f'git worktree add failed: {added.stderr.strip()[:400]}')
        _bridge(top, warm)
        yield warm
    finally:
        lock.close()


@contextlib.contextmanager
def merge_checkout(top, head: str, *, cache: Path | None = None):
    """A private checkout at HEAD to merge and verify in; the warm one when it is free.

    The warm checkout keeps Python bytecode between landings, so only changed
    files are rewritten and recompiled (cold collection of 30 test files took
    127 s, warm 30 s on 2026-11-07). A landing that finds it in use gets a
    temporary checkout instead of waiting.
    """
    with _warm_slot(top, head, 'checkout', cache) as warm:
        yield warm


def _global_inputs(selector) -> dict[str, str]:
    """The selector's blast-radius files and their scopes (``ALL`` or a basename prefix)."""
    inputs = dict(getattr(selector, 'BLAST_RADIUS', None) or {})
    inputs[getattr(selector, 'EXECUTION_POLICY', 'scripts/test_select.py')] = 'ALL'
    return inputs


def _remember(store, run: _Run, tests: list[str], tree: impact_selection.Tree, tree_id: str,
              index: dict, global_inputs: dict[str, str]) -> tuple[list, str]:
    """Record a run of whole test files, and why nothing was saved when nothing was.

    Files without complete observations stay unrecorded.
    """
    if store is None:
        return [], 'the record store could not be opened'
    refs = {test: _refs(index.get(test)) for test in tests}
    records = impact_selection.records_from_observations(list(run.observations), tests, tree,
                                                         tree_id, refs, global_inputs)
    try:
        store.save(records, replace_tests=tests)
    except impact_selection.ImpactCacheError as error:
        return [], f'saving the records failed: {error}'
    return records, '' if run.observations else 'the test run produced no recorder output'


def _static_selection(selector, index: dict, changed: list[str], global_inputs: dict[str, str],
                      tip, merged, store, top: Path) -> dict[str, str]:
    """The selector's reasons for test files that have no record.

    A conftest or blast-radius Python file whose code did not change (only
    comments or layout) selects only the test files that reference it instead
    of the whole family the selector escalates to: escalation stands for
    "this may change any test's behavior", and unchanged code cannot.
    """
    neutral = {path for path in changed
               if path.endswith('.py')
               and (path in global_inputs or os.path.basename(path) == 'conftest.py')
               and impact_selection.same_code(top, tip.blobs.get(path), merged.blobs.get(path),
                                              store)}
    execution = getattr(selector, 'EXECUTION_POLICY', None)
    if (execution in changed and execution in merged.blobs and execution not in tip.blobs
            and 'scripts/test_select.py' in tip.blobs):
        from lib.project_mod.verification_policy import same_extracted_execution
        legacy = _git(top, 'cat-file', 'blob', tip.blobs['scripts/test_select.py']).stdout
        standalone = _git(top, 'cat-file', 'blob', merged.blobs[execution]).stdout
        if same_extracted_execution(legacy, standalone):
            neutral.add(execution)
    inputs = [path for path in changed if path not in neutral]
    reasons = dict(selector.select_tests(index, inputs)[1])
    expand = getattr(selector, 'expand_dependencies', None)
    if expand is not None and len(reasons) != len(index):
        expanded = expand(top, merged, inputs, _cache_dir(top) / 'source-dependencies-v1.json')
        # An importer is affected, not edited. Only actual input changes activate
        # shared-fixture/execution blast rules; fixture/import edges select consumers.
        for test in index.keys() & set(expanded):
            reasons.setdefault(test, 'transitive')
    for test, entry in index.items():
        if test not in reasons and (test in neutral or neutral & _refs(entry)):
            reasons[test] = 'direct'
    return reasons


def _index(selector, top: Path, checkout: Path, tree: str | None = None,
           paths: list[str] | None = None, cache: Path | None = None) -> dict:
    """Share immutable source indexes across runs; explicit scopes parse only their files."""
    from lib.project_mod.verification_index import UnsupportedIndex, build_index, select_paths
    revision = tree or _git(checkout, 'rev-parse', 'HEAD').stdout.strip()
    try:
        directory = cache if cache is not None else _cache_dir(top)
        result = build_index(top, revision, selector, directory / 'source-index-v1.json', paths)
        if result.cache_error:
            print(f'[verification] source-index cache unavailable: {result.cache_error}',
                  file=sys.stderr, flush=True)
        return result.index
    except UnsupportedIndex:
        # Other repositories may define a selector without a pure extractor or
        # use symlinked tests. Their selector still owns discovery and policy.
        index = selector.build_index(str(checkout / 'tests'), None)
        return {test: index[test] for test in select_paths(index, paths)}


# ─── Verification ──────────────────────────────────────────────────────────

def verify_merge(top, head: str, source: str, merged_root, tree: str | None = None, *,
                 execute: bool = True) -> Verdict:
    """Run the test files the merged tree can affect on MERGED_ROOT and attribute failures.

    Without EXECUTE the same selection is made and returned as ``planned``;
    no test runs and no record is written.
    """
    started = time.monotonic()
    top, merged_root = Path(top), Path(merged_root)
    tree = tree or _git(merged_root, 'write-tree').stdout.strip()
    base = _git(top, 'merge-base', head, source).stdout.strip()
    count = _git(top, 'rev-list', '--count', f'{base}..{head}').stdout.strip() if base else ''
    common = {'head': head, 'trunk_commits': int(count) if count.isdigit() else 0}
    changed = _names(_git(top, 'diff', '--name-only', '--no-renames', '-z', head, tree))
    if not changed:
        return Verdict('skipped', 'the merged result equals the current branch tip', **common)
    try:
        selector = _load_selector(merged_root)
    except (OSError, SyntaxError, ImportError) as error:
        return Verdict('unavailable', f'the test selector could not load: {error}', **common)
    if selector is None:
        return Verdict('skipped', 'the repository declares no test selector '
                       '(scripts/test_select.py)', **common)
    python = _project_interpreter(top)
    store = _store(top, python)
    try:
        verdict = _verify(top, head, merged_root, tree, changed, selector, store, python, common,
                          execute)
    finally:
        if store is not None:
            store.close()
    return dataclasses.replace(verdict, seconds=time.monotonic() - started)


def _verify(top: Path, head: str, merged_root: Path, tree: str, changed: list[str], selector,
            store, python: str, common: dict, execute: bool) -> Verdict:
    try:
        index = _index(selector, top, merged_root, tree)
        merged = impact_selection.Tree.of(top, tree)
        tip = impact_selection.Tree.of(top, head)
        records = _records(store)
        if top.resolve() == merged_root.resolve():
            # CI's live checkout can gain ignored directory entries without
            # changing its Git tree. Private, reset snapshots do not share that
            # mutable input: keep their directory-based reuse available.
            records = {test: dataclasses.replace(record, opaque=True) if record.dirs else record
                       for test, record in records.items()}
        global_inputs = _global_inputs(selector)
        # Blast-radius files are dependencies of every record they scope, so a
        # real change to one re-runs those files once and its records answer
        # every landing after it, including this landing's own re-verification
        # when the current branch moves during a long run.
        static = _static_selection(selector, index, changed, global_inputs, tip, merged,
                                   store, top)
        planned = impact_selection.plan(records, sorted(index), static, tip, merged, store, top)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError,
            impact_selection.ImpactCacheError) as error:
        return Verdict('unavailable', f'test selection failed: {error}', **common)
    selected = sorted(planned.run)
    common.update(selected=tuple(selected), skipped_files=planned.skipped,
                  unrecorded_files=sum(1 for test in selected if test not in records))
    if not selected:
        if planned.recorded:
            return Verdict('passed', 'no recorded test file used code or files the merge '
                           'changes', **common)
        return Verdict('skipped', "no test references the branch's changes", **common)
    if not execute:
        return Verdict('planned', 'the affected test files were selected, not run', **common)
    _bridge(top, merged_root)
    with tempfile.TemporaryDirectory(prefix='git-land-verify-') as logs:
        logs = Path(logs)
        run = _check_snapshot(merged_root, tree,
            _run(python, merged_root, selector, selected, logs / 'merged.log'))
        recorded, record_error = _remember(store, run, selected, merged, tree, index,
                                           global_inputs)
        verdict = _attribute(top, head, python, selector, run, merged_root, logs, common,
                             records, tip, store)
    return dataclasses.replace(verdict, recorded=len(recorded), record_error=record_error)


def _known_at_tip(failed: tuple[str, ...], records: dict, tip, store, top: Path):
    """Reuse explicit matching failures; absence needs a current control run."""
    known, unknown = {}, []
    for nodeid in failed:
        record = records.get(nodeid.split('::', 1)[0])
        if (record is not None and not record.opaque and nodeid in record.failed
                and not impact_selection.affected(record, tip, store, top)):
            known[nodeid] = 'failed'
        else:
            unknown.append(nodeid)
    return known, unknown


def _attribute(top: Path, head: str, python: str, selector, merged: _Run, merged_root: Path,
               logs: Path, common: dict, records: dict, tip, store) -> Verdict:
    """Classify the merged run's failures as preexisting, flaky or regressions."""
    if merged.usable and not merged.failed:
        return Verdict('passed', 'the affected test files pass on the merged result', **common)
    known, unknown = _known_at_tip(merged.failed, records, tip, store, top)
    failing_at_tip = {nodeid for nodeid, outcome in known.items() if outcome == 'failed'}
    # The merge already ran the complete affected files. Attribution needs
    # only its failing cases; exact execution receipts guard targeted rechecks.
    files = sorted({test.split('::', 1)[0] for test in unknown})
    # A test file the branch added does not exist at the tip: its failure
    # cannot be preexisting, so it needs no comparison run.
    present_files = {test for test in files if _git(top, 'cat-file', '-e', f'{head}:{test}'
                                                   ).returncode == 0}
    present = [test for test in unknown if test.split('::', 1)[0] in present_files]
    if not merged.usable or present:
        compared = _tip_failures(top, head, python, selector, merged, present, logs, common)
        if isinstance(compared, Verdict):
            return compared
        failing_at_tip |= compared
    preexisting = tuple(test for test in merged.failed if test in failing_at_tip)
    candidates = [test for test in merged.failed if test not in failing_at_tip]
    if not candidates:
        return Verdict('passed', 'only tests already failing on the current branch failed',
                       preexisting=preexisting, **common)
    tree = _git(merged_root, 'write-tree').stdout.strip()
    confirm = _check_snapshot(merged_root, tree,
        _run(python, merged_root, selector, candidates, logs / 'confirm.log'))
    reproduced = set(confirm.failed) & set(candidates)
    if not confirm.usable and not reproduced:
        return Verdict('unavailable', 'the failing tests could not be rerun on the merged '
                       'result', preexisting=preexisting,
                       evidence=_tail(confirm.output), **common)
    regressions = tuple(test for test in candidates if test in reproduced)
    flaky = tuple(test for test in candidates if test not in reproduced) if confirm.usable else ()
    if regressions:
        return Verdict('regressed', 'tests that pass on the current branch tip fail on the '
                       'merged result', regressions=regressions, preexisting=preexisting,
                       flaky=flaky, evidence=_tail(confirm.output), **common)
    return Verdict('passed', 'failures on the merged result did not reproduce',
                   preexisting=preexisting, flaky=flaky, **common)


# ─── Recording ahead of landings ───────────────────────────────────────────

def _record_batch(top: Path, head: str, python: str, selector, chosen: list[str],
                  checkout: Path, index: dict, store, logs: Path) -> dict:
    """Run and record one batch in CHECKOUT; the records are saved before the next."""
    tree_id = _git(top, 'rev-parse', f'{head}^{{tree}}').stdout.strip()
    tree = impact_selection.Tree.of(top, head)
    run = _check_snapshot(checkout, tree_id,
        _run(python, checkout, selector, chosen, logs))
    records, record_error = _remember(store, run, chosen, tree, tree_id, index,
                                      _global_inputs(selector))
    if not run.usable:
        record_error = record_error or 'the test runner did not complete:\n' + _tail(run.output)
    elif not _execution_coverage(run.observations)[1]:
        record_error = record_error or 'no selected test cases executed'
    elif len(records) != len(chosen):
        record_error = record_error or 'some selected files lack complete non-skipped evidence'
    if run.failed:
        # The private log is retired by the caller; keep its diagnostic in the
        # caller's output so a failure does not require a rerun.
        print(_tail(run.output), file=sys.stderr, flush=True)
    failed_files = {test.split('::', 1)[0] for test in run.failed}
    failed_files.update(record.test for record in records if record.outcome == 'failed')
    return {'tests': len(chosen), 'recorded': len(records), 'failed': len(failed_files),
            'failed_cases': run.failed, 'error': record_error}


def record_tests(top, paths: list[str] | None = None) -> dict:
    """Run the test files under PATHS (default: all) at the current branch tip and record them.

    Landings record what they run; this records ahead of them, so the first
    landing in an area already runs only what it affects.
    """
    top = Path(top)
    head = _git(top, 'rev-parse', 'HEAD').stdout.strip()
    started = time.monotonic()
    python = _project_interpreter(top)
    with merge_checkout(top, head) as checkout:
        selector = _load_selector(checkout)
        if selector is None:
            raise OSError('the repository declares no test selector (scripts/test_select.py)')
        try:
            index = _index(selector, top, checkout, paths=paths)
        except ValueError as error:
            return {'head': head, 'tests': 0, 'recorded': 0,
                    'failed': 0, 'seconds': 0.0,
                    'error': str(error)}
        store = _store(top, python)
        try:
            with tempfile.TemporaryDirectory(prefix='git-land-record-') as logs:
                result = _record_batch(top, head, python, selector, sorted(index), checkout,
                                       index, store, Path(logs) / 'record.log')
        finally:
            if store is not None:
                store.close()
    return {'head': head, **result, 'seconds': round(time.monotonic() - started, 1)}


def record_pending(top) -> dict:
    """Record only the test files whose current-tip evidence is missing or stale.

    A whole-suite recording takes hours on this host, and until now one
    interrupted run kept nothing, so the next attempt started cold again.
    Batches follow the suite's own top-level directories (a boundary the
    repository defines, not a tuning knob), each batch is saved before the
    next starts, and the next call skips files whose recorded inputs still
    match the tip. Repeating it converges instead of restarting.
    """
    top = Path(top)
    head = _git(top, 'rev-parse', 'HEAD').stdout.strip()
    started = time.monotonic()
    python = _project_interpreter(top)
    totals = {'head': head, 'tests': 0, 'recorded': 0, 'failed': 0, 'batches': 0,
              'already_fresh': 0, 'errors': []}
    with merge_checkout(top, head) as checkout:
        selector = _load_selector(checkout)
        if selector is None:
            raise OSError('the repository declares no test selector (scripts/test_select.py)')
        index = _index(selector, top, checkout)
        tip = impact_selection.Tree.of(top, head)
        store = _store(top, python)
        try:
            records = _records(store)
            fresh, pending = 0, []
            for test in sorted(index):
                record = records.get(test)
                if record is not None and not impact_selection.affected(record, tip, store, top):
                    fresh += 1
                else:
                    pending.append(test)
            totals['already_fresh'] = fresh
            print(f'[verification] recording {len(pending)} of {len(index)} test files; '
                  f'{fresh} already have current evidence', file=sys.stderr, flush=True)
            groups: dict[str, list[str]] = {}
            for test in pending:
                groups.setdefault(test.split('/')[1] if '/' in test else '', []).append(test)
            for group in sorted(groups):
                chosen = groups[group]
                with tempfile.TemporaryDirectory(prefix='git-land-record-') as logs:
                    result = _record_batch(top, head, python, selector, chosen, checkout, index,
                                           store, Path(logs) / 'record.log')
                totals['batches'] += 1
                for key in ('tests', 'recorded', 'failed'):
                    totals[key] += result[key]
                if result['error']:
                    totals['errors'].append({group: result['error']})
                print(f'[verification] recorded {group or "tests"}: '
                      f'{result["recorded"]}/{result["tests"]} '
                      f'({totals["recorded"]}/{len(pending)} so far)',
                      file=sys.stderr, flush=True)
        finally:
            if store is not None:
                store.close()
    totals['seconds'] = round(time.monotonic() - started, 1)
    totals['error'] = '; '.join(next(iter(item)) for item in totals['errors'])
    return totals


def check_revision(top, base: str, *, expected_revision: str | None = None) -> dict:
    """Check a clean, dedicated CI checkout, including its verified build artifacts."""
    top = Path(top)
    head = _git(top, 'rev-parse', '--verify', 'HEAD').stdout.strip()
    if not head or (expected_revision is not None and head != expected_revision):
        raise ValueError('the candidate revision differs from the CI scope receipt')
    reference = _git(top, 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}')
    if reference.returncode or not reference.stdout.strip():
        raise ValueError(f'invalid comparison base: {base}')
    ancestor = _git(top, 'merge-base', reference.stdout.strip(), head)
    if ancestor.returncode or not ancestor.stdout.strip():
        raise ValueError(f'comparison base has no common history: {base}')
    tree = _git(top, 'rev-parse', f'{head}^{{tree}}').stdout.strip()
    if not _check_snapshot(top, tree, _Run(True, (), '')).usable:
        raise ValueError('CI verification requires a clean committed checkout')
    if not (top / 'scripts/test_select.py').is_file():
        raise ValueError('CI verification requires a declared test selector')
    verdict = verify_merge(top, ancestor.stdout.strip(), head, top, tree)
    if (_git(top, 'rev-parse', 'HEAD').stdout.strip() != head
            or not _check_snapshot(top, tree, _Run(True, (), '')).usable):
        verdict = dataclasses.replace(verdict, status='unavailable',
                                      reason='the CI candidate changed during verification')
    return {'schema_version': 1, 'scope': 'incremental', 'revision': head, 'tree': tree,
            **dataclasses.asdict(verdict)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description='Check a committed candidate or record test runs for landing verification.')
    commands = parser.add_subparsers(dest='command', required=True)
    record = commands.add_parser('record', help='run and record test files')
    record.add_argument('paths', nargs='*', help='test files or directories (default: all)')
    record.add_argument('--into', default='.', help='any checkout of the repository')
    record.add_argument('--pending', action='store_true',
                        help='record only files whose evidence is missing or stale, in batches')
    check = commands.add_parser('check', help='verify the committed candidate against a base')
    check.add_argument('--base', required=True, help='comparison revision (its merge base is used)')
    check.add_argument('--expected-revision', help='exact candidate SHA from the CI scope receipt')
    check.add_argument('--into', default='.', help='checkout whose committed HEAD is verified')
    check.add_argument('--json', action='store_true', help='emit a revision-bound verification receipt')
    args = parser.parse_args(argv)
    try:
        checkout = _git(Path(args.into).expanduser().resolve(), 'rev-parse', '--show-toplevel')
    except OSError as error:
        parser.error(str(error))
    if checkout.returncode or not checkout.stdout.strip():
        parser.error(checkout.stderr.strip() or 'the selected path is not a Git checkout')
    root = Path(checkout.stdout.strip())
    if args.command == 'check':
        try:
            result = check_revision(root, args.base, expected_revision=args.expected_revision)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            parser.error(str(error))
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Incremental verification {result['status']} at {result['revision'][:12]}: "
                  f"{len(result['selected'])} files executed, {result['skipped_files']} reused; "
                  f"{result['reason']}")
            if result['evidence']:
                print(result['evidence'], file=sys.stderr)
        return 0 if result['status'] in ('passed', 'skipped') else 1
    result = record_pending(root) if args.pending else record_tests(root, args.paths)
    print(f"Recorded {result['recorded']} of {result['tests']} test files "
          f"({result['failed']} failing) in {result['seconds']} s "
          f"at {result['head'][:12]} from {root}.")
    for case in result.get('failed_cases', ()):
        print(f'Failing case: {case}', file=sys.stderr)
    if result['error']:
        print(f"Verification incomplete: {result['error']}")
        return 1
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
