#!/usr/bin/env python3
"""Offline cache-verdict REPLAY harness suite.

WHY THIS EXISTS (the restart deadlock):
  The always-on namespace + marker-ttl fingerprints (commits 815bd66 / 830d676)
  live in the RUNNING process's imported bytecode. Verifying them against live
  traffic needs an ``os.execv`` restart — but this is a multi-sibling concurrent
  environment where a restart is refused (HTTP 409) unless ``force=true``, and a
  forced restart kills EVERY in-flight task INCLUDING the verification
  conversation itself (the verifier commits suicide). That is a structural
  deadlock, not a flaky click.

  The robust engineering answer is an OFFLINE REPLAY: reconstruct the detector's
  per-round input from a captured/synthetic sequence of post-translation bodies,
  feed each round through the SAME production fingerprint functions
  (routing_fingerprint / marker_signature / canonical_messages / wire_byte_*)
  and the SAME ``detect_cache_break``, and classify the verdicts into hard-count
  buckets — with ZERO restart and ZERO task-kill. It doubles as a permanent
  regression asset and a live-equivalent acceptance instrument.

  ``build_round_usage(body, cache_read, cache_write, routing)`` mirrors EXACTLY
  what ``lib/llm/_sse_core.py`` assembles into ``usage`` at send time (the same
  ``_wire_fp`` / ``_wire_static`` / ``_wire_bytes`` / ``_wire_field_bytes`` /
  ``_wire_region`` / ``_wire_markers`` / ``_wire_system`` / ``_wire_routing``
  keys), so a replayed round is byte-for-byte the input the live detector would
  have seen. ``replay_rounds(rounds, user_id=...)`` drives
  ``detect_cache_break`` across the
  sequence and returns per-round verdicts + a bucket tally.

Buckets (the objective's classification):
  - ``cache_namespace_switch`` — body identical, routing (key/beta/endpoint) flip
  - ``ttl_flip``               — body identical, marker cache_control.ttl flip
  - ``breakpoint_lost``        — body identical, a breakpoint disappeared
  - ``upstream_identical``     — body AND routing AND markers all identical (the
                                 ONLY bucket allowed to be called upstream)
  - ``body_change``            — a real client body/prefix mutation
  - ``no_break``               — round produced no break verdict

Run DIRECTLY (env-guarded):
    python tests/test_cache_replay.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _sys_block(text, ttl=None):
    cc = None
    if ttl is not None:
        cc = {'type': 'ephemeral'} if ttl == '' else {'type': 'ephemeral', 'ttl': ttl}
    blk = {'type': 'text', 'text': text}
    if cc:
        blk['cache_control'] = cc
    return blk


def _body(system_ttl=None, sys_text='STATIC SYSTEM PROMPT'):
    """A minimal post-translation Anthropic-shape body. The system block carries
    a cache_control marker whose ttl is `system_ttl` ('' bare/5m, '1h' extended,
    None = no marker). The message content is fixed so the BODY bytes are
    identical across rounds regardless of the marker ttl."""
    return {
        'model': 'claude-opus-4',
        'system': [_sys_block(sys_text, ttl=system_ttl)],
        'tools': [{'function': {'name': 't', 'description': 'd', 'parameters': {}}}],
        'messages': [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'},
        ],
    }


def test_build_round_usage_mirrors_sse_core_keys():
    """build_round_usage must populate the SAME wire keys _sse_core relays into
    usage, so a replayed round is the exact input the live detector sees."""
    from lib.tasks_pkg.cache_tracking.replay import build_round_usage

    u = build_round_usage(_body(system_ttl='1h'), cache_read=50000,
                          cache_write=10000,
                          routing={'key_hash': 'k', 'anthropic_beta': 'b',
                                   'endpoint': 'e'})
    for k in ('_wire_fp', '_wire_static', '_wire_bytes', '_wire_field_bytes',
              '_wire_region', '_wire_markers', '_wire_system', '_wire_routing',
              'cache_read_tokens', 'cache_creation_input_tokens'):
        assert k in u, f'build_round_usage missing {k} (must mirror _sse_core)'
    # routing fingerprint carries the three namespace attributes
    assert set(u['_wire_routing']) == {'key', 'beta', 'endpoint'}
    # marker signature captured the system marker's ttl
    assert u['_wire_markers'].get('ttls'), 'marker ttl not captured'


def test_replay_classifies_namespace_switch_bucket():
    """A body-identical sequence whose KEY flips round 2 → the round lands in
    the cache_namespace_switch bucket (client-caused), never upstream."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    rounds = [
        {'body': b, 'cache_read': 90000, 'cache_write': 50000,
         'routing': {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
        {'body': b, 'cache_read': 40000, 'cache_write': 120000,
         'routing': {'key_hash': 'kB', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-nsflip')
    assert res['buckets'].get('cache_namespace_switch', 0) == 1, res['buckets']
    assert res['buckets'].get('upstream_identical', 0) == 0, res['buckets']


def test_replay_classifies_ttl_flip_bucket():
    """Body identical, marker ttl flips 1h→5m round 2 → ttl_flip bucket."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    same_routing = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    rounds = [
        {'body': _body(system_ttl='1h'), 'cache_read': 90000, 'cache_write': 50000,
         'routing': same_routing},
        {'body': _body(system_ttl=''), 'cache_read': 40000, 'cache_write': 120000,
         'routing': same_routing},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-ttlflip')
    assert res['buckets'].get('ttl_flip', 0) == 1, res['buckets']
    assert res['buckets'].get('upstream_identical', 0) == 0, res['buckets']


def test_replay_classifies_upstream_identical_bucket():
    """Body + routing + markers ALL identical, yet cache_read drops → the ONLY
    bucket allowed to be called upstream. Verdict must carry the evidence
    string, not blame the client."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    same_routing = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    rounds = [
        {'body': b, 'cache_read': 90000, 'cache_write': 50000, 'routing': same_routing},
        {'body': b, 'cache_read': 40000, 'cache_write': 120000, 'routing': same_routing},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-upstream')
    assert res['buckets'].get('upstream_identical', 0) == 1, res['buckets']
    assert res['buckets'].get('cache_namespace_switch', 0) == 0
    assert res['buckets'].get('ttl_flip', 0) == 0
    # the round-2 verdict must be the evidence-grade upstream wording
    r2 = res['rounds'][1]['verdict']
    assert r2 is not None
    import json as _j
    blob = _j.dumps(r2).lower()
    assert 'identical' in blob and 'upstream cache miss' in blob, r2


def test_replay_classifies_body_change_bucket():
    """A real prefix body mutation round 2 → body_change bucket (existing
    client-culprit path), not upstream, not namespace."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    same_routing = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    rounds = [
        {'body': _body(system_ttl='1h', sys_text='STATIC A'),
         'cache_read': 90000, 'cache_write': 50000, 'routing': same_routing},
        {'body': _body(system_ttl='1h', sys_text='STATIC B — MUTATED'),
         'cache_read': 40000, 'cache_write': 120000, 'routing': same_routing},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-bodychange')
    assert res['buckets'].get('upstream_identical', 0) == 0, res['buckets']
    assert res['buckets'].get('cache_namespace_switch', 0) == 0, res['buckets']
    # the mutated system prefix must be caught as a client body change
    assert res['buckets'].get('body_change', 0) == 1, res['buckets']


def test_replay_NEUTER_without_routing_launders_nsflip_to_upstream():
    """NEUTER — proves the replay's routing relay is load-bearing. The SAME
    key-flip sequence, but with routing capture disabled (mirrors a pre-fix
    process): the namespace switch is NOT named and the round launders into the
    upstream bucket."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    rounds = [
        {'body': b, 'cache_read': 90000, 'cache_write': 50000,
         'routing': {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
        {'body': b, 'cache_read': 40000, 'cache_write': 120000,
         'routing': {'key_hash': 'kB', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
    ]
    res = replay_rounds(
        rounds, user_id=7, conv_id='replay-neuter', capture_routing=False)
    assert res['buckets'].get('cache_namespace_switch', 0) == 0, (
        f'NEUTER: without routing capture the key flip must NOT be named — {res["buckets"]}')
    assert res['buckets'].get('upstream_identical', 0) == 1, res['buckets']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
