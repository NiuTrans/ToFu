"""Landing verification: a merged result must not newly fail tests that pass on trunk.

Responsibility
    ``scripts/git_land.py`` asks this module before it publishes any merge,
    fast-forwards included. Only the test files the merged tree can affect run
    on it. A test file with a recorded run (``impact_selection.py``) runs when
    code it executed, a file it read or a directory it listed changed, or when
    it failed and the current branch differs from its recorded run. A test
    file without a record runs when the repository's static selector
    (``scripts/test_select.py``) chooses it for the change. Every run is
    recorded, so the next landing runs fewer. A test that fails on the merged
    tree, passes on the current branch tip and fails again when rerun is a
    regression and refuses the landing. A record that still describes the tip
    decides the tip's outcome without running it. Tests failing at the tip as
    well, and failures that do not reproduce, are reported and never block.
    Missing evidence (no selector, a runner that cannot start on either side)
    is reported as unavailable and does not block: only a reproduced
    regression is attributable to a landing.

Entry points
    ``merge_checkout(top, head)``: the private checkout git_land merges in,
        the warm one (Python bytecode kept) when no other landing holds it,
        else a temporary one, so landings never wait for each other.
    ``verify_merge(top, head, source, merged_root, tree)`` -> ``Verdict``
    ``record_tests(top, paths)``, also ``python3 -m
        lib.project_mod.landing_verification record [PATH ...]``: record runs at
        the current branch tip ahead of landings.
    ``warm_checkout_path(top)``.

Dependencies: the git executable, the merged tree's ``scripts/test_select.py``
(test selection and the inner-loop pytest command), the receiving checkout's
project interpreter (``.venv/bin/python3``), ``impact_selection`` and ``lanes``
(the private local root holding the warm checkout and the records).
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

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
# Ignored state the warm checkout keeps between landings: caches their tools
# validate against sources, and the dependency links.
_WARM_KEEP = ('__pycache__', '/.venv', '/node_modules')


@dataclasses.dataclass(frozen=True)
class Verdict:
    status: str  # skipped | passed | regressed | unavailable
    reason: str = ''
    head: str = ''
    trunk_commits: int = 0
    selected: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    preexisting: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    evidence: str = ''
    skipped_files: int = 0
    seconds: float = 0.0


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


def _failed_ids(output: str) -> tuple[str, ...]:
    failed, in_summary = [], False
    for line in output.splitlines():
        if _SUMMARY_HEADER.match(line):
            in_summary = True
            continue
        if in_summary:
            match = _SUMMARY_LINE.match(line)
            if match:
                failed.append(match.group(1).split(' - ', 1)[0].strip())
    return tuple(dict.fromkeys(failed))


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
    (plugin_dir / f'{_PLUGIN_MODULE}.py').write_text(impact_selection.plugin_source(),
                                                      encoding='utf-8')
    argv = [python, *selector.build_pytest_command(list(tests), None)[1:],
            '-rfE', '--continue-on-collection-errors', '-p', _PLUGIN_MODULE]
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(PYTHONPATH=os.pathsep.join([str(plugin_dir), str(root)]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               TOFU_TEST_IMPACT_OUT=str(recorder / 'out'), TOFU_TEST_IMPACT_ROOT=str(root))
    with open(log, 'w', encoding='utf-8') as handle:
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
    failed = _failed_ids(output)
    observations = tuple(_observations(recorder / 'out'))
    if returncode in (0, 5):
        return _Run(True, (), output, observations)
    if returncode == 1 and failed:
        return _Run(True, failed, output, observations)
    return _Run(False, failed, output, observations)


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
    with _checkout(top, head) as tip:
        _bridge(top, tip)
        if not merged.usable:
            at_tip = _run(python, tip, selector, list(common['selected']), logs / 'tip.log')
            if at_tip.usable:
                return Verdict('regressed', 'the selected tests cannot run on the merged '
                               'result but run on the current branch tip',
                               evidence=_tail(merged.output), **common)
            return Verdict('unavailable', 'the selected tests cannot run on the merged '
                           'result or on the current branch tip',
                           evidence=_tail(merged.output), **common)
        at_tip = _run(python, tip, selector, present, logs / 'tip.log')
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


def _environment_key(python: str) -> str:
    """The project interpreter and its installed distributions; records hold for one."""
    probe = ('import importlib.metadata as m, sys\nprint(sys.version)\n'
             'print(sorted(f"{d.metadata[\'Name\']}=={d.version}" for d in m.distributions()))')
    listed = subprocess.run([python, '-I', '-c', probe], capture_output=True, text=True)
    return hashlib.sha256(f'{os.path.realpath(python)}\n{listed.returncode}\n'
                          f'{listed.stdout}'.encode('utf-8')).hexdigest()[:20]


def _store(top: Path, python: str) -> impact_selection.ImpactStore | None:
    try:
        return impact_selection.ImpactStore(_cache_dir(top) / 'impact.sqlite',
                                            _environment_key(python))
    except (OSError, impact_selection.ImpactCacheError):
        return None  # records are a cache: without them every file is selected statically


def _records(store) -> dict:
    try:
        return store.records() if store is not None else {}
    except impact_selection.ImpactCacheError:
        return {}


def _reset_warm(top: Path, warm: Path, head: str) -> bool:
    if not (warm / '.git').is_file():
        return False
    common = _git(warm, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    expected = _git(top, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if common.returncode or common.stdout.strip() != expected.stdout.strip():
        return False
    # Exactly a fresh checkout's contents, plus the caches named in _WARM_KEEP.
    return (_git(warm, 'reset', '-q', '--hard', head).returncode == 0
            and _git(warm, 'clean', '-ffdxq',
                     *(f'--exclude={pattern}' for pattern in _WARM_KEEP)).returncode == 0)


@contextlib.contextmanager
def merge_checkout(top, head: str):
    """A private checkout at HEAD to merge and verify in; the warm one when it is free.

    The warm checkout keeps Python bytecode between landings, so only changed
    files are rewritten and recompiled (cold collection of 30 test files took
    127 s, warm 30 s on 2026-11-07). A landing that finds it in use gets a
    temporary checkout instead of waiting.
    """
    top = Path(top)
    lock = None
    try:
        cache = _cache_dir(top)
        lock = open(cache / 'checkout.lock', 'a+')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock is not None:
            lock.close()
        lock = None
    if lock is None:
        with _checkout(top, head) as scratch:
            _bridge(top, scratch)
            yield scratch
        return
    try:
        warm = cache / 'checkout'
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


def _remember(store, run: _Run, tests: list[str], tree: impact_selection.Tree, tree_id: str,
              index: dict) -> list:
    """Record a run of whole test files; files without complete observations stay unrecorded."""
    if store is None or not run.observations:
        return []
    refs = {test: _refs(index.get(test)) for test in tests}
    records = impact_selection.records_from_observations(list(run.observations), tests, tree,
                                                         tree_id, refs)
    try:
        if records:
            store.save(records)
    except impact_selection.ImpactCacheError:
        return []
    return records


def _index(selector, top: Path, checkout: Path) -> dict:
    """The selector's static index, cached between landings for the warm checkout."""
    cache = (_cache_dir(top) / 'test-index.json') if checkout == warm_checkout_path(top) else None
    return selector.build_index(str(checkout / 'tests'), str(cache) if cache else None)


