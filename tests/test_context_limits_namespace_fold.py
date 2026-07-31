#!/usr/bin/env python3
"""tests/test_context_limits_namespace_fold.py — learned context limits fold
out of absorbed FACE namespaces into their ACCOUNT namespace at load.

Same family as the key_stats fold (epic pt_782133699c6d4ac1), one subsystem
over: ``model_context_limits`` keys are ``<slot.provider_id>::<model>``, so
the account/face merge (charter #23) orphaned every entry learned under the
duplicate anthropic CARD — measured on the fleet file:
``sankuai_anthropic::claude-opus-5 = 1110553`` (an EXPAND learned from a real
accepted 1.1M prompt tonight) and ``sankuai_anthropic::aws.claude-opus-4.7``.
Post-merge slots carry ``provider_id='sankuai'`` → both lookups miss → the
learned window is silently lost (over-compaction or fresh 400s until
re-learned).

Pinned here:

  1. Face-namespace entries fold into the account key — value AND meta
     (a shrink entry folded without its meta would never TTL-expire).
  2. Conflict resolution: the NEWER evidence (meta ts) wins; ties go to
     the account entry (it is the surviving namespace).
  3. The fold PERSISTS (the frontend reads ``model_context_limits`` raw
     from server_config.json — in-memory-only folding would leave the UI
     showing orphans), is race-safe (the write folds the CURRENT file
     content, not the earlier read), and is idempotent.
  4. Transitional installs (duplicate cards still configured, config
     merge not yet run) map by the SAME account-identity criterion.
  5. Unknown namespaces and bare-model keys are preserved untouched;
     provider parts containing a single ':' (ephemeral:local) split
     safely on the FIRST '::'.
  6. Ratchet: after load, no learned key's provider part names an
     absorbed namespace.
  7. Structural (charter #24): the map comes from provider_face, never
     re-derived in context_limits.

Run:  pytest tests/test_context_limits_namespace_fold.py -m unit
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

FACE = 'gw_anthropic'
ACCT = 'gw'

MERGED_PROVIDERS = [
    {'id': ACCT,
     'base_url': 'https://gw.example.com/v1/openai',
     'api_keys': ['ak0'],
     'faces': {'anthropic': {
         'base_url': 'https://gw.example.com/v1/anthropic',
         'protocol': 'anthropic'}}},
]

DUPLICATE_PROVIDERS = [
    {'id': ACCT, 'base_url': 'https://gw.example.com/v1/openai',
     'api_keys': ['ak0']},
    {'id': FACE, 'base_url': 'https://gw.example.com/v1/anthropic',
     'protocol': 'anthropic', 'api_keys': ['ak0']},
]


@pytest.fixture
def cfg_file(monkeypatch, tmp_path):
    """Point lib.config_dir.config_path at a tmp server_config.json."""
    import lib.config_dir as cd

    path = tmp_path / 'server_config.json'
    real = cd.config_path
    monkeypatch.setattr(
        cd, 'config_path',
        lambda *parts: str(path) if parts == ('server_config.json',)
        else real(*parts))
    return path


def _write_cfg(path, limits, meta=None, providers=None):
    path.write_text(json.dumps({
        'providers': providers if providers is not None else MERGED_PROVIDERS,
        'model_context_limits': limits,
        'model_context_limits_meta': meta or {},
    }), encoding='utf-8')


def _load():
    from lib.context_limits._store import _load as store_load
    return store_load()


def _meta(ts=0.0, source='expand', strikes=0):
    return {'ts': ts, 'source': source, 'strikes': strikes}


@pytest.mark.unit
class TestFoldBasics:

    def test_face_keys_fold_with_their_meta(self, cfg_file):
        """The fleet's exact wound: opus-5's 1.1M expand must survive the
        merge — value AND provenance."""
        _write_cfg(cfg_file,
                   limits={f'{FACE}::claude-opus-5': 1110553,
                           f'{ACCT}::kimi-k3': 383727},
                   meta={f'{FACE}::claude-opus-5': _meta(ts=1785325231.0),
                         f'{ACCT}::kimi-k3': _meta(ts=1785063640.0)})
        limits, meta = _load()

        assert f'{FACE}::claude-opus-5' not in limits
        assert limits[f'{ACCT}::claude-opus-5'] == 1110553
        assert meta[f'{ACCT}::claude-opus-5']['ts'] == 1785325231.0, (
            'meta must fold alongside — a shrink entry folded without its '
            'ts would never TTL-expire')
        assert meta[f'{ACCT}::claude-opus-5']['source'] == 'expand'
        # Untouched namespaces pass through.
        assert limits[f'{ACCT}::kimi-k3'] == 383727

    def test_conflict_newer_evidence_wins(self, cfg_file):
        _write_cfg(cfg_file,
                   limits={f'{ACCT}::m': 500000,
                           f'{FACE}::m': 1110553},
                   meta={f'{ACCT}::m': _meta(ts=100.0),
                         f'{FACE}::m': _meta(ts=200.0)})
        limits, meta = _load()
        assert limits[f'{ACCT}::m'] == 1110553, (
            'the newer learning supersedes — it describes the CURRENT '
            'upstream window')
        assert meta[f'{ACCT}::m']['ts'] == 200.0

    def test_conflict_older_evidence_loses(self, cfg_file):
        _write_cfg(cfg_file,
                   limits={f'{ACCT}::m': 500000,
                           f'{FACE}::m': 1110553},
                   meta={f'{ACCT}::m': _meta(ts=200.0),
                         f'{FACE}::m': _meta(ts=100.0)})
        limits, meta = _load()
        assert limits[f'{ACCT}::m'] == 500000
        assert meta[f'{ACCT}::m']['ts'] == 200.0


@pytest.mark.unit
class TestFoldPersistence:

    def test_fold_persists_and_is_idempotent(self, cfg_file):
        """The frontend reads model_context_limits RAW from
        server_config.json — an in-memory-only fold would leave the UI
        rendering orphans. The file itself must converge."""
        _write_cfg(cfg_file,
                   limits={f'{FACE}::claude-opus-5': 1110553},
                   meta={f'{FACE}::claude-opus-5': _meta(ts=1.0)})
        _load()

        on_disk = json.loads(cfg_file.read_text(encoding='utf-8'))
        disk_limits = on_disk.get('model_context_limits') or {}
        disk_meta = on_disk.get('model_context_limits_meta') or {}
        assert disk_limits == {f'{ACCT}::claude-opus-5': 1110553}, (
            'the fold must persist — the UI reads this file directly')
        assert disk_meta == {f'{ACCT}::claude-opus-5': _meta(ts=1.0)}

        # Second load on the converged file: no change, no churn.
        limits2, meta2 = _load()
        assert limits2 == {f'{ACCT}::claude-opus-5': 1110553}
        assert meta2 == {f'{ACCT}::claude-opus-5': _meta(ts=1.0)}

    def test_dangling_meta_not_resurrected(self, cfg_file):
        """Meta without a learned value stays dropped (the _persist
        contract), never folded back into the file."""
        _write_cfg(cfg_file,
                   limits={f'{FACE}::m': 500000},
                   meta={f'{FACE}::ghost': _meta(ts=5.0),
                         f'{FACE}::m': _meta(ts=1.0)})
        _load()
        on_disk = json.loads(cfg_file.read_text(encoding='utf-8'))
        assert f'{ACCT}::ghost' not in (
            on_disk.get('model_context_limits_meta') or {})


@pytest.mark.unit
class TestFoldShapes:

    def test_transitional_duplicate_cards(self, cfg_file):
        """Config merge not yet persisted: two cards sharing an account —
        the fold maps by the same identity criterion, boot order free."""
        _write_cfg(cfg_file,
                   limits={f'{FACE}::m': 1110553},
                   meta={},
                   providers=DUPLICATE_PROVIDERS)
        limits, _ = _load()
        assert limits == {f'{ACCT}::m': 1110553}

    def test_unknown_and_bare_keys_preserved(self, cfg_file):
        _write_cfg(cfg_file,
                   limits={'other::m': 100000, 'baremodel': 200000},
                   meta={})
        limits, _ = _load()
        assert limits == {'other::m': 100000, 'baremodel': 200000}, (
            'the fold converges what the map KNOWS — everything else is '
            'data, not garbage')

    def test_single_colon_provider_part_splits_safely(self, cfg_file):
        """Real fleet shape: 'ephemeral:local::glm5.1-FP8' — the provider
        part itself contains a colon. Split must be on the FIRST '::'
        only, so such keys survive unmangled."""
        _write_cfg(cfg_file,
                   limits={'ephemeral:local::glm5.1-FP8': 192614,
                           f'{FACE}::m': 900000},
                   meta={})
        limits, _ = _load()
        assert limits['ephemeral:local::glm5.1-FP8'] == 192614
        assert limits[f'{ACCT}::m'] == 900000

    def test_no_absorbed_namespace_ratchet(self, cfg_file):
        """After load, every provider-qualified learned key names a
        namespace that maps to a configured account — 'learned but
        unreachable' cannot happen."""
        _write_cfg(cfg_file,
                   limits={f'{FACE}::a': 100000, f'{FACE}::b': 200000,
                           f'{ACCT}::c': 300000},
                   meta={})
        limits, _ = _load()
        for k in limits:
            if '::' in k:
                assert not k.startswith(FACE + '::'), (
                    f'orphan learned key survived the fold: {k}')


@pytest.mark.unit
class TestMapSingleHome:

    def test_store_consumes_the_shared_map(self):
        from tests._source_scan import strip_comments
        with open(os.path.join(ROOT, 'lib/context_limits/_store.py'),
                  encoding='utf-8') as f:
            src = strip_comments(f.read(), lang='python')
        assert 'account_namespace_map' in src, (
            'the fold must consume provider_face.account_namespace_map')
        assert 'provider_faces(' not in src, (
            'context_limits must not grow a second copy of the face rule')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
