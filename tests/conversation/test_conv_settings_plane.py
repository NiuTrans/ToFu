"""Conversation settings plane — turn submit must merge metadata safely.

Incident (2026-08-21, owner follow-up to the empty-conv purge fix: "old
dialogues are ALSO losing the project bar"). Root cause had THREE
independent breaks along the per-conversation settings plane:

  1. ``_SIDEBAR_SETTINGS_KEYS`` excluded ``projectPath``/``projectPaths``/
     ``readOnlyPaths`` — freshly-hydrated shells never knew the mount.
  2. The authoritative Turn snapshot originally omitted settings, so a
     conversation's local catalog shell kept incomplete project metadata.
     Conversation Sync v3 now returns Turns, attempts, revision, and settings
     in one snapshot.
  3. ``create_turn_pair`` applied ``conversation.settings`` as a WHOLESALE
     COLUMN REPLACE on every turn submit. Any incomplete local picture
     (breaks 1+2) was then laundered into the stored settings on EVERY
     send — '' over projectPath, and silent deletion of every field the
     client payload doesn't even carry (autopilotObjective /
     autopilotSummaries / projectSummary / fold marker / lastMsg*).

Break 3 is the permanent, server-side half of the incident and is pinned
here: the write must MERGE (explicit client values still overwrite,
unknown keys survive).
"""

from __future__ import annotations

import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


@pytest.fixture()
def seeded_conversation():
    from tests._seed import seed_conversation

    conversation_id = f'conv-settings-merge-{uuid.uuid4().hex}'
    seed_conversation(
        conversation_id,
        title='Settings merge',
        settings={
            'projectPath': '/repo/chatui',
            'projectPaths': ['/repo/chatui', '/repo/extra'],
            'readOnlyPaths': ['/repo/extra'],
            'autopilotObjective': 'server-owned objective',
            'autopilotSummaries': {'run-1': {'content': 'server-owned'}},
            'projectSummary': {'text': 'server-owned summary'},
        },
    )
    return conversation_id


def _stored_settings(conversation_id):
    from tests._seed import conv_settings
    return conv_settings(conversation_id, user_id=1)


def test_turn_submit_merges_settings_and_preserves_server_only_fields(
        seeded_conversation):
    """A turn submit whose client payload lacks the server-only
    fields (and carries a stale '' for projectPath) must NOT destroy them.

    Under the pre-fix REPLACE semantics this exact call wiped
    autopilotObjective/autopilotSummaries/projectSummary from the column —
    asserting their survival is what makes this test load-bearing."""
    from lib.turn_lifecycle import create_turn_pair

    create_turn_pair(
        seeded_conversation, command_id='send-1',
        input_projection={'content': 'hello'}, config={'model': 'gpt-4o'},
        conversation_defaults={'settings': {
            # The incomplete shell picture: an explicit '' overwrite.
            'projectPath': '', 'projectPaths': [], 'readOnlyPaths': [],
            'chatMode': 'chat', 'model': 'gpt-4o',
        }}, user_id=1)
    stored = _stored_settings(seeded_conversation)
    # Explicit client values DO overwrite (intentional clears keep working):
    assert stored['projectPath'] == ''
    assert stored['projectPaths'] == []
    assert stored['chatMode'] == 'chat'
    # Server-owned fields the payload never carried must SURVIVE (the merge):
    assert stored['autopilotObjective'] == 'server-owned objective'
    assert stored['autopilotSummaries'] == {'run-1': {'content': 'server-owned'}}
    assert stored['projectSummary'] == {'text': 'server-owned summary'}


def test_turn_submit_without_path_keys_keeps_stored_mount(seeded_conversation):
    """A payload that simply doesn't mention the mount (e.g. a headless or
    older client) must leave the stored paths fully intact."""
    from lib.turn_lifecycle import create_turn_pair

    create_turn_pair(
        seeded_conversation, command_id='send-2',
        input_projection={'content': 'hello'}, config={'model': 'gpt-4o'},
        conversation_defaults={'settings': {'chatMode': 'studio'}}, user_id=1)
    stored = _stored_settings(seeded_conversation)
    assert stored['projectPath'] == '/repo/chatui'
    assert stored['projectPaths'] == ['/repo/chatui', '/repo/extra']
    assert stored['readOnlyPaths'] == ['/repo/extra']
    assert stored['chatMode'] == 'studio'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
