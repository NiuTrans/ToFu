"""Resource and lifecycle contracts for reconstructible Feishu sessions."""

from __future__ import annotations

import pytest

from lib.feishu.user_state import (
    FeishuSessionCapacityError,
    FeishuUserSessionStore,
)


pytestmark = pytest.mark.unit


def test_idle_sessions_are_lru_evicted_but_pinned_session_survives():
    store = FeishuUserSessionStore(capacity=2)
    store.set_model('user-a', 'model-a')
    store.set_model('user-b', 'model-b')

    with store.pin('user-a'):
        store.set_model('user-c', 'model-c')
        assert store.model('user-a', 'default') == 'model-a'
        assert store.model('user-b', 'default') == 'default'
        assert len(store) == 2


def test_capacity_fails_closed_when_every_session_is_active():
    store = FeishuUserSessionStore(capacity=2)

    with store.pin('user-a'), store.pin('user-b'):
        with pytest.raises(
            FeishuSessionCapacityError,
            match='all Feishu session slots are active',
        ):
            store.set_mode('user-c', 'tool')


def test_history_is_bounded_by_count_per_message_and_total_chars():
    store = FeishuUserSessionStore(
        capacity=2,
        history_messages=3,
        history_message_chars=5,
        history_total_chars=9,
    )

    store.append_message('user-a', 'user', '123456789')
    store.append_message('user-a', 'assistant', 'abcde')
    store.append_message('user-a', 'user', 'xyz')

    assert store.history('user-a') == [
        {'role': 'assistant', 'content': 'abcde'},
        {'role': 'user', 'content': 'xyz'},
    ]


def test_state_inputs_have_explicit_type_and_size_bounds():
    store = FeishuUserSessionStore(capacity=1)

    with pytest.raises(ValueError, match='user id'):
        store.mode('')
    with pytest.raises(ValueError, match='model'):
        store.set_model('user-a', 'x' * 257)
    with pytest.raises(ValueError, match='pending state'):
        store.set_pending('user-a', {'value': object()})
