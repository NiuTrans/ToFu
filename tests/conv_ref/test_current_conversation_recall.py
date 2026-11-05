"""get_conversation recalls the CURRENT conversation's durable transcript.

A long-lived conversation (a router that runs for months) loses Turns from its
working view to compaction. The durable transcript keeps them, and the only
model-facing reader of a transcript is ``get_conversation``, which used to
refuse the caller's own conversation outright. These specs pin the recall
contract: the current conversation is readable through the ordinary paging
(``before``/``limit``, raw and readable), the read says it is the current
conversation's durable transcript (not the working view), and owner scoping is
unchanged — the read still goes through the owner-scoped repository seam.
"""

import json

import pytest

from lib.conv_ref import execute_conv_ref_tool
from lib.conv_ref._detail import _CURRENT_CONVERSATION_NOTE

pytestmark = pytest.mark.unit

OWNER = 7


def _messages(total):
    return [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'TURN{index:03d} body', '_turnId': f't{index}'}
        for index in range(total)
    ]


@pytest.fixture
def transcript(monkeypatch):
    """One owner's conversation 'self' behind the repository read seam."""
    from lib.conv_ref import _detail

    messages = _messages(90)
    reads = []

    def read(conversation_id, *, user_id, **projection):
        reads.append((conversation_id, user_id, dict(projection)))
        if conversation_id != 'self' or user_id != OWNER:
            return None  # another owner's view of this ID: not found
        row = {
            'id': 'self', 'user_id': OWNER, 'title': 'Router',
            'messages': messages, 'created_at': 1, 'updated_at': 2,
            'settings': {}, 'msg_count': len(messages), 'rev': 5,
        }
        window = projection.get('message_window')
        if window is None:
            return row
        end = min(projection.get('before_sequence', len(messages)), len(messages))
        return {**row, 'messages': messages[max(0, end - window):end]}

    monkeypatch.setattr(_detail, '_read_conversation_snapshot', read)
    return reads


def _get(args, *, user_id=OWNER, current='self'):
    return execute_conv_ref_tool(
        'get_conversation', {'conversation_id': 'self', **args},
        current_conv_id=current, user_id=user_id)


def test_current_conversation_is_readable_and_labelled(transcript):
    out = _get({'raw': False})
    assert not out.startswith('Error')
    assert 'Referenced Conversation: "Router"' in out
    assert _CURRENT_CONVERSATION_NOTE in out
    # The default window ends at the newest Turn, like any other read.
    assert 'TURN089' in out
    assert all(user_id == OWNER for _cid, user_id, _p in transcript)


def test_current_conversation_pages_backwards_to_out_of_view_turns(transcript):
    out = _get({'raw': False, 'before': 21, 'limit': 5})
    assert _CURRENT_CONVERSATION_NOTE in out
    # before=21 ends just before message #21: messages #16..#20 (0-based 15..19).
    for index in range(15, 20):
        assert f'TURN{index:03d}' in out
    assert 'TURN020' not in out and 'TURN089' not in out


def test_current_conversation_raw_record_parses_and_is_labelled(transcript):
    out = _get({'before': 11, 'limit': 4})
    assert 'Raw Conversation Record' in out
    assert _CURRENT_CONVERSATION_NOTE in out
    record = json.loads(out.split('```json', 1)[1].rsplit('```', 1)[0])
    assert record['id'] == 'self'
    assert record['messageIndices'][-4:] == [7, 8, 9, 10]


def test_other_conversation_reads_carry_no_current_label(transcript):
    out = _get({'raw': False}, current='another-conversation')
    assert 'TURN089' in out
    assert _CURRENT_CONVERSATION_NOTE not in out


def test_owner_scoping_is_unchanged_for_the_current_conversation(transcript):
    out = _get({'raw': False}, user_id=OWNER + 1)
    assert out.startswith("Error: Conversation 'self' not found")
    assert transcript and all(user_id == OWNER + 1 for _c, user_id, _p in transcript)


def test_tool_contract_names_the_current_conversation_recall_path():
    from lib.tools.conversation import CONV_REF_GET_TOOL, CONV_REF_LIST_TOOL
    get_text = CONV_REF_GET_TOOL['function']['description']
    assert "current conversation's own ID" in get_text
    assert 'outside your current working view' in get_text
    assert 'compaction' in get_text and 'before=N' in get_text
    conversation_id = CONV_REF_GET_TOOL['function']['parameters']['properties']['conversation_id']
    assert 'durable transcript' in conversation_id['description']
    assert 'never listed' in CONV_REF_LIST_TOOL['function']['description']
