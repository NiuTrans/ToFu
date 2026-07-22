#!/usr/bin/env python3
"""Always-on structured per-round cache-verdict RECORD suite.

WHY (the empirical-evidence gap replay could not close):
  The offline replay harness proved the instrument's LOGIC on synthetic
  sequences + real-body fingerprint extraction, but it could NOT produce a
  real-traffic bucket count: the only offline data source (the default-OFF
  byte-probe dump) records neither ``cache_read``/``cache_write`` nor routing,
  and making it record them needs the new code running = a restart. Circular.

  The fix that ROOT-solves "monitoring is insufficient": ``detect_cache_break``
  emits ONE machine-readable structured record EVERY round (not just on a
  break), tagged ``[CacheRoundRecord]`` with a JSON payload carrying the
  ``bucket`` (from the SINGLE-SOURCE ``classify_verdict`` shared with replay),
  the routing diff, the ttl-flip / breakpoint-lost flags, ``cache_read`` /
  ``cache_write``, and whether the body was byte-identical. After one clean
  deploy the real-traffic bucket count is a ``grep [CacheRoundRecord] | ...``
  aggregation — no probe, no manual replay, no restart-to-verify.

Each behavioural test asserts the emitted record's bucket label; the NEUTER
control proves the bucket is driven by the real fingerprint (drop the routing
capture → the same key flip is no longer labelled a namespace switch).

Run DIRECTLY (env-guarded):
    python tests/test_cache_round_record.py
"""

from __future__ import annotations

import json as _json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


class _RecordCapture:
    """Capture ``[CacheRoundRecord]`` JSON payloads emitted by the detector."""

    def __init__(self):
        self.records: list[dict] = []
        self._handler = None

    def __enter__(self):
        logger = logging.getLogger('lib.tasks_pkg.cache_tracking._detect')
        cap = self

        class _H(logging.Handler):
            def emit(self, rec):
                msg = rec.getMessage()
                if '[CacheRoundRecord]' in msg:
                    try:
                        payload = msg.split('[CacheRoundRecord]', 1)[1].strip()
                        cap.records.append(_json.loads(payload))
                    except (ValueError, IndexError):
                        pass

        self._handler = _H()
        self._handler.setLevel(logging.DEBUG)
        logger.addHandler(self._handler)
        self._prev_level = logger.level
        logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        logger = logging.getLogger('lib.tasks_pkg.cache_tracking._detect')
        if self._handler:
            logger.removeHandler(self._handler)
        logger.setLevel(self._prev_level)


def _body(system_ttl=None, sys_text='STATIC SYSTEM PROMPT'):
    cc = None
    if system_ttl is not None:
        cc = {'type': 'ephemeral'} if system_ttl == '' else {
            'type': 'ephemeral', 'ttl': system_ttl}
    blk = {'type': 'text', 'text': sys_text}
    if cc:
        blk['cache_control'] = cc
    return {
        'model': 'claude-opus-4',
        'system': [blk],
        'tools': [{'function': {'name': 't', 'description': 'd', 'parameters': {}}}],
        'messages': [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'},
        ],
    }


def test_classify_verdict_is_single_source():
    """replay.classify_verdict MUST be the SAME object as the detector's — no
    second copy that could drift (the bug class this whole objective is about)."""
    from lib.tasks_pkg.cache_tracking import classify_verdict as detect_cv
    from lib.tasks_pkg.cache_tracking.replay import classify_verdict as replay_cv
    assert detect_cv is replay_cv, (
        'classify_verdict must be single-source (replay re-exports the '
        'detector function), else offline and live buckets can drift')


