"""Static guard for the Request Inspector snapshot contract.

Design: docs/FRONTEND_ARCHITECTURE.md §3 (frozen). Every
``build_event(EventType.MESSAGES_SNAPSHOT, ...)`` emission site MUST carry an
explicit ``kind=`` classifying it:

  * ``kind='request'`` — the payload the model is ABOUT to receive. Exactly
    ONE site today (orchestrator/_run.py, pre-request per round). It MUST
    also carry ``model=`` and ``params=`` with the frozen schema keys.
  * ``kind='state'`` — a state mirror, NOT an LLM request (post-tool /
    final / fallback). The request list must never mix these in.

The guard is AST-based (regexes would choke on the multi-line call sites):

  1. Scans ``lib/`` for every ``build_event(EventType.MESSAGES_SNAPSHOT, …)``
     call AND for raw ``{'type': 'messages_snapshot', …}`` dict literals
     (which would bypass the contract — none may exist).
  2. Asserts the site count stays exactly 5 (a NEW snapshot site must make a
     conscious kind decision here; a removed site must update this guard).
  3. Asserts each site's kind value and the request site's frozen params
     schema.

NEUTER (negative control): patching a COPY of the request site to drop
``kind=`` must make the validator flag it — proving the guard bites.
"""

from __future__ import annotations

import ast
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
LIB = os.path.join(ROOT, 'lib')

# Frozen emission-site table (docs/FRONTEND_ARCHITECTURE.md §3.1). Adding a
# fifth site must update this table deliberately; removing one likewise.
EXPECTED_SITES = {
    # The request site moved out of _run.py into its own HOT_PATH helper
    # module (pt_03f4cdf1 slice 15, 2026-07-31).
    os.path.join('lib', 'tasks_pkg', 'orchestrator',
                 '_messages_snapshot.py'): 'request',
    os.path.join('lib', 'tasks_pkg', 'tool_dispatch', '_pipeline.py'): 'state',
    os.path.join('lib', 'tasks_pkg', 'orchestrator', '_post_loop.py'): 'state',
    os.path.join('lib', 'tasks_pkg', 'orchestrator', '_finalize.py'): 'state',
    # P4 (epic pt_e3dc7198e7e34bb1): swarm sub-agent per-round LLM request
    # (persisted directly under '{parent}#agent:{id}' — see agent.py).
    os.path.join('lib', 'swarm', 'agent.py'): 'request',
}
REQUEST_SITE = os.path.join('lib', 'tasks_pkg', 'orchestrator',
                            '_messages_snapshot.py')

# Frozen request-params schema (design doc §3.3). Additive renames forbidden —
# the frontend request row reads exactly these keys.
FROZEN_PARAMS_KEYS = {
    'maxTokens', 'temperature', 'thinkingEnabled', 'thinkingDepth',
    'preset', 'responseFormat', 'stream',
}


def _is_snapshot_build_event(node: ast.Call) -> bool:
    """True for ``build_event(EventType.MESSAGES_SNAPSHOT, …)`` calls."""
    if not isinstance(node.func, ast.Name) or node.func.id != 'build_event':
        return False
    if not node.args:
        return False
    a0 = node.args[0]
    return (
        isinstance(a0, ast.Attribute)
        and a0.attr == 'MESSAGES_SNAPSHOT'
        and isinstance(a0.value, ast.Name)
        and a0.value.id == 'EventType'
    )


def _is_snapshot_dict_literal(node: ast.Call) -> bool:
    """True for append_event(task, {'type': 'messages_snapshot', …}) — a raw
    dict emission that bypasses build_event's contract surface."""
    for kw in node.args:
        if not isinstance(kw, ast.Dict):
            continue
        for k, v in zip(kw.keys, kw.values):
            if (
                isinstance(k, ast.Constant) and k.value == 'type'
                and isinstance(v, ast.Constant)
                and v.value == 'messages_snapshot'
            ):
                return True
    return False