# ─── Verification ──────────────────────────────────────────────────────────

def verify_merge(top, head: str, source: str, merged_root, tree: str | None = None) -> Verdict:
    """Run the test files the merged tree can affect on MERGED_ROOT and attribute failures."""
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
        verdict = _verify(top, head, merged_root, tree, changed, selector, store, python, common)
    finally:
        if store is not None:
            store.close()
    return dataclasses.replace(verdict, seconds=time.monotonic() - started)


def _verify(top: Path, head: str, merged_root: Path, tree: str, changed: list[str], selector,
            store, python: str, common: dict) -> Verdict:
    try:
        index = _index(selector, top, merged_root)
        _static, reasons = selector.select_tests(index, changed)
        blast = any(path in getattr(selector, 'BLAST_RADIUS', {}) for path in changed)
        merged = impact_selection.Tree.of(top, tree)
        tip = impact_selection.Tree.of(top, head)
        records = _records(store)
        planned = impact_selection.plan(records, sorted(index), reasons, blast, tip, merged,
                                        store, top)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError,
            impact_selection.ImpactCacheError) as error:
        return Verdict('unavailable', f'test selection failed: {error}', **common)
    selected = sorted(planned.run)
    common.update(selected=tuple(selected), skipped_files=planned.skipped)
    if not selected:
        if planned.recorded:
            return Verdict('passed', 'no recorded test file used code or files the merge '
                           'changes', **common)
        return Verdict('skipped', "no test references the branch's changes", **common)
    _bridge(top, merged_root)
    with tempfile.TemporaryDirectory(prefix='git-land-verify-') as logs:
        logs = Path(logs)
        run = _run(python, merged_root, selector, selected, logs / 'merged.log')
        _remember(store, run, selected, merged, tree, index)
        return _attribute(top, head, python, selector, run, merged_root, logs, common,
                          records, tip, store)


