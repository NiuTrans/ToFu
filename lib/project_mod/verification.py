"""Incremental verification of an immutable snapshot of the working checkout.

Entry points: verify_tests and ``python -m lib.project_mod.verification
{run,plan} [TEST FILE/DIRECTORY ... | --changed [--base REF]]``. Captures staged, unstaged and nonignored
untracked bytes using the lane owner's private index. The shared landing runner
executes only files without reusable complete passing evidence, and publishes
its records to the same cache used by landing verification. Failures and opaque
external-input tests always run. Domain/release gates remain independent.

Dependencies: lanes (capture), landing_verification (execution and evidence),
impact_selection (dependency comparison). No application storage or live service.
Contract: docs/modules/project_integration.md, "Incremental verification".
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from lib.project_mod import impact_selection as impact
from lib.project_mod import landing_verification as landing
from lib.project_mod import lanes
from lib.project_mod import verification_index


@dataclasses.dataclass
class Result:
    status: str
    head: str
    tree: str
    scope: str = 'explicit'
    base: str = ''
    changed: tuple[str, ...] = ()
    selected: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    failed_cases: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    recorded: int = 0
    reasons: dict = dataclasses.field(default_factory=dict)
    selection_reasons: dict = dataclasses.field(default_factory=dict)
    seconds: dict = dataclasses.field(default_factory=dict)
    cache_error: str = ''
    evidence: str = ''


@contextlib.contextmanager
def _phase(result, name):
    started = time.perf_counter()
    try:
        yield
    finally:
        result.seconds[name] = round(result.seconds.get(name, 0.0) + time.perf_counter() - started, 6)


def _snapshot(root):
    head, tree = lanes.capture_working_tree(root)
    if tree == lanes._git(root, 'rev-parse', f'{head}^{{tree}}').stdout.strip():
        return head, tree, head
    timestamp = lanes._git(root, 'show', '-s', '--format=%ct', head).stdout.strip()
    revision = lanes._git(root, 'commit-tree', tree, '-p', head, '-m', 'Verification snapshot',
                          env={'GIT_AUTHOR_NAME': 'Tofu Verification',
                               'GIT_AUTHOR_EMAIL': 'verification@tofu.local',
                               'GIT_COMMITTER_NAME': 'Tofu Verification',
                               'GIT_COMMITTER_EMAIL': 'verification@tofu.local',
                               'GIT_AUTHOR_DATE': f'@{timestamp} +0000',
                               'GIT_COMMITTER_DATE': f'@{timestamp} +0000'}).stdout.strip()
    return head, tree, revision


def _plan(result, tree, records, store, root):
    reused = []
    for test in result.selected:
        record = records.get(test)
        if record is None:
            reason = 'no complete recorded evidence'
        elif not {'scripts/test_select.py', 'scripts/test_execution.py'} & record.deps.keys():
            reason = 'record lacks execution-policy evidence'
        elif record.outcome != 'passed':
            reason = 'previous failure requires current execution'
        elif record.opaque:
            reason = 'unobserved external inputs require execution'
        else:
            changed = impact.affected(record, tree, store, root)
            reason = f'recorded input changed: {changed}' if changed else ''
        result.reasons[test] = reason or 'complete passing evidence; recorded inputs unchanged'
        if not reason:
            reused.append(test)
    result.reused = tuple(reused)
    reused_set = set(reused)
    return [test for test in result.selected if test not in reused_set]


def _changed_paths(root, result, base):
    """Diff only the captured input; a named base also includes working bytes."""
    result.scope = 'changed'
    result.base = result.head
    if base is not None:
        revision = lanes._git(root, 'rev-parse', '--verify', '--end-of-options',
                              f'{base}^{{commit}}', check=False)
        if revision.returncode:
            raise ValueError(f'invalid comparison base: {base}')
        result.base = lanes._git(root, 'merge-base', revision.stdout.strip(),
                                  result.head).stdout.strip()
    difference = lanes._git(root, 'diff', '--name-only', '--no-renames', '-z',
                             result.base, result.tree, '--')
    result.changed = tuple(sorted(filter(None, difference.stdout.split('\0'))))


def _affected_scope(result, root, selector, index, tree, records, store):
    """Combine the existing static policy with recorded dynamic dependencies."""
    selected = landing._static_selection(
        selector, index, list(result.changed), landing._global_inputs(selector),
        impact.Tree.of(root, result.base), tree, store, root)
    for test, record in records.items():
        if test in index:
            changed = impact.affected(record, tree, store, root)
            if changed:
                selected.setdefault(test, f'recorded input changed: {changed}')
    result.selection_reasons = selected
    result.selected = tuple(sorted(selected))


def _execute(result, root, checkout, selector, python, chosen, tree, store, index):
    if not chosen:
        result.status = 'passed'
        result.seconds.update(execute=0.0, record=0.0)
        return
    with tempfile.TemporaryDirectory(prefix='tofu-verify-run-') as directory:
        with _phase(result, 'execute'):
            run = landing._run(python, checkout, selector, chosen, Path(directory) / 'run.log')
            run = landing._check_snapshot(checkout, result.tree, run)
    result.executed = tuple(chosen)
    result.failed_cases = run.failed
    with _phase(result, 'record'):
        recorded, error = landing._remember(store, run, chosen, tree, result.tree, index,
                                             landing._global_inputs(selector))
    result.recorded = len(recorded)
    result.cache_error = error
    # Cache availability is separate from execution evidence: a disk failure
    # cannot erase completed assertions, but cannot authorize later reuse either.
    observed = recorded if not error else impact.records_from_observations(
        list(run.observations), chosen, tree, result.tree,
        {test: landing._refs(index[test]) for test in chosen}, landing._global_inputs(selector))
    verified = {record.test for record in observed}
    result.unverified = tuple(test for test in chosen if test not in verified)
    result.status = ('failed' if run.failed else
                     'incomplete' if not run.usable or result.unverified else 'passed')
    if result.status != 'passed':
        result.evidence = landing._tail(run.output)


def verify_tests(top, paths=None, *, execute=True, changed=False, base=None) -> Result:
    """Verify the captured working bytes. PLAN never claims that tests passed."""
    started = time.perf_counter()
    changed = changed or base is not None
    if changed and paths:
        raise ValueError('changed selection and explicit test paths are separate scopes')
    root = Path(lanes._git(top, 'rev-parse', '--show-toplevel').stdout.strip())
    result = Result('planned', '', '')
    with _phase(result, 'snapshot'):
        result.head, result.tree, revision = _snapshot(root)
        if changed:
            _changed_paths(root, result, base)
    if changed and not result.changed:
        result.status = 'unchanged' if execute else 'planned'
        result.seconds['total'] = round(time.perf_counter() - started, 6)
        return result
    python = landing._project_interpreter(root)
    with contextlib.ExitStack() as stack:
        with _phase(result, 'prepare'):
            cache = landing._cache_dir(root)
            store = landing._store(root, python, cache)
            if store is not None:
                stack.callback(store.close)
        checkout = selector = index = None
        reuse_supported = True
        with _phase(result, 'index'):
            tree = impact.Tree.of(root, result.tree)
            if 'scripts/test_select.py' not in tree.blobs:
                raise ValueError('the repository declares no test selector')
            try:
                inventory = verification_index.test_inventory(root, result.tree)
            except verification_index.UnsupportedIndex:
                reuse_supported = False
                checkout = stack.enter_context(landing.merge_checkout(root, revision, cache=cache))
                selector = landing._load_selector(checkout)
                index = landing._index(selector, root, checkout, result.tree, paths, cache)
                inventory = index
            if changed:
                if checkout is None:
                    checkout = stack.enter_context(landing.merge_checkout(root, revision, cache=cache))
                    selector = landing._load_selector(checkout)
                    index = landing._index(selector, root, checkout, result.tree, cache=cache)
            else:
                result.selected = tuple(verification_index.select_paths(inventory, paths))
        with _phase(result, 'plan'):
            records = landing._records(store) if reuse_supported else {}
            if changed:
                _affected_scope(result, root, selector, index, tree, records, store)
            chosen = _plan(result, tree, records, store, root)
        if execute:
            if chosen and checkout is None:
                with _phase(result, 'prepare'):
                    checkout = stack.enter_context(landing.merge_checkout(root, revision, cache=cache))
                    selector = landing._load_selector(checkout)
                with _phase(result, 'index'):
                    index = landing._index(selector, root, checkout, result.tree, chosen, cache)
            _execute(result, root, checkout, selector, python, chosen, tree, store, index)
            if not result.selected:
                result.status = 'unselected'
    result.seconds['total'] = round(time.perf_counter() - started, 6)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'plan'))
    parser.add_argument('paths', nargs='*', help='test files/directories; default: all tests')
    parser.add_argument('--into', default='.', help='checkout to snapshot (never modified)')
    parser.add_argument('--json', action='store_true', help='machine-readable result and phase timings')
    parser.add_argument('--changed', action='store_true', help='select tests affected by captured changes')
    parser.add_argument('--base', help='compare with this ref\'s merge base, including working changes')
    args = parser.parse_args(argv)
    try:
        result = verify_tests(Path(args.into).expanduser(), args.paths, execute=args.command == 'run',
                               changed=args.changed, base=args.base)
    except (OSError, ValueError, subprocess.SubprocessError, lanes.LaneError,
            impact.ImpactCacheError) as error:
        if args.json:
            print(json.dumps({'status': 'incomplete', 'error': str(error)}))
        else:
            print(f'Verification incomplete: {error}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(dataclasses.asdict(result), ensure_ascii=False))
    else:
        print(f'Verification {result.status}: {len(result.executed)} files executed, '
              f'{len(result.reused)} reused, {len(result.unverified)} incomplete; '
              f'tree {result.tree[:12]} in {result.seconds["total"]:.2f}s')
        print('Phases: ' + ', '.join(f'{name}={seconds:.3f}s' for name, seconds in result.seconds.items()))
        if args.command == 'plan':
            for test, reason in result.reasons.items():
                scope = result.selection_reasons.get(test)
                print(f'  {test}: {scope + "; " if scope else ""}{reason}')
        if result.scope == 'changed':
            print(f'Affected scope: {len(result.changed)} changed paths against {result.base[:12]}; '
                  'domain and release gates remain required')
        for case in result.failed_cases:
            print(f'Failing case: {case}', file=sys.stderr)
        if result.cache_error:
            print(f'Evidence cache: {result.cache_error}', file=sys.stderr)
        if result.evidence:
            print(result.evidence, file=sys.stderr)
    return 0 if result.status in ('passed', 'planned', 'unchanged', 'unselected') else 1


if __name__ == '__main__':
    sys.exit(main())
