"""Channel initiator vocabulary and its persistence/authority boundary (CHANNELS.md C4, C7)."""
from pathlib import Path

import pytest
import yaml

from lib.turn_initiation import (
    INITIATOR_CHANNEL, NON_HUMAN_INITIATORS, VALID_INITIATORS, require_human_turn, stamp_initiator,
)
from lib.turn_projection_patch import normalize_projection_document

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _channel_origin(**extra):
    return {'channel': 'dx', 'thread': 'dx:gid:11', 'eventIds': ['dx:gid:11/22'], 'title': 'G', **extra}


def test_channel_is_a_nonhuman_initiator():
    assert INITIATOR_CHANNEL == 'channel'
    assert INITIATOR_CHANNEL in NON_HUMAN_INITIATORS


def test_stored_channel_turn_keeps_its_initiator_and_is_never_human_authority():
    projection = {'content': 'evidence', 'origin': {'initiator': 'channel', 'channel': _channel_origin()}}
    stamp_initiator(projection, INITIATOR_CHANNEL)
    stored = normalize_projection_document(projection)
    assert stored['origin']['initiator'] == 'channel'
    assert stored['origin']['channel'] == _channel_origin()
    with pytest.raises(ValueError):
        require_human_turn({'actor': 'human', 'projection': stored})


def test_stamp_without_origin_still_persists_as_channel():
    stored = normalize_projection_document(stamp_initiator({'content': 'x'}, INITIATOR_CHANNEL))
    assert stored['origin']['initiator'] == 'channel'
    with pytest.raises(ValueError):
        require_human_turn({'actor': 'human', 'projection': stored})


def test_command_chat_owner_message_is_human_with_channel_origin():
    stored = normalize_projection_document(
        {'content': 'do it', 'origin': {'initiator': 'human', 'channel': _channel_origin(junk=1)}})
    assert stored['origin']['channel'] == _channel_origin()
    require_human_turn({'actor': 'human', 'projection': stored})


def test_wire_contract_enumerates_exactly_the_python_vocabulary():
    document = yaml.safe_load((ROOT / 'contracts/conversation_sync_v3.yaml').read_text())
    schemas = document['components']['schemas']
    assert set(schemas['TurnOrigin']['properties']['initiator']['enum']) == set(VALID_INITIATORS)
    assert set(schemas['TurnChannelOrigin']['properties']) == {'channel', 'thread', 'eventIds', 'title'}