def test_round_record_emitted_every_round():
    """A record is emitted on EVERY round — including the baseline round-0 that
    produces no break — so the log stream is a complete per-round ledger."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    rt = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    with _RecordCapture() as cap:
        replay_rounds([
            {'body': b, 'cache_read': 90000, 'cache_write': 50000, 'routing': rt},
            {'body': b, 'cache_read': 88000, 'cache_write': 3000, 'routing': rt},
        ], conv_id='rec-every')
    assert len(cap.records) == 2, (
        f'expected one record per round (2), got {len(cap.records)}')
    for r in cap.records:
        for k in ('bucket', 'routing_diff', 'ttl_flip', 'body_identical',
                  'cache_read', 'cache_write'):
            assert k in r, f'record missing field {k}: {r}'


def test_round_record_labels_namespace_switch():
    """A body-identical key flip is labelled bucket=cache_namespace_switch with
    routing_diff naming the key, in the structured record."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    with _RecordCapture() as cap:
        replay_rounds([
            {'body': b, 'cache_read': 90000, 'cache_write': 50000,
             'routing': {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
            {'body': b, 'cache_read': 40000, 'cache_write': 120000,
             'routing': {'key_hash': 'kB', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
        ], conv_id='rec-nsflip')
    r2 = cap.records[-1]
    assert r2['bucket'] == 'cache_namespace_switch', r2
    assert '<ns>key' in r2['routing_diff'], r2
    assert r2['body_identical'] is True, r2


def test_round_record_labels_ttl_flip():
    """A body-identical marker ttl flip is labelled bucket=ttl_flip, ttl_flip=True."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    rt = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    with _RecordCapture() as cap:
        replay_rounds([
            {'body': _body(system_ttl='1h'), 'cache_read': 90000,
             'cache_write': 50000, 'routing': rt},
            {'body': _body(system_ttl=''), 'cache_read': 40000,
             'cache_write': 120000, 'routing': rt},
        ], conv_id='rec-ttlflip')
    r2 = cap.records[-1]
    assert r2['bucket'] == 'ttl_flip', r2
    assert r2['ttl_flip'] is True, r2

    # The RAW wire-culprit tokens must be surfaced so a post-deploy live A/B can
    # see the ACTUAL driver of a break, not only the final bucket (the
    # disambiguation the mid_oow-misattribution investigation needed).
    assert 'culprits' in r2, f'record must carry the raw culprit tokens: {r2}'
    assert any('ttl-flip' in c for c in r2['culprits']), (
        f'a ttl-flip break must name <ttl-flip> in culprits: {r2}')


def test_round_record_labels_upstream_identical():
    """Body + routing + markers all identical → bucket=upstream_identical and
    namespace_verified=True (the only bucket allowed to be called upstream)."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    rt = {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}
    with _RecordCapture() as cap:
        replay_rounds([
            {'body': b, 'cache_read': 90000, 'cache_write': 50000, 'routing': rt},
            {'body': b, 'cache_read': 40000, 'cache_write': 120000, 'routing': rt},
        ], conv_id='rec-upstream')
    r2 = cap.records[-1]
    assert r2['bucket'] == 'upstream_identical', r2
    assert r2['namespace_verified'] is True, r2


def test_round_record_NEUTER_without_routing_not_namespace():
    """NEUTER — the record's bucket is driven by the real fingerprint. The SAME
    key flip, but with routing capture disabled, is NOT labelled a namespace
    switch (it launders to upstream), proving the record reflects the actual
    captured signal rather than a hard-coded guess."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    b = _body(system_ttl='1h')
    with _RecordCapture() as cap:
        replay_rounds([
            {'body': b, 'cache_read': 90000, 'cache_write': 50000,
             'routing': {'key_hash': 'kA', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
            {'body': b, 'cache_read': 40000, 'cache_write': 120000,
             'routing': {'key_hash': 'kB', 'anthropic_beta': 'pc', 'endpoint': 'e1'}},
        ], conv_id='rec-neuter', capture_routing=False)
    r2 = cap.records[-1]
    assert r2['bucket'] != 'cache_namespace_switch', (
        f'NEUTER: without routing capture the flip must not be labelled a '
        f'namespace switch — got {r2}')
    assert r2['bucket'] == 'upstream_identical', r2


def test_aggregate_round_records_from_log_lines():
    """The offline aggregator tallies buckets from a stream of [CacheRoundRecord]
    log lines — the post-deploy 'grep | count' the objective needs."""
    from lib.tasks_pkg.cache_tracking.replay import aggregate_round_records

    lines = [
        'ts [INFO] ...: [CacheRoundRecord] {"bucket":"upstream_identical","cache_read":1}',
        'ts [INFO] ...: [CacheRoundRecord] {"bucket":"cache_namespace_switch"}',
        'ts [INFO] ...: [CacheRoundRecord] {"bucket":"upstream_identical"}',
        'ts [INFO] ...: [CacheRoundRecord] {"bucket":"ttl_flip"}',
        'unrelated line, ignored',
        'ts [INFO] ...: [CacheRoundRecord] {"bucket":"no_break"}',
    ]
    counts = aggregate_round_records(lines)
    assert counts['upstream_identical'] == 2, counts
    assert counts['cache_namespace_switch'] == 1, counts
    assert counts['ttl_flip'] == 1, counts
    assert counts['no_break'] == 1, counts


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
