"""Ownership and rate-limit contracts for asyncio slow-callback debug mode."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from lib.server_loop_debug import LoopDebugGuard, SlowCallbackRateLimit


pytestmark = pytest.mark.unit


def test_enabled_guard_restores_loop_and_logger_state():
    async def exercise():
        loop = asyncio.get_running_loop()
        asyncio_logger = logging.getLogger('asyncio')
        previous_level = asyncio_logger.level
        previous_debug = loop.get_debug()
        previous_duration = loop.slow_callback_duration
        before = tuple(asyncio_logger.filters)
        guard = LoopDebugGuard(
            loop,
            environ={
                'TOFU_LOOP_DEBUG_GUARD': '1',
                'TOFU_LOOP_SLOW_CALLBACK_SECS': '0.25',
            },
        ).start()
        installed = guard.rate_filter

        assert guard.enabled is True
        assert loop.get_debug() is True
        assert loop.slow_callback_duration == 0.25
        assert installed in asyncio_logger.filters
        guard.stop()
        guard.stop()
        assert guard.enabled is False
        assert loop.get_debug() is previous_debug
        assert loop.slow_callback_duration == previous_duration
        assert asyncio_logger.level == previous_level
        assert tuple(asyncio_logger.filters) == before

    asyncio.run(exercise())


def test_disabled_guard_changes_no_loop_or_logger_state():
    async def exercise():
        loop = asyncio.get_running_loop()
        asyncio_logger = logging.getLogger('asyncio')
        before = (
            loop.get_debug(),
            loop.slow_callback_duration,
            asyncio_logger.level,
            tuple(asyncio_logger.filters),
        )
        guard = LoopDebugGuard(
            loop, environ={'TOFU_LOOP_DEBUG_GUARD': '0'}).start()
        guard.stop()
        after = (
            loop.get_debug(),
            loop.slow_callback_duration,
            asyncio_logger.level,
            tuple(asyncio_logger.filters),
        )
        assert after == before

    asyncio.run(exercise())


def test_rate_limit_reports_suppressed_count_on_next_window():
    ticks = iter((1.0, 2.0, 3.0, 12.0))
    limiter = SlowCallbackRateLimit(
        burst=2, window=10.0, clock=lambda: next(ticks))
    records = [logging.LogRecord(
        'asyncio', logging.WARNING, __file__, 1, 'slow %s', ('step',), None)
        for _ in range(4)]

    assert [limiter.filter(record) for record in records] == [True, True, False, True]
    assert '+1 more slow-callback warnings suppressed' in records[-1].getMessage()


def test_production_entry_registers_reversible_debug_owner():
    source = (Path(__file__).resolve().parents[1]
              / 'lib/serving_loop_lifecycle.py').read_text()
    assert 'LoopDebugGuard(' in source
    assert 'debug_guard.stop()' in source
    assert "name='tofu.serving-loop.shutdown'" in source
    assert 'class _SlowCallbackRateLimit' not in source