def _kw_const(node: ast.Call, name: str):
    """Return the constant value of keyword ``name`` (or None if absent /
    non-constant)."""
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _has_kw(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


def _params_keys(node: ast.Call) -> set:
    """Keys of the literal dict passed as ``params=`` (empty if absent /
    non-literal)."""
    for kw in node.keywords:
        if kw.arg == 'params' and isinstance(kw.value, ast.Dict):
            return {
                k.value for k in kw.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    return set()


def _scan_source(src: str, rel_path: str) -> list:
    """Return [(rel_path, lineno, call)] for every snapshot emission site."""
    tree = ast.parse(src, filename=rel_path)
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            _is_snapshot_build_event(node) or _is_snapshot_dict_literal(node)
        ):
            sites.append((rel_path, node.lineno, node))
    return sites


def _scan_lib(lib_root: str) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(lib_root):
        # Skip trash/cache dirs (e.g. lib/tasks_pkg/.tofu_trash holds stale
        # file copies that are NOT live emission sites and may not parse).
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith('.') and d != '__pycache__'
        ]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            with open(full, encoding='utf-8') as f:
                try:
                    out.extend(_scan_source(f.read(), rel))
                except SyntaxError:  # pragma: no cover - defensive
                    raise AssertionError(f'cannot parse {full}')
    return out


def _validate(sites: list) -> list:
    """Return a list of contract violations (empty = pass)."""
    problems = []
    by_file = {}
    for rel, _lineno, node in sites:
        by_file.setdefault(rel, []).append(node)

    # 1. Site set must match the frozen table exactly (no new/removed sites).
    got = set(by_file)
    want = set(EXPECTED_SITES)
    if got != want:
        problems.append(
            f'snapshot emission site set drifted: missing={sorted(want - got)} '
            f'unexpected={sorted(got - want)} — update the frozen table in '
            f'tests/test_messages_snapshot_kind.py deliberately')

    # 2. Every site must carry an explicit kind= matching its table entry.
    for rel, lineno, node in sites:
        kind = _kw_const(node, 'kind')
        if kind not in ('request', 'state'):
            problems.append(
                f'{rel}:{lineno} snapshot emission missing kind= '
                f"(got {kind!r}) — 'request' or 'state' required "
                f'(docs/FRONTEND_ARCHITECTURE.md §3)')
            continue
        expected = EXPECTED_SITES.get(rel)
        if expected and kind != expected:
            problems.append(
                f'{rel}:{lineno} kind={kind!r} != frozen {expected!r}')

    # 3. The request site must carry model= + params= with the frozen keys.
    for rel, lineno, node in sites:
        if rel != REQUEST_SITE:
            continue
        if not _has_kw(node, 'model'):
            problems.append(f'{rel}:{lineno} request snapshot missing model=')
        keys = _params_keys(node)
        if keys != FROZEN_PARAMS_KEYS:
            problems.append(
                f'{rel}:{lineno} request params schema drifted: '
                f'missing={sorted(FROZEN_PARAMS_KEYS - keys)} '
                f'extra={sorted(keys - FROZEN_PARAMS_KEYS)} — the request-row '
                f'schema is frozen (design doc §3.3)')
    return problems


def test_snapshot_sites_all_stamped_with_kind():
    sites = _scan_lib(LIB)
    assert len(sites) == 5, (
        f'expected exactly 5 messages_snapshot emission sites, found '
        f'{len(sites)}: {[(r, ln) for r, ln, _ in sites]}')
    problems = _validate(sites)
    assert not problems, 'snapshot contract violations:\n' + '\n'.join(problems)


def test_neuter_guard_bites_when_kind_dropped():
    """Negative control: strip kind= from a COPY of the request site → the
    validator MUST flag it. Proves the guard is not vacuous."""
    full = os.path.join(ROOT, REQUEST_SITE)
    with open(full, encoding='utf-8') as f:
        src = f.read()
    assert "kind='request'," in src, (
        'request site source drifted — expected literal kind= marker')
    neutered = src.replace("kind='request',\n", '', 1)
    assert neutered != src
    sites = _scan_source(neutered, REQUEST_SITE)
    assert sites, 'neutered copy should still contain the emission site'
    problems = _validate(sites)
    assert any('missing kind=' in p for p in problems), (
        f'guard did NOT bite on the kind-stripped copy: {problems}')


def test_neuter_guard_bites_when_params_dropped():
    """Negative control: drop the params= block from a COPY → flagged."""
    full = os.path.join(ROOT, REQUEST_SITE)
    with open(full, encoding='utf-8') as f:
        src = f.read()
    assert "'maxTokens': max_tokens," in src
    neutered = src.replace("'maxTokens': max_tokens,\n", '', 1)
    assert neutered != src
    sites = _scan_source(neutered, REQUEST_SITE)
    problems = _validate(sites)
    assert any('params schema drifted' in p for p in problems), (
        f'guard did NOT bite on the params-stripped copy: {problems}')


if __name__ == '__main__':
    test_snapshot_sites_all_stamped_with_kind()
    test_neuter_guard_bites_when_kind_dropped()
    test_neuter_guard_bites_when_params_dropped()
    print('OK')
