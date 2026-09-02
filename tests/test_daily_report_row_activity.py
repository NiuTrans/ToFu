"""Daily-report scans consume owner-scoped conversation snapshots only."""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from lib.conversations.repository import ConversationSnapshot
from lib.daily_report import conversations, cost


pytestmark = pytest.mark.unit
OWNER_USER_ID = 41


def _ms(day: int) -> int:
    return int(dt.datetime(2026, 8, day, 12).timestamp() * 1000)


def _range() -> tuple[int, int]:
    return (
        int(dt.datetime(2026, 8, 1).timestamp() * 1000),
        int(dt.datetime(2026, 9, 1).timestamp() * 1000),
    )


def _snapshot(
    conversation_id: str,
    messages: list[dict],
    *,
    created_at: int | None = None,
    updated_at: int | None = None,
    title: str | None = None,
    settings: dict | None = None,
) -> ConversationSnapshot:
    return ConversationSnapshot(
        metadata={
            'id': conversation_id,
            'user_id': OWNER_USER_ID,
            'title': title or f'Title {conversation_id}',
            'created_at': created_at if created_at is not None else _ms(1),
            'updated_at': updated_at if updated_at is not None else _ms(3),
            'settings': settings or {},
            'msg_count': len(messages),
            'rev': 1,
        },
        messages=messages,
    )


def test_activity_counts_each_conversation_once_per_active_day(monkeypatch):
    snapshots = [
        _snapshot('multi', [
            {'role': 'user', 'timestamp': _ms(1), 'content': 'a'},
            {'role': 'assistant', 'timestamp': _ms(2), 'content': 'b'},
            {'role': 'user', 'timestamp': _ms(2), 'content': 'c'},
        ]),
        _snapshot('same-day', [
            {'role': 'user', 'timestamp': _ms(2), 'content': 'd'},
        ]),
    ]
    calls = []

    def list_snapshots(**kwargs):
        calls.append(kwargs)
        return len(snapshots), iter(snapshots)

    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded',
        list_snapshots)
    start, end = _range()

    assert conversations._activity_counts_for_range(
        start, end, owner_user_id=OWNER_USER_ID) == {1: 1, 2: 2}
    assert calls == [{
        'user_id': OWNER_USER_ID,
        'updated_at_gte': start,
        'created_at_lt': end,
        'limit': 10_000,
        'settings_keys': [],
    }]


def test_missing_message_timestamp_uses_snapshot_update_time(monkeypatch):
    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded',
        lambda **_kwargs: (1, iter([_snapshot(
            'legacy', [{'role': 'user', 'content': 'legacy'}],
            created_at=_ms(1), updated_at=_ms(4))])),
    )
    start, end = _range()

    assert conversations._activity_counts_for_range(
        start, end, owner_user_id=OWNER_USER_ID) == {4: 1}


def test_activity_scan_fails_closed_when_authority_is_unavailable(monkeypatch):
    def unavailable(**_kwargs):
        raise RuntimeError('authority unavailable')

    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded', unavailable)
    start, end = _range()

    assert conversations._activity_counts_for_range(
        start, end, owner_user_id=OWNER_USER_ID) == {}


def test_report_extraction_uses_exact_snapshot_transcript(monkeypatch):
    rows = [
        {'role': 'user', 'timestamp': _ms(1), 'content': 'old'},
        {'role': 'user', 'timestamp': _ms(2), 'content': 'today question'},
        {
            'role': 'assistant', 'timestamp': _ms(2), 'content': 'today answer',
            'toolRounds': [{'calls': [{'name': 'read_file'}]}],
        },
        {'role': 'assistant', 'timestamp': _ms(3), 'content': 'future'},
    ]
    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded',
        lambda **_kwargs: (1, iter([_snapshot('report', rows)])),
    )

    got = conversations._extract_convs_for_date(
        '2026-08-02', owner_user_id=OWNER_USER_ID)

    assert len(got) == 1
    assert got[0]['id'] == 'report'
    assert got[0]['rounds'] == 1
    assert got[0]['toolsUsed'] == ['read_file']
    assert 'today question' in got[0]['transcript']
    assert 'today answer' in got[0]['transcript']
    assert 'old' not in got[0]['transcript']
    assert 'future' not in got[0]['transcript']


def test_cost_scan_uses_usage_from_authority_snapshot(monkeypatch):
    snapshot = _snapshot(
        'cost-current',
        [
            {'role': 'user', 'timestamp': _ms(1), 'content': 'question'},
            {
                'role': 'assistant', 'timestamp': _ms(2), 'content': 'answer',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 25},
                'model': 'model-x', 'providerId': 'provider-y',
            },
        ],
        settings={'model': 'fallback-model'},
    )
    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded',
        lambda **_kwargs: (1, iter([snapshot])),
    )
    pricing_calls = []

    def price(usage, model, provider, **kwargs):
        pricing_calls.append((usage, model, provider, kwargs))
        return 1.25

    monkeypatch.setattr(cost, '_calc_msg_cost_cny', price)
    start, end = _range()

    got = cost._scan_costs_in_range(
        start, end, 2026, 8, owner_user_id=OWNER_USER_ID)

    assert got[2]['cost'] == 1.25
    assert got[2]['conversations']['cost-current'] == {
        'name': 'Title cost-current', 'cost': 1.25, 'tokens': 125,
    }
    assert pricing_calls[0][1:3] == ('model-x', 'provider-y')
