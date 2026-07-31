#!/usr/bin/env python3
"""tests/test_probe_per_cell_face.py — the probe asks each cell on ITS wire.

WHY
===
The access-matrix probe sent ONE ``protocol`` for a whole provider, because a
provider only ever had one wire. After the account/face separation a single
card can serve Claude over ``/v1/anthropic`` and everything else over
``/v1/openai/native`` with the same keys — so a provider-wide protocol probes
the Claude cells on the wrong endpoint and reports a false ``not_found``,
which the matrix then surfaces as "recommend disable" for models that work
perfectly.

The fix reuses ``provider_face.resolve_face`` (the SAME resolver the
dispatcher uses) rather than re-deriving the rule in the probe or in JS —
otherwise the probe and the dispatcher could disagree about which wire a
model uses, and the matrix would be confidently wrong.

WHAT IS PINNED (results, not implementation)
--------------------------------------------
  * Each Claude cell is probed against the anthropic base_url + protocol.
  * Non-Claude cells on the same provider keep the default face.
  * A provider with no faces{} behaves exactly as before (regression guard).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_probe_per_cell_face.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.openai.com/v1/anthropic'


def _build_work(models, faces, base_url=OPENAI_URL, protocol='openai',
                api_keys=('k1',)):
    """Drive the REAL work-list builder the route uses.

    Deliberately NOT a local re-implementation: an earlier version of this
    test rebuilt the loop itself and therefore stayed green when the route
    stopped resolving per cell — it was structurally blind to the very
    regression it existed to catch. ``build_probe_work`` is the shared seam,
    so route and test cannot drift apart.
    """
    from lib.provider_probe import build_probe_work
    prov = {'id': 'p', 'base_url': base_url, 'protocol': protocol,
            'faces': faces or {}}
    return build_probe_work(prov, models, list(api_keys))


MERGED_FACES = {'anthropic': {'base_url': ANTHROPIC_URL,
                              'protocol': 'anthropic'}}
MODELS = [
    {'model_id': 'kimi-k3', 'capabilities': ['text']},
    {'model_id': 'claude-opus-5', 'capabilities': ['text'],
     'request_ids': ['yuju-claude-opus-5-evaDaily']},
]


def test_claude_cells_carry_the_anthropic_wire():
    work = _build_work(MODELS, MERGED_FACES)
    claude = [w for w in work if w[2] == 'claude-opus-5']
    assert claude, 'no work item for the Claude model'
    for item in claude:
        assert item[5] == ANTHROPIC_URL, item
        assert item[6] == 'anthropic', item


def test_non_claude_cells_keep_the_default_wire():
    work = _build_work(MODELS, MERGED_FACES)
    other = [w for w in work if w[2] == 'kimi-k3']
    assert other
    for item in other:
        assert item[5] == OPENAI_URL, item
        assert item[6] == 'openai', item


def test_a_provider_without_faces_is_unchanged():
    """Regression: every existing single-face provider must probe exactly as
    it did before."""
    work = _build_work([{'model_id': 'gpt-4.1-mini', 'capabilities': ['text']}],
                       faces={}, base_url='https://api.openai.com/v1')
    assert work
    for item in work:
        assert item[5] == 'https://api.openai.com/v1', item
        assert item[6] == 'openai', item


def test_worker_honours_the_per_cell_face():
    """The engine must USE the tuple's face, not the task-level one."""
    import lib.provider_probe as pp

    seen = []

    def _fake_probe(base_url, api_key, model_id, extra_headers, timeout,
                    protocol='openai', oauth=''):
        seen.append({'base_url': base_url, 'model_id': model_id,
                     'protocol': protocol})
        return 'ok', 'HTTP 200'

    task = {
        'provider_id': 'p', 'status': 'running', 'started_at': 0,
        'finished_at': None, 'total': 2, 'done_count': 0, 'cells': {},
        'summary': {'ok': 0, 'disable': 0}, 'error': None, 'attempts': 1,
        '_abort': False, '_base_url': OPENAI_URL, '_extra_headers': {},
        '_protocol': 'openai', '_oauth': '',
    }
    work = _build_work(MODELS, MERGED_FACES)

    orig = pp.probe_one_cell
    orig_persist = pp.persist_probe_task
    pp.probe_one_cell = _fake_probe
    pp.persist_probe_task = lambda t: None
    try:
        pp.run_cell_probe_task(task, work, timeout=5)
    finally:
        pp.probe_one_cell = orig
        pp.persist_probe_task = orig_persist

    by_model = {s['model_id']: s for s in seen}
    # The work list probes ``[model_id] + aliases`` — i.e. the LOGICAL id for
    # a model-identity-contract entry (whose wire ids live in request_ids).
    # That is pre-existing probe behaviour and orthogonal to the wire face;
    # what matters here is WHICH WIRE the cell was asked on.
    claude = by_model.get('claude-opus-5')
    assert claude is not None, sorted(by_model)
    assert claude['protocol'] == 'anthropic', claude
    assert claude['base_url'] == ANTHROPIC_URL, claude

    kimi = by_model.get('kimi-k3')
    assert kimi is not None
    assert kimi['protocol'] == 'openai', kimi
    assert kimi['base_url'] == OPENAI_URL, kimi


def test_worker_falls_back_for_legacy_five_tuples():
    """Older work tuples (no face columns) must still probe on the task-level
    values — a snapshot resumed across the upgrade must not crash."""
    import lib.provider_probe as pp

    seen = []

    def _fake_probe(base_url, api_key, model_id, extra_headers, timeout,
                    protocol='openai', oauth=''):
        seen.append({'base_url': base_url, 'protocol': protocol})
        return 'ok', 'HTTP 200'

    task = {
        'provider_id': 'p', 'status': 'running', 'started_at': 0,
        'finished_at': None, 'total': 1, 'done_count': 0, 'cells': {},
        'summary': {'ok': 0, 'disable': 0}, 'error': None, 'attempts': 1,
        '_abort': False, '_base_url': OPENAI_URL, '_extra_headers': {},
        '_protocol': 'openai', '_oauth': '',
    }
    legacy_work = [(0, 'k1', 'kimi-k3', 'kimi-k3', ['text'])]

    orig = pp.probe_one_cell
    orig_persist = pp.persist_probe_task
    pp.probe_one_cell = _fake_probe
    pp.persist_probe_task = lambda t: None
    try:
        pp.run_cell_probe_task(task, legacy_work, timeout=5)
    finally:
        pp.probe_one_cell = orig
        pp.persist_probe_task = orig_persist

    assert seen and seen[0]['base_url'] == OPENAI_URL
    assert seen[0]['protocol'] == 'openai'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
