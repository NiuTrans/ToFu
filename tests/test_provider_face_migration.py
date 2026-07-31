#!/usr/bin/env python3
"""tests/test_provider_face_migration.py — two cards become one, in CODE.

WHY A MIGRATION AND NOT A HAND-EDIT
===================================
The YourProvider gateway ships as TWO provider cards (``example-corp`` +
``example-corp_anthropic``) in every install that predates the account/face
separation. Hand-editing one machine's ``server_config.json`` would fix that
machine and leave every other install with:

  * two cards for one account (the thing this epic removes), and
  * an ORPHANED ``example-corp_anthropic`` card — once the second template file is
    gone, ``_findMatchingTemplate`` (which matches on exact base_url) can
    never match it again, so "sync from template" silently stops working for
    it.

So the merge runs at load time, for everyone, on the same code path — which
also means the migration is genuinely exercised rather than being a block of
never-run code. Modelled on the existing
``_migrate_provider_extra_headers`` precedent (dispatcher.py): mutate, persist
once, never run again.

WHAT IS PINNED
--------------
  * Two same-account faces merge into ONE card that keeps the OpenAI face as
    default and folds the Anthropic one into ``faces{}``.
  * The Claude roster survives with its wire pools intact, and resolves to
    the anthropic face afterwards (the merge must not strip signatures).
  * Merging is keyed on SAME HOST + SAME KEYS. Different accounts on the same
    host, or the same host with different keys, must NOT be merged.
  * Idempotent: running it twice changes nothing.
  * A single-face install is untouched.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_provider_face_migration.py -v
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.openai.com/v1/anthropic'
KEYS = ['sk-a', 'sk-b', 'sk-c']
HDRS = {'X-Custom-Header': 'your-header-value'}


def _legacy_pair():
    """The shape every existing install has today."""
    return [
        {'id': 'default', 'name': 'Default', 'base_url': OPENAI_URL,
         'extra_headers': dict(HDRS), 'api_keys': list(KEYS), 'enabled': True,
         'models': [{'model_id': 'kimi-k3', 'capabilities': ['text']},
                    {'model_id': 'gemini-3.5-flash', 'capabilities': ['text']}]},
        {'id': 'example-corp_anthropic', 'name': 'YourProvider (Anthropic native)',
         'base_url': ANTHROPIC_URL, 'protocol': 'anthropic',
         'extra_headers': dict(HDRS), 'api_keys': list(KEYS), 'enabled': True,
         'models': [
             {'model_id': 'claude-opus-5', 'capabilities': ['text'],
              'request_ids': ['yuju-claude-opus-5-evaDaily']},
             {'model_id': 'claude-fable-5', 'capabilities': ['text']}]},
    ]


def _migrate(providers):
    from lib.llm_dispatch.provider_face import merge_duplicate_account_faces
    return merge_duplicate_account_faces(providers)


# ═══════════════════════════════════════════════════════════
#  1. The merge
# ═══════════════════════════════════════════════════════════

def test_two_faces_of_one_account_become_one_card():
    provs = _legacy_pair()
    changed = _migrate(provs)
    assert changed is True
    assert len(provs) == 1, [p.get('id') for p in provs]
    card = provs[0]
    assert card['id'] == 'example-corp', 'the default-face card keeps its identity'
    assert card['base_url'] == OPENAI_URL
    assert card['faces']['anthropic']['base_url'] == ANTHROPIC_URL
    assert card['faces']['anthropic']['protocol'] == 'anthropic'


def test_the_claude_roster_survives_with_its_wire_pool():
    provs = _legacy_pair()
    _migrate(provs)
    models = {m['model_id']: m for m in provs[0]['models']}
    assert 'claude-opus-5' in models
    assert 'claude-fable-5' in models
    assert models['claude-opus-5']['request_ids'] == \
        ['yuju-claude-opus-5-evaDaily']
    assert 'kimi-k3' in models, 'default-face models must survive too'


def test_claude_still_resolves_to_the_anthropic_wire_after_merge():
    """The merge is only correct if the end state still routes Claude right —
    this is the property the whole epic exists to preserve."""
    from lib.llm_dispatch.provider_face import resolve_face
    provs = _legacy_pair()
    _migrate(provs)
    card = provs[0]
    for m in card['models']:
        r = resolve_face(card, m)
        assert r.ok, r.error
        if m['model_id'].startswith('claude'):
            assert r.protocol == 'anthropic', m['model_id']
            assert r.base_url == ANTHROPIC_URL, m['model_id']
        else:
            assert r.base_url == OPENAI_URL, m['model_id']


def test_migration_is_idempotent():
    provs = _legacy_pair()
    assert _migrate(provs) is True
    snapshot = copy.deepcopy(provs)
    assert _migrate(provs) is False, 'second run must be a no-op'
    assert provs == snapshot


# ═══════════════════════════════════════════════════════════
#  2. It must not over-merge
# ═══════════════════════════════════════════════════════════

def test_different_keys_on_the_same_host_are_NOT_merged():
    """Same gateway, DIFFERENT accounts — merging would cross-wire billing
    and quota between two tenants."""
    provs = _legacy_pair()
    provs[1]['api_keys'] = ['sk-someone-else']
    assert _migrate(provs) is False
    assert len(provs) == 2


def test_different_hosts_are_NOT_merged():
    provs = _legacy_pair()
    provs[1]['base_url'] = 'https://other.example.com/v1/anthropic'
    assert _migrate(provs) is False
    assert len(provs) == 2


def test_single_face_install_is_untouched():
    provs = [{'id': 'openai', 'base_url': 'https://api.openai.com/v1',
              'api_keys': ['sk-x'], 'enabled': True,
              'models': [{'model_id': 'gpt-4.1-mini'}]}]
    snapshot = copy.deepcopy(provs)
    assert _migrate(provs) is False
    assert provs == snapshot


def test_a_lone_anthropic_card_is_left_alone():
    """Nothing to merge INTO — must not be destroyed or rewritten."""
    provs = [_legacy_pair()[1]]
    snapshot = copy.deepcopy(provs)
    assert _migrate(provs) is False
    assert provs == snapshot


# ═══════════════════════════════════════════════════════════
#  3. NEUTER faces
# ═══════════════════════════════════════════════════════════

def test_neuter_merge_predicate_is_not_a_tautology():
    """If the predicate degraded to 'merge any two providers', the
    over-merge guards above would be the only thing catching it. Assert the
    positive and negative case through the SAME call."""
    ok_case = _legacy_pair()
    assert _migrate(ok_case) is True and len(ok_case) == 1

    bad_case = _legacy_pair()
    bad_case[1]['api_keys'] = ['different']
    assert _migrate(bad_case) is False and len(bad_case) == 2


def test_neuter_key_comparison_ignores_order_but_not_content():
    """Key ORDER is not identity (a user may reorder them); key CONTENT is."""
    provs = _legacy_pair()
    provs[1]['api_keys'] = list(reversed(KEYS))
    assert _migrate(provs) is True, 'reordered identical keys are the same account'

    provs2 = _legacy_pair()
    provs2[1]['api_keys'] = KEYS[:2]
    assert _migrate(provs2) is False, 'a strict subset is NOT the same account'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
