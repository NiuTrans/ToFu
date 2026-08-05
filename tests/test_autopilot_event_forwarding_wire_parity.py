"""Wire-parity guards for the VU event-forwarding cluster.

History: pt_00459503 slice 5 extracted ``_VU_FORWARD_TYPES`` /
``_VUEventForwarder`` / ``_emit_vu_setup_phase`` out of
``lib.tasks_pkg.autopilot`` into the leaf
``lib.tasks_pkg.autopilot_event_forwarding``.  The 2026-07-26
VU-carrier stream-contract fix (conv ms1rrjchpa5pqw) then REPLACED the
list-subclass ``_VUEventForwarder`` with ``make_vu_event_transform``:
the carrier's own stream / push / event log now carry the full VU
envelope (wrapped ``autopilot_vu_event`` + verbatim lifecycle frames),
consumed through the ``append_event`` facade seam instead of a list
subclass.  This suite pins the NEW contract:

  1. The leaf module exists AND owns the definitions (not just re-exports).
  2. The facade at ``lib.tasks_pkg.autopilot`` exposes the public names,
     each IDENTITY-EQUAL to the leaf's — the load-bearing property for
     ``monkeypatch.setattr(ap, '_emit_vu_setup_phase', ...)`` which
     ``tests/test_autopilot_warmup_setup_phase.py`` uses.
  3. The old list-subclass machinery is gone from BOTH files — no
     zombie ``_VUEventForwarder`` declarations (source-scan guard).
  4. Functional smoke: the transform wraps a forwardable event onto the
     carrier's own stream AND forwards it to the parent (dual-emit),
     passes lifecycle frames verbatim WITHOUT a parent copy (the
     explicit dual-emit helper owns those), and drops non-contract
     frames.

Guarded against NEUTER: delete the leaf / reinject the class inline /
drop the dual-emit from the transform.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


AUTOPILOT_PATH = Path(__file__).resolve().parents[1] / 'lib' / 'tasks_pkg' / 'autopilot.py'
LEAF_PATH = Path(__file__).resolve().parents[1] / 'lib' / 'tasks_pkg' / 'autopilot_event_forwarding.py'

_LEAF_SYMBOLS = ('_VU_FORWARD_TYPES', '_VU_LIFECYCLE_TYPES',
                 'make_vu_event_transform', '_emit_vu_setup_phase')


@pytest.fixture
def reload_modules():
    """Force a fresh import so any monkeypatched sys.modules state is dropped —
    then RESTORE the original module objects. The delete-and-leave version of
    this fixture broke the session-wide single-module-instance invariant every
    monkeypatch-steering suite relies on (pt_788b25a5 batch pollution)."""
    from tests._hermetic_import import hermetic_import_surface
    with hermetic_import_surface('lib.tasks_pkg.autopilot'):
        yield


def test_event_forwarding_leaf_module_exists_and_defines_all_symbols(reload_modules):
    """Leaf must exist and OWN the four symbols (not merely re-export)."""
    assert LEAF_PATH.exists(), (
        f'{LEAF_PATH} missing — event-forwarding leaf was not extracted.')
    leaf = importlib.import_module('lib.tasks_pkg.autopilot_event_forwarding')
    for name in _LEAF_SYMBOLS:
        assert hasattr(leaf, name), f'leaf missing {name}'

    # Source-scan: ensure the leaf actually declares them (defends against
    # a fake leaf that just re-exports from autopilot — which would keep
    # the cycle and defeat the extraction).
    src = LEAF_PATH.read_text(encoding='utf-8')
    tree = ast.parse(src)
    top_level = {node.name: node for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    top_level_assigns = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    top_level_assigns.add(tgt.id)
    assert 'make_vu_event_transform' in top_level, (
        'leaf must declare def make_vu_event_transform at top level.')
    assert '_emit_vu_setup_phase' in top_level, (
        'leaf must declare def _emit_vu_setup_phase at top level.')
    assert '_VU_FORWARD_TYPES' in top_level_assigns
    assert '_VU_LIFECYCLE_TYPES' in top_level_assigns


def test_autopilot_facade_reexports_by_identity(reload_modules):
    """The facade attribute IS the leaf attribute — required for monkeypatch steering."""
    leaf = importlib.import_module('lib.tasks_pkg.autopilot_event_forwarding')
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    for name in _LEAF_SYMBOLS:
        assert hasattr(ap, name), (
            f'autopilot facade missing re-exported name {name!r}.')
        assert getattr(ap, name) is getattr(leaf, name), (
            f'{name}: facade must be IDENTITY-EQUAL to leaf — a copy breaks '
            f'monkeypatch.setattr(ap, {name!r}, ...) which relies on rebinding '
            f'the facade attribute.')


def test_no_zombie_vu_event_forwarder_declarations():
    """Source-scan: the list-subclass era is over — neither file may declare
    ``_VUEventForwarder`` (a zombie class would silently split the contract
    back into raw-own-list + wrapped-parent)."""
    for path in (AUTOPILOT_PATH, LEAF_PATH):
        src = path.read_text(encoding='utf-8')
        tree = ast.parse(src)
        inline = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        assert '_VUEventForwarder' not in inline, (
            f'{path.name} must NOT declare class _VUEventForwarder — the '
            f'transform replaced the list subclass (2026-07-26).')


def test_transform_wraps_and_dual_emits(monkeypatch, reload_modules):
    """Functional smoke: a forwardable event lands wrapped on BOTH streams."""
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    forwarded = []

    def fake_append_event(task, ev):
        forwarded.append((task, ev))

    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', fake_append_event)

    parent = {'id': 'parent-t1', 'events': []}
    xform = ap.make_vu_event_transform(parent, 'vu-msg-abc')
    own = xform({'id': 'vu-t1'}, {'type': 'delta', 'content': 'hi'})

    # The carrier's own stream gets the wrapped frame.
    assert own is not None and own.get('type') == 'autopilot_vu_event', (
        f'own-stream frame must be autopilot_vu_event, got {own!r}')
    assert own.get('vuMsgId') == 'vu-msg-abc'
    assert own.get('inner', {}).get('type') == 'delta'

    # …and the parent stream gets the SAME wrapped forward (pre-hop window).
    assert len(forwarded) == 1, (
        f'expected exactly 1 forwarded frame to parent, got {len(forwarded)}')
    parent_task, wrapped = forwarded[0]
    assert parent_task is parent
    assert wrapped.get('type') == 'autopilot_vu_event'
    assert wrapped.get('vuMsgId') == 'vu-msg-abc'
    assert wrapped.get('inner', {}).get('type') == 'delta'


def test_transform_lifecycle_verbatim_and_drops(monkeypatch, reload_modules):
    """Lifecycle frames pass verbatim (no parent forward — the explicit
    dual-emit owns that copy); non-contract frames are dropped."""
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    forwarded = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: forwarded.append((task, ev)))

    parent = {'id': 'parent-t2', 'events': []}
    xform = ap.make_vu_event_transform(parent, 'vu-msg-xyz')

    for et in ('autopilot_vu_start', 'autopilot_vu_done', 'autopilot_vu_cancel'):
        out = xform({'id': 'vu-t2'}, {'type': et, 'vuMsgId': 'vu-msg-xyz'})
        assert out is not None and out.get('type') == et, (
            f'lifecycle frame {et} must pass verbatim, got {out!r}')
    assert forwarded == [], (
        f'lifecycle frames must NOT be forwarded by the transform: {forwarded}')

    for raw in ({'type': 'done'}, {'type': 'round_committed'},
                {'type': 'mystery'}):
        assert xform({'id': 'vu-t2'}, raw) is None, (
            f'non-contract frame {raw!r} must be dropped')
    assert forwarded == []
