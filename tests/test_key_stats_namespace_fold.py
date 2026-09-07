#!/usr/bin/env python3
"""tests/test_key_stats_namespace_fold.py — face/duplicate-card namespaces
fold into their ACCOUNT namespace at key_stats load time.

The 2026-07-29 invisible-total-outage: key-health history was recorded per
FACE card (``sankuai_anthropic::…`` — the duplicate provider card an
Anthropic wire used to require), while the Settings UI renders one card per
ACCOUNT (``sankuai::…``) after the account/face separation (charter #23).
Result: all three Opus-5 keys auto-stopped on the anthropic face and the
settings panel showed 87–93% healthy green; a PERSISTENT manual re-enable
written against the face namespace (the 22:20 incident fix) controlled
nothing the account-namespace dispatcher checks, and rendered nowhere.

The account/face merge (547827e1) stops NEW recordings from using the face
namespace, but it does nothing about the state already sitting in
``data/config/key_stats.json``: day-scoped stops (a folded 402 would
otherwise be re-burned live once per model) and PERSISTENT overrides (they
survive day rollovers by design — orphaned, they'd silently stop applying).

Pinned here:

  1. Stats under a face namespace fold into the account entry — counters
     SUM, the 429 streak takes MAX, ``exhausted`` ORs, per-model stops
     UNION, an empty account ``last_error`` inherits the face's.
  2. A face-only entry MOVES byte-identically when the account has none.
  3. Folded stops actually gate dispatch: key-wide exhausted and a
     per-model billing-stop both answer ``is_key_enabled`` correctly on
     the account namespace after the fold.
  4. Override migration: a face-only override MOVES to the account; when
     both exist the ACCOUNT's explicit user decision wins (it is the
     surviving namespace — the one the UI toggle writes today).
  5. The fold PERSISTS (the file loses the face namespace) and is
     idempotent across reloads.
  6. Transitional installs (duplicate cards still in config — the
     dispatcher's config merge hasn't run yet) map by the SAME
     account-identity criterion the merge uses; boot ordering is
     irrelevant.
  7. Ratchet: after load, ``get_all_stats`` exposes NO namespace without
     a configured provider card — "invisible to Settings" cannot happen.
  8. Namespaces the map does NOT know are preserved untouched — the fold
     converges state, it never silently deletes history.
  9. Structural (charter #24, comment-stripped): the namespace map lives
     in provider_face.py (the account/face home); key_stats consumes it
     rather than growing a second copy of the face-shape rule.

Run:  pytest tests/test_key_stats_namespace_fold.py -m unit
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

#: The merged single-card shape (what the fleet's config looks like AFTER
#: the account/face merge persisted): one account, its anthropic wire
#: declared under ``faces{}``.
MERGED_PROVIDERS = [
    {'id': ACCT,
     'base_url': 'https://gw.example.com/v1/openai',
     'api_keys': ['ak0', 'ak1'],
     'faces': {'anthropic': {
         'base_url': 'https://gw.example.com/v1/anthropic',
         'protocol': 'anthropic'}}},
]

#: The pre-merge shape (duplicate cards, merge not yet run): the anthropic
#: card shares the account identity (host + key set) — the SAME criterion
#: merge_duplicate_account_faces folds by.
DUPLICATE_PROVIDERS = [
    {'id': ACCT, 'base_url': 'https://gw.example.com/v1/openai',
     'api_keys': ['ak0', 'ak1']},
    {'id': FACE, 'base_url': 'https://gw.example.com/v1/anthropic',
     'protocol': 'anthropic', 'api_keys': ['ak0', 'ak1']},
]


def _entry(**kw):
    base = {'success': 0, 'failure': 0, 'rate_limited': 0,
            'consecutive_429': 0, 'last_error': '', 'exhausted': False,
            'exhausted_models': {}}
    base.update(kw)
    return base


@pytest.fixture
def fresh_stats(monkeypatch, tmp_path):
    """Isolate lib.key_stats on a tmp stats file + merged-config seam."""
    import lib
    import lib.key_stats as ks

    snapshot = {
        'day': ks._cache['day'],
        'stats': ks._cache['stats'],
        'overrides': ks._cache['overrides'],
        'loaded': ks._cache['loaded'],
    }
    stats_path = tmp_path / 'key_stats.json'
    monkeypatch.setattr(ks, '_STATS_PATH', str(stats_path))
    monkeypatch.setattr(ks, '_list_siblings',
                        lambda pid: [f'{ACCT}::{ACCT}_key_0',
                                     f'{ACCT}::{ACCT}_key_1'])
    monkeypatch.setattr(lib, '_load_server_config',
                        lambda: {'providers': MERGED_PROVIDERS})
    ks._cache['day'] = ''
    ks._cache['stats'] = {}
    ks._cache['overrides'] = {}
    ks._cache['loaded'] = False
    yield ks, stats_path
    ks._cache['day'] = snapshot['day']
    ks._cache['stats'] = snapshot['stats']
    ks._cache['overrides'] = snapshot['overrides']
    ks._cache['loaded'] = snapshot['loaded']


def _seed(stats_path, stats=None, overrides=None):
    from datetime import date
    stats_path.write_text(json.dumps({
        # The REAL today — any other value trips the day-rollover branch in
        # _load_unlocked, which RESETS stats (overrides survive) and turns
        # every stats assertion below into a self-inflicted empty-dict.
        'day': date.today().isoformat(),
        'stats': stats or {},
        'overrides': overrides or {},
    }), encoding='utf-8')


def _load(ks):
    with ks._lock:
        ks._load_unlocked()


@pytest.mark.unit
class TestStatsFold:

    def test_fold_merges_into_existing_account_entry(self, fresh_stats):
        ks, stats_path = fresh_stats
        _seed(stats_path, stats={
            f'{ACCT}::{ACCT}_key_0': _entry(
                success=10, failure=2, rate_limited=1, consecutive_429=4),
            f'{FACE}::{FACE}_key_0': _entry(
                success=3, failure=1, rate_limited=5, consecutive_429=2,
                last_error='HTTP 429 storm'),
        })
        _load(ks)

        stats = ks._cache['stats']
        assert not any(k.startswith(FACE + '::') for k in stats), (
            'the face namespace must be gone from the store after the fold')
        row = stats[f'{ACCT}::{ACCT}_key_0']
        assert row['success'] == 13, 'counters must SUM across faces'
        assert row['failure'] == 3
        assert row['rate_limited'] == 6
        assert row['consecutive_429'] == 4, 'a streak is a max, not a sum'
        assert row['last_error'] == 'HTTP 429 storm', (
            'an empty account last_error inherits the face one')

    def test_fold_moves_entry_when_account_has_none(self, fresh_stats):
        ks, stats_path = fresh_stats
        face_row = _entry(success=7, failure=2, rate_limited=9,
                          consecutive_429=3, last_error='boom',
                          exhausted=True,
                          exhausted_models={'claude-opus-5': '402 credit'})
        _seed(stats_path, stats={f'{FACE}::{FACE}_key_1': dict(face_row)})
        _load(ks)

        stats = ks._cache['stats']
        assert f'{FACE}::{FACE}_key_1' not in stats
        assert stats.get(f'{ACCT}::{ACCT}_key_1') == face_row, (
            'a face-only entry must MOVE byte-identically')

    def test_key_wide_stop_gates_account_key_after_fold(self, fresh_stats):
        ks, stats_path = fresh_stats
        _seed(stats_path, stats={
            f'{FACE}::{FACE}_key_0': _entry(exhausted=True,
                                            last_error='HTTP 402'),
            f'{ACCT}::{ACCT}_key_1': _entry(success=5),  # healthy sibling
        })
        _load(ks)

        assert ks.is_key_enabled(ACCT, f'{ACCT}_key_0') is False, (
            'a folded key-wide stop must keep gating dispatch — losing it '
            'would burn a live 402 per model before re-stopping')

    def test_per_model_stop_gates_after_fold(self, fresh_stats):
        ks, stats_path = fresh_stats
        _seed(stats_path, stats={
            f'{FACE}::{FACE}_key_0': _entry(
                exhausted_models={'claude-opus-5': '您的Credit已耗尽'}),
        })
        _load(ks)

        assert ks.is_key_enabled(ACCT, f'{ACCT}_key_0',
                                 model='claude-opus-5') is False
        assert ks.is_key_enabled(ACCT, f'{ACCT}_key_0',
                                 model='kimi-k3') is True, (
            'the folded stop must stay per-model — sibling models are not '
            'poisoned (aggregating-gateway isolation unchanged)')


@pytest.mark.unit
class TestOverrideFold:

    def test_face_only_override_moves_to_account(self, fresh_stats):
        ks, stats_path = fresh_stats
        _seed(stats_path, overrides={f'{FACE}::{FACE}_key_0': True})
        _load(ks)

        ov = ks._cache['overrides']
        assert ov == {f'{ACCT}::{ACCT}_key_0': True}, (
            'a PERSISTENT manual decision must not be orphaned onto a '
            'namespace nothing reads — the 22:20 manual re-enable was '
            'exactly this row')

    def test_account_override_wins_over_face(self, fresh_stats):
        """Both namespaces carry an override → the ACCOUNT's explicit user
        decision wins: it is the surviving namespace, the one the Settings
        toggle writes today."""
        ks, stats_path = fresh_stats
        _seed(stats_path, overrides={f'{ACCT}::{ACCT}_key_0': False,
                                     f'{FACE}::{FACE}_key_0': True})
        _load(ks)

        assert ks._cache['overrides'] == {f'{ACCT}::{ACCT}_key_0': False}


@pytest.mark.unit
class TestFoldMechanics:

    def test_fold_persists_and_is_idempotent(self, fresh_stats):
        ks, stats_path = fresh_stats
        _seed(stats_path,
              stats={f'{FACE}::{FACE}_key_0': _entry(success=4)},
              overrides={f'{FACE}::{FACE}_key_0': True})
        _load(ks)

        on_disk = json.loads(stats_path.read_text(encoding='utf-8'))
        assert not any(k.startswith(FACE + '::')
                       for k in on_disk.get('stats') or {}), (
            'the fold must PERSIST — otherwise it re-runs (and re-merges) '
            'on every load')
        assert not any(k.startswith(FACE + '::')
                       for k in on_disk.get('overrides') or {})

        # Second load from the folded file: nothing changes.
        before = (dict(ks._cache['stats']), dict(ks._cache['overrides']))
        ks._cache['loaded'] = False
        _load(ks)
        assert (dict(ks._cache['stats']), dict(ks._cache['overrides'])) == before

    def test_transitional_duplicate_cards_fold_by_account_identity(
            self, fresh_stats, monkeypatch):
        """Boot-ordering robustness: the dispatcher's CONFIG merge may not
        have run yet (duplicate cards still configured). The fold must map
        by the SAME account-identity criterion the merge uses — not by
        waiting for the merge."""
        import lib
        monkeypatch.setattr(lib, '_load_server_config',
                            lambda: {'providers': DUPLICATE_PROVIDERS})
        ks, stats_path = fresh_stats
        _seed(stats_path, stats={
            f'{FACE}::{FACE}_key_0': _entry(success=6)})
        _load(ks)

        assert f'{ACCT}::{ACCT}_key_0' in ks._cache['stats']
        assert not any(k.startswith(FACE + '::')
                       for k in ks._cache['stats'])

    def test_unknown_namespaces_preserved(self, fresh_stats):
        """The fold converges what the map KNOWS — an unrelated namespace
        (a genuinely removed provider) is data, not garbage: preserved."""
        ks, stats_path = fresh_stats
        _seed(stats_path,
              stats={'other::other_key_0': _entry(success=2)},
              overrides={'other::other_key_0': False})
        _load(ks)

        assert ks._cache['stats'].get('other::other_key_0'), (
            'namespaces the map does not know must survive — the fold '
            'converges state, it never silently deletes history')
        assert ks._cache['overrides'].get('other::other_key_0') is False

    def test_no_orphan_namespace_ratchet(self, fresh_stats):
        """THE invariant from the incident: after load, every namespace in
        the stats snapshot names a configured provider card — nothing the
        Settings UI cannot render may remain."""
        ks, stats_path = fresh_stats
        _seed(stats_path,
              stats={f'{FACE}::{FACE}_key_0': _entry(success=1),
                     f'{ACCT}::{ACCT}_key_0': _entry(success=2)},
              overrides={f'{FACE}::{FACE}_key_0': True})
        _load(ks)

        configured = {ACCT}
        snap = ks.get_all_stats()
        namespaces = {pk.split('::', 1)[0] for pk in snap['keys']}
        assert namespaces <= configured | {'default'}, (
            f'orphan namespaces visible to no card: '
            f'{namespaces - configured - {"default"}}')


@pytest.mark.unit
class TestMapSingleHome:
    """Charter #24/#12: the face→account rule lives ONCE, in the
    account/face module — key_stats consumes it, never re-derives it."""

    def _stripped(self, relpath):
        from tests._source_scan import strip_comments
        with open(os.path.join(ROOT, relpath), encoding='utf-8') as f:
            return strip_comments(f.read(), lang='python')

    def test_map_lives_in_provider_face(self):
        src = self._stripped('lib/llm_dispatch/provider_face.py')
        assert 'def account_namespace_map' in src, (
            'the face→account namespace map must live in the account/face '
            'module — its single home')

    def test_key_stats_consumes_not_reimplements(self):
        for relpath in ('lib/key_stats/_state.py', 'lib/key_stats/_record.py',
                        'lib/key_stats/_query.py', 'lib/key_stats/_enable.py'):
            src = self._stripped(relpath)
            assert 'provider_faces(' not in src, (
                f'{relpath} derives faces itself — a second copy of the '
                f'face rule that WILL drift from provider_face')
        state = self._stripped('lib/key_stats/_state.py')
        assert 'account_namespace_map' in state, (
            'key_stats must consume the shared map at fold time')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