def _known_at_tip(failed: tuple[str, ...], records: dict, tip, store, top: Path):
    """Tip outcomes that records still describe, and the failures that need a tip run."""
    known, unknown = {}, []
    for nodeid in failed:
        record = records.get(nodeid.split('::', 1)[0])
        if record is not None and not impact_selection.affected(record, tip, store, top):
            known[nodeid] = 'failed' if nodeid in record.failed else 'passed'
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
    # A test file the branch added does not exist at the tip: its failure
    # cannot be preexisting, so it needs no comparison run.
    present = [test for test in unknown if _git(
        top, 'cat-file', '-e', f"{head}:{test.split('::', 1)[0]}").returncode == 0]
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
    confirm = _run(python, merged_root, selector, candidates, logs / 'confirm.log')
    if not confirm.usable and not confirm.failed:
        return Verdict('unavailable', 'the failing tests could not be rerun on the merged '
                       'result', preexisting=preexisting,
                       evidence=_tail(confirm.output), **common)
    reproduced = set(confirm.failed)
    regressions = tuple(test for test in candidates if test in reproduced)
    flaky = tuple(test for test in candidates if test not in reproduced)
    if regressions:
        return Verdict('regressed', 'tests that pass on the current branch tip fail on the '
                       'merged result', regressions=regressions, preexisting=preexisting,
                       flaky=flaky, evidence=_tail(confirm.output), **common)
    return Verdict('passed', 'failures on the merged result did not reproduce',
                   preexisting=preexisting, flaky=flaky, **common)


# ─── Recording ahead of landings ───────────────────────────────────────────

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
        index = _index(selector, top, checkout)
        wanted = [path.rstrip('/') for path in paths or ()]
        chosen = sorted(test for test in index if not wanted or any(
            test == path or test.startswith(f'{path}/') for path in wanted))
        if not chosen:
            return {'tests': 0, 'recorded': 0, 'failed': 0, 'seconds': 0.0}
        tree_id = _git(top, 'rev-parse', f'{head}^{{tree}}').stdout.strip()
        tree = impact_selection.Tree.of(top, head)
        store = _store(top, python)
        try:
            with tempfile.TemporaryDirectory(prefix='git-land-record-') as logs:
                run = _run(python, checkout, selector, chosen, Path(logs) / 'record.log')
                records = _remember(store, run, chosen, tree, tree_id, index)
        finally:
            if store is not None:
                store.close()
    return {'tests': len(chosen), 'recorded': len(records),
            'failed': sum(1 for record in records if record.outcome == 'failed'),
            'seconds': round(time.monotonic() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description='Record test runs at the current branch tip for landing verification.')
    commands = parser.add_subparsers(dest='command', required=True)
    record = commands.add_parser('record', help='run and record test files')
    record.add_argument('paths', nargs='*', help='test files or directories (default: all)')
    record.add_argument('--into', default='.', help='any checkout of the repository')
    args = parser.parse_args(argv)
    from lib.project_mod import lanes
    result = record_tests(lanes.repository(args.into).top, args.paths)
    print(f"Recorded {result['recorded']} of {result['tests']} test files "
          f"({result['failed']} failing) in {result['seconds']} s.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
