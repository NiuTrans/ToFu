"""A cacheable Claude round with no cache activity is never ``no_break``.

Claude caches only at explicit ``cache_control`` markers. On 2026-11-03 a
client regression sent requests with zero markers: every round reported
cache_read=0 and cache_write=0 and paid the base input rate for its whole
prompt. No break gate can fire on that shape (each needs a read drop or a
large write), so the ledger filed those rounds as ``no_break`` next to genuine
hits. The rounds are now recorded as ``uncached`` with the wire-marker evidence
that names the responsible side, while the detector's return value stays
``None``: telemetry changes, product behaviour does not.
"""

from __future__ import annotations

import logging

import pytest

import lib.tasks_pkg.cache_tracking._detect as det
from lib.tasks_pkg.cache_tracking._policy import (
    BUCKET_NO_BREAK,
    BUCKET_UNCACHED,
    MIN_CACHEABLE_PROMPT_TOKENS,
    classify_verdict,
)
from lib.tasks_pkg.wire_fingerprint import marker_signature

pytestmark = pytest.mark.unit

_PROMPT = 120_000
_MARKED_BODY = {
    'system': [{'type': 'text', 'text': 'sys',
                'cache_control': {'type': 'ephemeral'}}],
    'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'x', 'cache_control': {'type': 'ephemeral'}}]}],
}
_UNMARKED_BODY = {
    'system': [{'type': 'text', 'text': 'sys'}],
    'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'x'}]}],
}


def _drive(monkeypatch, rounds, *, model='claude-opus-5', prompt=_PROMPT,
           markers=None):
    """Feed ``(cache_read, cache_write)`` rounds through the real detector.

    Returns ``(buckets, returned)``: the bucket each emitted round record
    carried and the detector's return value for each round.
    """
    seen = []
    real_emit = det._emit_round_record

    def _spy(conv_id, call_num, verdict, **kw):
        seen.append(classify_verdict(verdict))
        return real_emit(conv_id, call_num, verdict, **kw)

    monkeypatch.setattr(det, '_emit_round_record', _spy)
    conv = f'conv-uncached-{id(rounds)}-{model}-{prompt}'
    returned = []
    for read, write in rounds:
        # Anthropic convention: input_tokens excludes cached tokens.
        usage = {'input_tokens': prompt - read - write, 'output_tokens': 10,
                 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': write}
        if markers is not None:
            usage['_wire_markers'] = markers
        returned.append(det.detect_cache_break(
            conv, [{'role': 'user', 'content': 'x'}], None, model, usage,
            user_id=1))
    return seen, returned


def test_uncached_verdict_has_its_own_bucket():
    assert classify_verdict({'uncached': 'no cache activity'}) == BUCKET_UNCACHED
    assert classify_verdict(None) == BUCKET_NO_BREAK


def test_zero_marker_regression_is_recorded_as_uncached(monkeypatch, caplog):
    """The 2026-11-03 shape: no marker on the wire, nothing read or written."""
    caplog.set_level(logging.WARNING, logger=det.logger.name)
    buckets, returned = _drive(monkeypatch, [(0, 0)] * 3,
                               markers=marker_signature(_UNMARKED_BODY))
    assert buckets == [BUCKET_NO_BREAK, BUCKET_UNCACHED, BUCKET_UNCACHED]
    assert returned == [None, None, None]
    warnings = [r.getMessage() for r in caplog.records if 'UNCACHED' in r.getMessage()]
    assert len(warnings) == 2
    assert 'no cache_control marker reached the wire' in warnings[0]


def test_markers_on_the_wire_implicate_the_provider_side(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=det.logger.name)
    buckets, _ = _drive(monkeypatch, [(0, 0)] * 2,
                        markers=marker_signature(_MARKED_BODY))
    assert buckets[1] == BUCKET_UNCACHED
    warning = next(r.getMessage() for r in caplog.records
                   if 'UNCACHED' in r.getMessage())
    assert '2 cache_control marker(s) were on the wire' in warning


def test_missing_wire_markers_stay_unattributed(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=det.logger.name)
    buckets, _ = _drive(monkeypatch, [(0, 0)] * 2)
    assert buckets[1] == BUCKET_UNCACHED
    warning = next(r.getMessage() for r in caplog.records
                   if 'UNCACHED' in r.getMessage())
    assert 'cannot be told apart' in warning


def test_healthy_reuse_is_never_uncached(monkeypatch):
    buckets, _ = _drive(monkeypatch, [(0, 100_000), (100_000, 5_000),
                                      (105_000, 4_000)])
    assert BUCKET_UNCACHED not in buckets


def test_a_round_that_only_writes_is_not_uncached(monkeypatch):
    """A zero-read round that wrote its prefix used the cache; the existing
    break gates own it."""
    buckets, _ = _drive(monkeypatch, [(0, 100_000), (100_000, 5_000),
                                      (0, 110_000)])
    assert BUCKET_UNCACHED not in buckets


def test_first_round_is_a_cold_start(monkeypatch):
    buckets, _ = _drive(monkeypatch, [(0, 0)])
    assert buckets == [BUCKET_NO_BREAK]


def test_non_claude_models_are_out_of_scope(monkeypatch):
    """Automatic-caching providers report zero on legitimately cold prompts."""
    buckets, _ = _drive(monkeypatch, [(0, 0)] * 3, model='kimi-k2')
    assert BUCKET_UNCACHED not in buckets


@pytest.mark.parametrize('prompt, expected', [
    (MIN_CACHEABLE_PROMPT_TOKENS - 1, BUCKET_NO_BREAK),
    (MIN_CACHEABLE_PROMPT_TOKENS, BUCKET_UNCACHED),
])
def test_prompts_below_every_claude_cache_minimum_are_out_of_scope(
        monkeypatch, prompt, expected):
    buckets, _ = _drive(monkeypatch, [(0, 0)] * 2, prompt=prompt)
    assert buckets[1] == expected
