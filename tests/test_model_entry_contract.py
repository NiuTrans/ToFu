#!/usr/bin/env python3
"""tests/test_model_entry_contract.py — the model-identity contract.

``model_id`` is the LOGICAL name (presets / picker / persisted conversations);
``request_ids`` is the ordered POOL of ids actually put on the wire. See
``lib/llm_dispatch/model_entry.py``.

What is pinned here
-------------------
1. Pool resolution, including the compatibility rule that a LEGACY entry
   (``aliases``, no ``request_ids``) keeps its root ``model_id`` in the pool.
   This is the assertion that catches the dangerous half-migration: reading
   ``aliases`` as *the* pool silently drops one wire deployment, and every
   remaining id still works, so nothing raises.
2. Shipped legacy-import templates carry no provider-tainted spelling as a
   ``model_id``. Runtime routing is covered by model-routing v2 tests.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_model_entry_contract.py -v
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.provider_template_recipes import offering_recipes

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A model_id carrying one of these is naming a DEPLOYMENT, not a model.
_TAINT = re.compile(r'^(aws\.|vertex\.|azure\.|bedrock\.|yuju-)|-evaDaily$|-huawei$'
                    r'|-tencent$|-baidu$|-doubao$|-nova\d+$', re.IGNORECASE)


def _templates() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(glob.glob(os.path.join(
            _ROOT, 'static', 'provider_templates', '*.json'))):
        with open(path, encoding='utf-8') as f:
            out.append((os.path.basename(path), json.load(f)))
    return out


# ═══════════════════════════════════════════════════════════
#  1. Pool resolution
# ═══════════════════════════════════════════════════════════

def test_explicit_pool_is_authoritative():
    """``request_ids`` present ⇒ that list verbatim. The logical model_id is
    NOT auto-appended: a logical-only name must never reach the wire."""
    from lib.llm_dispatch.model_entry import resolve_request_ids
    entry = {'model_id': 'claude-opus-5',
             'request_ids': ['yuju-claude-opus-5-evaDaily', 'aws.claude-opus-5']}
    assert resolve_request_ids(entry) == [
        'yuju-claude-opus-5-evaDaily', 'aws.claude-opus-5']


def test_legacy_alias_entry_keeps_root_in_the_pool():
    """★ The half-migration guard.

    A legacy entry's wire pool is ``[model_id] + aliases``. If the resolver
    ever reads ``aliases`` as *the* pool, this config loses ``aws.claude-opus-4.6``
    — one of three working deployments — with no error anywhere.
    """
    from lib.llm_dispatch.model_entry import resolve_request_ids
    entry = {'model_id': 'aws.claude-opus-4.6',
             'aliases': ['aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6']}
    assert resolve_request_ids(entry) == [
        'aws.claude-opus-4.6', 'aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6']


def test_pool_dedupes_and_preserves_order():
    from lib.llm_dispatch.model_entry import resolve_request_ids
    entry = {'model_id': 'm', 'request_ids': ['b', 'a', 'b', '', '  ', 'a', 'c']}
    assert resolve_request_ids(entry) == ['b', 'a', 'c']


def test_cell_overrides_and_disabled_ids_subtract():
    """A per-key cell may replace the pool; disabled_ids removes concrete ids
    for that key only."""
    from lib.llm_dispatch.model_entry import resolve_request_ids
    entry = {'model_id': 'logical', 'request_ids': ['wire-a', 'wire-b']}
    assert resolve_request_ids(entry, {'request_ids': ['wire-c']}) == ['wire-c']
    assert resolve_request_ids(entry, {'disabled_ids': ['wire-a']}) == ['wire-b']
    # Legacy cell aliases still fold in the root.
    legacy = {'model_id': 'root', 'aliases': ['x']}
    assert resolve_request_ids(legacy, {'aliases': ['y']}) == ['root', 'y']


def test_routing_group_includes_logical_and_every_wire_id():
    """The logical id must be routable even when it never goes on the wire,
    and every wire id must stay routable so conversations persisted against a
    gateway spelling keep resolving."""
    from lib.llm_dispatch.model_entry import routing_group
    entry = {'model_id': 'claude-opus-5',
             'request_ids': ['yuju-claude-opus-5-evaDaily'],
             'key_access': {'0': {'request_ids': ['aws.claude-opus-5'],
                                  'disabled_ids': ['aws.claude-opus-5']}}}
    group = routing_group(entry)
    assert group == {'claude-opus-5', 'yuju-claude-opus-5-evaDaily',
                     'aws.claude-opus-5'}, group


# ═══════════════════════════════════════════════════════════
#  2. Shipped templates use logical model_ids
# ═══════════════════════════════════════════════════════════

def test_shipped_templates_have_no_provider_tainted_model_id():
    bad = []
    for name, tpl in _templates():
        for m in offering_recipes(tpl, allow_legacy=False):
            mid = m.get('model_id') or ''
            if _TAINT.search(mid):
                bad.append('%s: model_id=%r names a deployment — move it into '
                           'request_ids and give the entry a logical id'
                           % (name, mid))
    assert not bad, '\n'.join(bad)


def test_shipped_templates_declare_a_nonempty_pool():
    """Every template model must resolve to at least one wire id."""
    from lib.llm_dispatch.model_entry import resolve_request_ids
    bad = []
    for name, tpl in _templates():
        for m in offering_recipes(tpl, allow_legacy=False):
            if not resolve_request_ids(m):
                bad.append('%s: %r resolves to an EMPTY wire pool'
                           % (name, m.get('model_id')))
    assert not bad, '\n'.join(bad)


def test_split_identity_entries_price_both_channels():
    """Where a logical id differs from its wire ids, BOTH channels must resolve.

    Splitting identity creates two distinct lookups that used to be one:
      * cost keys on the WIRE id (``slot.model`` reaches ``lookup_pricing``),
      * the picker's display name keys on the LOGICAL id
        (``_modelShortName`` → ``_modelPricingCache[model_id].name``).
    A row missing on either side is silent — cost books at $0, or the picker
    shows a raw gateway string. Scoped to split entries on purpose: unsplit
    free/non-chat models (LongCat, embeddings, ASR) have long had no pricing
    row and are not this change's business.
    """
    from lib.llm_dispatch.model_entry import resolve_request_ids
    from lib.pricing import MODEL_PRICING
    missing = []
    for name, tpl in _templates():
        for m in offering_recipes(tpl, allow_legacy=False):
            if m.get('pricing'):
                continue          # entry carries its own provider pricing
            logical = m.get('model_id') or ''
            wire = resolve_request_ids(m)
            if wire == [logical]:
                continue          # identity not split — out of scope
            for mid in [logical] + wire:
                if mid not in MODEL_PRICING:
                    missing.append(
                        '%s: %r (from logical %r) has no MODEL_PRICING row'
                        % (name, mid, logical))
    assert not missing, '\n'.join(missing)


# ═══════════════════════════════════════════════════════════
#  4. NEUTER faces — prove the assertions discriminate
# ═══════════════════════════════════════════════════════════

def test_neuter_taint_detector_flags_known_bad_ids():
    """If _TAINT ever degrades to 'never matches', the template audit becomes a
    tautology. Pin both faces."""
    for bad in ('aws.claude-opus-4.8', 'yuju-claude-opus-5-evaDaily',
                'vertex.claude-opus-4.6', 'glm-5.1-huawei',
                'deepseek-v3.2-tencent', 'aws.claude-opus-4.7-nova04'):
        assert _TAINT.search(bad), bad
    for ok in ('claude-opus-5', 'kimi-k3', 'gemini-3.5-flash', 'glm-5.1',
               'deepseek-v3.2', 'MiniMax-M3', 'hy3-preview'):
        assert not _TAINT.search(ok), ok


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
