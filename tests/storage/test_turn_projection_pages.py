"""A Turn projection reads back in frame-bounded pages on every authority.

Stage 1 of paged Turn storage (docs/STORAGE.md, "Paged Turn storage"):
``turn.projection.page`` returns one public Turn in pages whose encoded
responses each fit one storage.v2 frame payload, fenced by the projection
revision. Reassembling every page reproduces ``turn.get`` exactly. This spec
reassembles and measures pages itself; it never trusts the authority's own
page planner.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit]

_CONTRACT = 'tofu.turn-projection-page/v1'
_SEQUENCE_MEMBERS = ('toolRounds', 'segments', 'apiRounds')
_TEXT_MEMBERS = ('content', 'thinking')
_FRAME_PAYLOAD_BYTES = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts/storage_v2.json').read_text()
)['frame']['maximumPayloadBytes']
# Multi-byte characters, quotes, backslashes and control characters cost more
# encoded bytes than code points; text pages must split them exactly.
_TEXT_UNIT = 'Résumé "quoted" \\ path\n\t\x01 🚀 '


def _encoded_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def _large_projection() -> dict:
    """About four frames once segments mirror the tool rounds and the content.

    The derived text segment repeats all of ``content``, so it is one
    indivisible item larger than a frame; ``content`` itself must split.
    """
    result = ('x' * 63 + _TEXT_UNIT) * 1000
    return {
        'content': _TEXT_UNIT * 200_000,
        'thinking': _TEXT_UNIT * 20_000,
        'toolRounds': [{
            'toolCallId': f'call-{index}',
            'toolName': 'read_file',
            'roundNum': index,
            'llmRound': index,
            'toolArgs': {'path': f'src/module_{index}.py'},
            'toolContent': f'{index}:{result}',
            'status': 'done',
        } for index in range(64)],
        'apiRounds': [{'round': index, 'inputTokens': 1000 + index, 'outputTokens': 10}
                      for index in range(64)],
    }


@pytest.fixture
def turns(semantic_authority, monkeypatch):
    from lib.turn_lifecycle import append_settled_turn

    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'paged-turn-projection'
    human = append_settled_turn(
        conversation, command_id='human-request', actor='human', user_id=owner,
        projection={'content': 'Inspect every module'},
        conversation_defaults={'allowCreate': True})['turn']
    large = append_settled_turn(
        conversation, command_id='large-reply', actor='assistant', user_id=owner,
        projection=_large_projection())['turn']
    return client, owner, conversation, human, large


def _query(client, owner, conversation, turn_id, **fields):
    return client().query('turn.projection.page', {
        'conversation_id': conversation, 'user_id': owner, 'turn_id': turn_id, **fields})


def _whole(client, owner, conversation, turn_id):
    return client().query('turn.get', {
        'conversation_id': conversation, 'user_id': owner, 'turn_id': turn_id})


def _read_pages(client, owner, conversation, turn_id):
    first = _query(client, owner, conversation, turn_id, page=0)
    assert first['contract'] == _CONTRACT and first['stale'] is False
    revision = first['projectionRevision']
    pages = [first]
    for index in range(1, first['pageCount']):
        page = _query(client, owner, conversation, turn_id, page=index,
                      projection_revision=revision)
        assert page['stale'] is False and page['projectionRevision'] == revision
        pages.append(page)
    return pages


def _reassemble(pages) -> dict:
    head = pages[0]
    turn = json.loads(json.dumps(head['turn']))
    lengths = head['pagedMembers']
    assert set(lengths) <= set(_SEQUENCE_MEMBERS + _TEXT_MEMBERS)
    members = {name: [] if name in _SEQUENCE_MEMBERS else '' for name in lengths}
    for index, page in enumerate(pages):
        assert page['page'] == index and page['pageCount'] == head['pageCount']
        assert ('turn' in page) == (index == 0) and ('pagedMembers' in page) == (index == 0)
        for name, piece in page['members'].items():
            assert name in lengths and piece['start'] == len(members[name])
            members[name] += piece['items'] if name in _SEQUENCE_MEMBERS else piece['text']
    assert {name: len(value) for name, value in members.items()} == lengths
    assert not set(lengths) & set(turn['projection'])
    turn['projection'].update(members)
    return turn


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_a_turn_larger_than_one_frame_reads_back_in_bounded_pages(turns):
    client, owner, conversation, _human, large = turns
    whole = _whole(client, owner, conversation, large['turnId'])
    assert _encoded_bytes(whole) > 2 * _FRAME_PAYLOAD_BYTES

    pages = _read_pages(client, owner, conversation, large['turnId'])

    assert len(pages) >= 3
    oversized = [page for page in pages if _encoded_bytes(page) > _FRAME_PAYLOAD_BYTES]
    # Only an item that alone exceeds the frame may exceed it, alone on a page.
    assert len(oversized) == 1
    [(name, piece)] = oversized[0]['members'].items()
    assert name == 'segments' and len(piece['items']) == 1 and oversized[0]['page'] > 0
    assert piece['items'][0]['text'] == whole['projection']['content']
    assert pages[0]['projectionRevision'] == whole['projectionRevision']
    assert _reassemble(pages) == whole
    # Text members split inside one string without losing a character.
    assert sum('content' in page['members'] for page in pages) >= 2


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_a_small_turn_is_one_page_and_revisions_fence_every_page(turns):
    from lib.turn_lifecycle import update_turn_projection

    client, owner, conversation, human, large = turns
    [page] = _read_pages(client, owner, conversation, human['turnId'])
    assert page['pageCount'] == 1
    assert _reassemble([page]) == _whole(client, owner, conversation, human['turnId'])

    revision = page['projectionRevision']
    update_turn_projection(conversation, human['turnId'],
                           projection={'content': 'Inspect every module twice'},
                           expected_projection_revision=revision, user_id=owner)
    current = _whole(client, owner, conversation, human['turnId'])['projectionRevision']
    assert current != revision
    assert _query(client, owner, conversation, human['turnId'], page=0,
                  projection_revision=revision) == {
        'contract': _CONTRACT, 'stale': True, 'projectionRevision': current}
    assert _query(client, owner, conversation, 'missing-turn', page=0) is None

    from lib.storage.errors import StorageError
    large_count = _query(client, owner, conversation, large['turnId'], page=0)['pageCount']
    with pytest.raises(StorageError) as beyond:
        _query(client, owner, conversation, large['turnId'], page=large_count,
               projection_revision=large['projectionRevision'])
    assert beyond.value.code == 'database_protocol_error'
