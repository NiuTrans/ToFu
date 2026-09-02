"""TTL expiry must bucket as its OWN verdict — not as a client body change.

WHY THIS EXISTS
===============
``_resolve_break_cause`` has emitted ``'TTL expiry (>5min gap, prompt
unchanged)'`` for any >300s gap since the elapsed-computation fix. But
``classify_verdict`` only ever matched ``'ttl marker flipped'`` and
``'new cache key'`` — it never matched the substring ``'ttl expiry'``.

Net effect measured on real traffic (logs/app.log [CacheRoundRecord], the
production classifier's own per-round output):

  * 73 zero-readback rounds with a byte-identical prefix and gap > 300s were
    bucketed ``body_change`` (n=37) or ``other`` (n=34).
  * ``body_change`` reads as "the CLIENT changed the bytes" — the exact
    mislabel this whole detector family exists to prevent. 95 of those 101
    rounds carried ``body_identical=True`` with ZERO culprits, i.e. the
    classifier had already PROVEN the bytes identical and then filed the round
    under a client body change anyway.
  * Cost consequence: 1,516 CNY of ordinary, UNAVOIDABLE cache rebuilds (the
    entry legitimately expired after 5+ minutes of idle) were being counted as
    recoverable waste. Removing them raises the upstream-gateway share of the
    genuinely avoidable spend from 86.1% to 91.2% — i.e. this mislabel was
    actively distorting where the money was thought to be going.

A TTL rebuild is NOT waste: the cache entry expired on its own schedule and
had to be re-created. Bucketing it honestly is what lets a cost dashboard
subtract it instead of chasing it.

GUARDS
  * The cause string the backend actually emits at >300s buckets as
    ``ttl_expiry`` under BOTH return keys it can arrive with
    (``no_cache_reuse`` → was body_change, ``server_side`` → was other).
  * COMPLEMENT: a short-gap byte-identical miss still buckets
    ``upstream_identical`` — the fix must not swallow the gateway class, which
    is where 91% of the recoverable money is.
  * COMPLEMENT: a real client body change still buckets ``body_change``.
  * The TTL branch is reached by CALLING the real backend fn, never by
    hardcoding its wording (the drift trap this file's sibling already fights).
"""

from __future__ import annotations

import pytest

from lib.tasks_pkg.cache_tracking._detect import (
    BUCKET_BODY_CHANGE,
    BUCKET_TTL_EXPIRY,
    BUCKET_UPSTREAM,
    _resolve_break_cause,
    classify_verdict,
)

pytestmark = pytest.mark.unit

_TTL_GAP_S = 330.0      # past the 300s TTL line — the observed p50 of the bucket
_FAST_GAP_S = 30.0      # inside a live conversation, far short of TTL


def _cause(**kw) -> str:
    """Derive a real cause string by CALLING the backend fn (never hardcoded)."""
    base = dict(client_changes={}, prefix_mutation_break=False,
                elapsed=_FAST_GAP_S, cache_read=0, prefix_mutated=False,
                prefix_culprits=[], wire_proven_identical=True,
                namespace_switch=None,
                namespace_verified_same=True)
    base.update(kw)
    return _resolve_break_cause(**base)


class TestTtlExpiryBuckets:
    def test_backend_still_emits_a_ttl_expiry_cause_past_the_line(self):
        """Anchor the premise: the >300s branch is live and says 'TTL expiry'.

        If this ever stops being true the rest of the file is testing nothing,
        so assert the premise explicitly rather than assuming it."""
        cause = _cause(elapsed=_TTL_GAP_S)
        assert 'ttl expiry' in cause.lower(), cause

    @pytest.mark.parametrize('return_key', ['no_cache_reuse', 'server_side'])
    def test_ttl_expiry_buckets_as_itself_not_a_client_change(self, return_key):
        """The whole point: a >5min idle rebuild is not a client body change.

        Parametrised over BOTH keys the detector can return this cause under —
        the pre-fix behaviour differed per key (body_change vs other), so a
        single-key test would have left half the mislabel alive."""
        cause = _cause(elapsed=_TTL_GAP_S)
        assert classify_verdict({return_key: cause}) == BUCKET_TTL_EXPIRY

    def test_ttl_expiry_is_never_bucketed_as_body_change(self):
        """Explicit negative on the specific mislabel that cost 1,516 CNY of
        misattribution — 'body_change' accuses our own client of mutating the
        prefix, which the wire fingerprint had already disproven."""
        cause = _cause(elapsed=_TTL_GAP_S)
        for key in ('no_cache_reuse', 'server_side'):
            assert classify_verdict({key: cause}) != BUCKET_BODY_CHANGE


class TestTheFixDoesNotSwallowNeighbours:
    """COMPLEMENT set. Without these, 'return ttl_expiry for everything' passes
    the tests above while destroying the gateway signal that carries 91% of the
    recoverable spend."""

    @pytest.mark.parametrize('return_key', ['no_cache_reuse', 'server_side'])
    def test_short_gap_byte_identical_still_buckets_upstream(self, return_key):
        cause = _cause(elapsed=_FAST_GAP_S)
        assert classify_verdict({return_key: cause}) == BUCKET_UPSTREAM

    def test_a_real_client_body_change_still_buckets_body_change(self):
        verdict = {'system_prompt': 'system prompt changed between turns'}
        assert classify_verdict(verdict) == BUCKET_BODY_CHANGE

    def test_prefix_mutation_past_the_ttl_line_is_still_the_client(self):
        """A genuine client byte mutation that happens to arrive after a long
        gap must stay attributed to the client — TTL must not become a laundry
        chute for real client faults. The backend names the mutation, so the
        cause never reaches the TTL branch."""
        cause = _cause(elapsed=_TTL_GAP_S, prefix_mutation_break=True,
                       prefix_culprits=['user:ab.content'])
        assert classify_verdict({'prefix_mutation': cause}) != BUCKET_TTL_EXPIRY


class TestTheUnprovenHedgeIsNotAConfirmedExpiry:
    """The no-fingerprint fallback MENTIONS TTL without concluding it.

    Regression guard for a real defect: the first version of this fix matched
    the bare substring 'ttl expiry', which also appears inside
    ``prefix not reused — likely server-side miss or TTL expiry (UNPROVEN —
    no wire fingerprint)``. That sentence is a HEDGE listing TTL as one
    possibility, not a finding. Claiming it as a confirmed expiry moved real,
    unexplained waste OUT of the recoverable total — the dangerous direction,
    because it makes the upstream problem look smaller than it is.
    """

    def test_unproven_zero_read_fallback_is_not_ttl(self):
        cause = _cause(cache_read=0, prefix_mutated=False,
                       wire_proven_identical=False, elapsed=_FAST_GAP_S)
        assert 'ttl expiry' in cause.lower(), (
            'premise: the fallback really does mention TTL — that is why the '
            'bare-substring match was wrong')
        assert classify_verdict({'no_cache_reuse': cause}) != BUCKET_TTL_EXPIRY

    def test_the_genuine_expiry_cause_still_buckets_as_ttl(self):
        """COMPLEMENT — tightening the match must not break the real case."""
        cause = _cause(elapsed=_TTL_GAP_S)
        assert classify_verdict({'no_cache_reuse': cause}) == BUCKET_TTL_EXPIRY
