"""Frozen conversation archives support allocation-bounded tail reads."""

from __future__ import annotations

import json

import pytest

pytest.importorskip('hypothesis', reason='optional hypothesis dependency not installed')
from hypothesis import given, settings, strategies as st

from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg import _conversations as conversations


pytestmark = pytest.mark.unit


_json_text = st.text(
    alphabet=st.characters(blacklist_categories=('Cs',)),
    max_size=30,
)
_json_value = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63 - 1),
        _json_text,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_json_text, children, max_size=4),
    ),
    max_leaves=12,
)
_archived_sequences = st.lists(
    st.fixed_dictionaries({
        'role': st.sampled_from(('user', 'assistant')),
        'content': _json_value,
    }),
    max_size=20,
)


def _archived_messages() -> list[dict]:
    return [
        {'role': 'user', 'content': 'old prefix'},
        {
            'role': 'assistant',
            'content': 'syntax inside text: ], [{, comma, and \\"quote',
            'nested': {'items': [1, {'close': '} ]'}]},
        },
        {'role': 'user', 'content': 'tail question 🧪'},
        {
            'role': 'assistant',
            'content': 'tail answer',
            'segments': [{'type': 'text', 'text': '[not structure], either'}],
        },
    ]


def test_archived_tail_scanner_handles_nested_json_strings_and_unicode():
    archived = _archived_messages()
    raw = json.dumps(archived, ensure_ascii=False).encode('utf-8')

    messages, total, start, end = (
        conversations._archived_conversation_tail_window(
            raw, window=2, expected_count=len(archived))
    )

    assert messages == archived[-2:]
    assert (total, start, end) == (4, 2, 4)


def test_archived_head_scanner_handles_nested_json_strings_and_unicode():
    archived = _archived_messages()
    raw = json.dumps(archived, ensure_ascii=False).encode('utf-8')

    messages, total, start, end = (
        conversations._archived_conversation_head_window(
            raw,
            window=2,
            before_sequence=2,
            expected_count=len(archived),
        )
    )

    assert messages == archived[:2]
    assert (total, start, end) == (4, 0, 2)


def test_archived_tail_scanner_rejects_a_count_that_claims_no_prefix():
    raw = json.dumps(_archived_messages()).encode()

    result = conversations._archived_conversation_tail_window(
        raw, window=2, expected_count=2)

    assert result is None


def test_archived_tail_scanner_rejects_predictably_large_suffix():
    archived = [
        {'role': 'assistant', 'content': str(index) * 80_000}
        for index in range(4)
    ]
    raw = json.dumps(archived).encode()

    result = conversations._archived_conversation_tail_window(
        raw, window=2, expected_count=len(archived))

    assert result is None


def test_archived_tail_scanner_caps_a_skewed_final_message():
    archived = [
        {'role': 'user', 'content': 'small'} for _ in range(99)
    ] + [{'role': 'assistant', 'content': 'x' * 140_000}]
    raw = json.dumps(archived).encode()

    result = conversations._archived_conversation_tail_window(
        raw, window=1, expected_count=len(archived))

    assert result is None


def test_archived_head_scanner_caps_a_skewed_first_message():
    archived = [
        {'role': 'user', 'content': 'x' * 140_000},
    ] + [{'role': 'assistant', 'content': 'small'} for _ in range(99)]
    raw = json.dumps(archived).encode()

    result = conversations._archived_conversation_head_window(
        raw, window=1, before_sequence=1, expected_count=len(archived))

    assert result is None


@settings(max_examples=120, deadline=None)
@given(
    archived=_archived_sequences,
    window=st.integers(min_value=1, max_value=30),
    text_storage=st.booleans(),
)
def test_archived_tail_scanner_matches_full_json_decode(
    archived,
    window,
    text_storage,
):
    raw_text = json.dumps(archived, ensure_ascii=False)
    raw = raw_text if text_storage else raw_text.encode('utf-8')

    result = conversations._archived_conversation_tail_window(
        raw, window=window, expected_count=len(archived))

    assert result is not None
    messages, total, start, end = result
    assert messages == archived[-window:]
    assert (total, start, end) == (
        len(archived),
        max(0, len(archived) - window),
        len(archived),
    )


@settings(max_examples=120, deadline=None)
@given(
    archived=_archived_sequences,
    window=st.integers(min_value=1, max_value=30),
    text_storage=st.booleans(),
)
def test_archived_head_scanner_matches_full_json_decode(
    archived,
    window,
    text_storage,
):
    raw_text = json.dumps(archived, ensure_ascii=False)
    raw = raw_text if text_storage else raw_text.encode('utf-8')

    result = conversations._archived_conversation_head_window(
        raw,
        window=window,
        before_sequence=window,
        expected_count=len(archived),
    )

    assert result is not None
    messages, total, start, end = result
    assert messages == archived[:window]
    assert (total, start, end) == (
        len(archived),
        0,
        min(window, len(archived)),
    )


def test_conversation_get_uses_tail_decoder_before_full_archive(monkeypatch):
    archived = _archived_messages()
    raw = json.dumps(archived, ensure_ascii=False).encode('utf-8')

    class Session:
        backend = 'sqlite'

        def fetch_one(self, sql, _params=()):
            if 'COUNT(*) AS c' in sql:
                return {'c': 0}
            if 'messages_json' in sql:
                return {'messages_json': raw}
            return {
                'id': 'legacy-tail',
                'user_id': 7,
                'title': 'Legacy tail',
                'created_at_ms': 1,
                'updated_at_ms': 2,
                'settings_json': '{}',
                'msg_count': len(archived),
                'rev': 3,
            }

        def fetch_all(self, _sql, _params=()):
            return []

    monkeypatch.setattr(
        conversations,
        '_archived_conversation_messages',
        lambda _raw: pytest.fail('full archive decoder must not run'),
    )

    document = conversations._conversation_get(Session(), {
        'conv_id': 'legacy-tail',
        'user_id': 7,
        'message_window': 2,
    })

    assert document['messages'] == archived[-2:]
    assert document['metadata']['msg_count'] == len(archived)
    assert document['metadata']['search_text'] == ''

    head = conversations._conversation_get(Session(), {
        'conv_id': 'legacy-tail',
        'user_id': 7,
        'message_window': 2,
        'before_sequence': 2,
    })

    assert head['messages'] == archived[:2]


def test_conversation_get_rejects_cursor_without_window_before_storage_read():
    class Session:
        backend = 'sqlite'

        def fetch_one(self, _sql, _params=()):
            raise AssertionError('invalid projection reached storage')

    with pytest.raises(
        StorageError,
        match='cursor requires a message window',
    ):
        conversations._conversation_get(Session(), {
            'conv_id': 'legacy-tail',
            'user_id': 7,
            'before_sequence': 2,
        })
