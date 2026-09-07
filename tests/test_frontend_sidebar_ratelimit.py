"""Boundary guards for the TurnState-derived sidebar rate-limit status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_sidebar_reads_typed_phase_selector_without_a_phase_writer():
    source = runtime_section('ui/conversation_list.js')
    start = source.index('function convRateLimitPhase(')
    end = source.index('function _convStatusFlags(', start)
    boundary = source[start:end]
    assert 'ConversationTurnRead' in boundary
    assert 'livePhase' in boundary
    assert 'presentConversationRateLimit(phase)' in boundary
    for retired in ('streamSessions', 'setStreamPhase', 'activeTaskId'):
        assert retired not in source


def test_sidebar_status_and_markup_keep_the_rate_limit_flag():
    source = runtime_section('ui/conversation_list.js')
    assert 'const rateLimited = !!(streaming && convRateLimitPhase(c));' in source
    assert 'unconfirmed, rateLimited };' in source
    assert 'conv-ratelimit-dot' in source
    assert 'conv-status-ratelimit' in source


def test_rate_limit_copy_exists_in_both_locales():
    zh = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    en = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    for key in ('sidebar.rateLimited', 'sidebar.rateLimitedTag'):
        assert zh.get(key)
        assert en.get(key)


def test_rate_limit_status_has_explicit_styles():
    styles = '\n'.join(path.read_text() for path in
        (ROOT / 'frontend/src/styles/application').glob('*.css'))
    assert '.conv-streaming-dot.conv-ratelimit-dot' in styles
    assert '.conv-status-ratelimit' in styles
