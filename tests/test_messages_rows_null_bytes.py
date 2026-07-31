"""Guard: the row mirror must serialize JSONB columns with ``json_dumps_pg``.

**The incident (2026-07-27).** ``message_to_row`` built its two JSONB-bound
columns — ``meta`` and ``content_json`` — with a bare
``json.dumps(..., ensure_ascii=False)``. ``json.dumps`` encodes a Python NUL
(``U+0000``) as the six-character escape ``\\u0000``, which PostgreSQL's JSONB
parser rejects outright::

    [DB] SQL execution failed (UntranslatableCharacter):
        unsupported Unicode escape sequence
    [messages_rows] dual-write mirror failed conv=ms2vpi7jned9 (non-fatal)

The authoritative blob writer never had this problem because it goes through
:func:`lib.database._wrappers.json_dumps_pg`, which strips NULs first. So the
two writers of the SAME data disagreed on serialization, and every message
carrying a NUL byte silently failed to mirror.

**Why that is worse than a noisy error.** ``dual_write_conv`` swallows
exceptions by design (the JSONB array stays authoritative and must never be
broken by a mirroring failure). A rejected row therefore leaves the
conversation with FEWER rows than blob messages — exactly the "partial
backfill" shape the project charter calls *the real killer*, because a
windowed read over it renders a prefix and silently drops the tail. The
``row_window_usable`` guard fails closed to the blob, so nothing is
user-visible today, but migration progress silently rots.

**The invariant this file pins:** every JSONB-bound value produced by
``message_to_row`` MUST be NUL-free, i.e. produced by ``json_dumps_pg`` (or
equivalent), so the row mirror and the blob writer agree byte-for-byte on how
a NUL is handled.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database.messages_rows import message_to_row  # noqa: E402

pytestmark = pytest.mark.unit


# A NUL in a plain string field, a NUL inside multipart content blocks, and a
# NUL in a nested metadata value — the three shapes real messages produce.
_NUL_MSG_PLAIN = {
    'role': 'user',
    'content': 'hello\x00world',
    '_msgId': 'm-plain',
}

_NUL_MSG_MULTIPART = {
    'role': 'user',
    'content': [
        {'type': 'text', 'text': 'before\x00after'},
        {'type': 'text', 'text': 'clean'},
    ],
    '_msgId': 'm-multipart',
}

_NUL_MSG_NESTED = {
    'role': 'assistant',
    'content': 'ok',
    '_msgId': 'm-nested',
    'toolRounds': [{'output': 'stdout\x00truncated'}],
}


def _jsonb_bound_values(row):
    """The row values that land in a JSONB column (see _core_schema)."""
    return {'meta': row['meta'], 'content_json': row['content_json']}


@pytest.mark.parametrize('msg', [
    _NUL_MSG_PLAIN,
    _NUL_MSG_MULTIPART,
    _NUL_MSG_NESTED,
], ids=['plain', 'multipart', 'nested'])
def test_jsonb_columns_carry_no_null_escape(msg):
    """No JSONB-bound column may contain a ``\\u0000`` escape.

    This is the assertion that fails on the pre-fix code: bare ``json.dumps``
    emits ``\\u0000`` and PostgreSQL then rejects the whole INSERT.
    """
    row = message_to_row('c-nul', 0, msg, now_ms=1)
    for col, value in _jsonb_bound_values(row).items():
        assert isinstance(value, str), f'{col} should be serialized JSON text'
        assert '\\u0000' not in value, (
            f'{col} contains a \\u0000 escape — PostgreSQL JSONB rejects it '
            f'(UntranslatableCharacter). Serialize with json_dumps_pg.'
        )
        # The literal NUL must not survive either — _sanitize_pg_param would
        # strip it at bind time, but then the row text and the blob text would
        # differ, breaking parity.
        assert '\x00' not in value, f'{col} contains a raw NUL byte'


@pytest.mark.parametrize('msg', [
    _NUL_MSG_PLAIN,
    _NUL_MSG_MULTIPART,
    _NUL_MSG_NESTED,
], ids=['plain', 'multipart', 'nested'])
def test_jsonb_columns_still_parse_as_json(msg):
    """Stripping NULs must leave syntactically valid JSON."""
    row = message_to_row('c-nul', 0, msg, now_ms=1)
    for col, value in _jsonb_bound_values(row).items():
        try:
            json.loads(value)
        except (ValueError, TypeError) as e:
            pytest.fail(f'{col} is not valid JSON after NUL handling: {e}')


def test_row_mirror_matches_blob_writer_on_nulls():
    """The row mirror and the authoritative blob writer must agree.

    Two writers persisting the same message must handle a NUL identically —
    otherwise ``verify_conv_parity`` can never converge.
    """
    from lib.database._wrappers import json_dumps_pg

    row = message_to_row('c-nul', 0, _NUL_MSG_PLAIN, now_ms=1)
    blob_text = json_dumps_pg(_NUL_MSG_PLAIN)

    assert json.loads(row['meta']) == json.loads(blob_text), (
        'row meta and blob serialization disagree on NUL handling'
    )


def test_null_free_messages_are_untouched():
    """The fix must be a no-op for the overwhelmingly common NUL-free case."""
    clean = {'role': 'user', 'content': 'plain text', '_msgId': 'm-clean'}
    row = message_to_row('c-clean', 0, clean, now_ms=1)
    assert json.loads(row['meta']) == clean

    clean_multipart = {
        'role': 'user',
        'content': [{'type': 'text', 'text': 'hi'}],
        '_msgId': 'm-clean-mp',
    }
    row_mp = message_to_row('c-clean', 1, clean_multipart, now_ms=1)
    assert json.loads(row_mp['content_json']) == clean_multipart['content']


def test_unicode_is_preserved_not_escaped():
    """``ensure_ascii=False`` semantics must survive the switch."""
    msg = {'role': 'user', 'content': '中文内容 🫧', '_msgId': 'm-cjk'}
    row = message_to_row('c-cjk', 0, msg, now_ms=1)
    assert '中文内容' in row['meta'], 'non-ASCII must not be \\u-escaped'
    assert json.loads(row['meta'])['content'] == '中文内容 🫧'
