"""Bounded live Turn projection cache contracts."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def _key(suffix: str):
    from lib.storage_sidecar.turn_projection_cache import projection_cache_key

    return projection_cache_key("sqlite", 1, "conv", suffix, "attempt")


def _remember(cache, key, revision, charge):
    return cache.remember(
        key,
        revision=revision,
        projection={"content": key[3], "thinking": ""},
        charge_bytes=charge,
        stored_payload_bytes=charge // 2,
        stored_matches_projection=True,
        stable_segments=True,
    )


def test_projection_cache_enforces_lru_byte_entry_and_idle_bounds():
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache

    now = [10.0]
    cache = TurnProjectionCache(
        10,
        max_entries=2,
        max_idle_seconds=5,
        clock=lambda: now[0],
    )
    first, second, third = _key("first"), _key("second"), _key("third")

    assert _remember(cache, first, 1, 4)
    assert _remember(cache, second, 1, 4)
    assert cache.get(first, revision=1) is not None
    assert _remember(cache, third, 1, 4)
    assert cache.get(second, revision=1) is None
    assert cache.get(first, revision=1) is not None
    assert cache.get(third, revision=1) is not None

    assert not _remember(cache, _key("oversize"), 1, 11)
    now[0] = 16.0
    assert cache.get(first, revision=1) is None
    stats = cache.stats()
    assert stats["entries"] == 0
    assert stats["charged_bytes"] == 0
    assert stats["capacity_evictions"] == 1
    assert stats["oversize_rejections"] == 1
    assert stats["expired_evictions"] == 2


def test_projection_cache_revision_mismatch_evicts_instead_of_serving_stale():
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache

    cache = TurnProjectionCache(1024, max_entries=2)
    key = _key("revision")
    assert _remember(cache, key, 7, 100)

    assert cache.get(key, revision=8) is None
    assert cache.get(key, revision=7) is None
    stats = cache.stats()
    assert stats["stale_evictions"] == 1
    assert stats["entries"] == 0


def test_projection_cache_text_charge_adjustment_avoids_full_serialization():
    from lib.storage_sidecar.turn_projection_cache import (
        CachedTurnProjection,
        text_update_charge_bytes,
    )

    entry = CachedTurnProjection(
        revision=1,
        projection={"content": "a", "thinking": ""},
        charge_bytes=100,
        text_bytes=1,
        stored_payload_bytes=50,
        stored_matches_projection=True,
        stable_segments=True,
        last_used_at=0.0,
    )

    assert text_update_charge_bytes(
        entry, {"content": "abcd", "thinking": "思"}) == 118
