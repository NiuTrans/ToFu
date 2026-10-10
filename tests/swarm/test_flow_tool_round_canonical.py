"""Canonical Flow tool-round persistence/projection parity."""

import pytest

pytestmark = pytest.mark.unit


def _canonical(tool, meta, **extra):
    row = {
        'round': 2,
        'tool': tool,
        'agentId': 'agent-worker-1',
        'tool_call_id': 'flow-tool-occurrence',
        'args_brief': 'brief',
        'status': 'done',
        'preview': 'visible result',
        'canonical_round': {
            'taskId': 'task-parent',
            'roundNum': 2,
            'llmRound': 2,
            'agentId': 'agent-worker-1',
            'toolCallId': 'provider-id',
            'toolName': tool,
            'toolArgs': {'path': 'src/app.py'},
            'query': 'canonical query',
            'results': [meta],
            'status': 'done',
            'toolContent': 'canonical result',
            'tStart': 100,
            'tEnd': 200,
        },
    }
    row.update(extra)
    return row


@pytest.mark.parametrize(('tool', 'meta'), [
    ('read_files', {
        'toolName': 'read_files', 'title': 'src/app.py', 'source': 'Project',
        'snippet': '1: print()', 'fetched': True, 'fetchedChars': 10,
    }),
    ('write_file', {
        'toolName': 'write_file', 'title': 'src/app.py', 'source': 'Project',
        'snippet': 'Wrote src/app.py', 'badge': 'written', 'writeOk': True,
    }),
    ('todo_write', {
        'toolName': 'todo_write', 'source': 'Checklist',
        'todos': [{'id': 'a', 'content': 'Ship', 'status': 'completed'}],
        'checklistId': 'c1', 'todoRevision': 3, 'badge': '1/1',
    }),
    ('run_command', {
        'toolName': 'run_command', 'source': 'Project', 'command': 'pytest',
        'output': 'must not persist', 'exitCode': 0, 'timedOut': False,
    }),
])
def test_projects_canonical_round_without_reconstructing_metadata(tool, meta):
    from lib.orchestration.chat_flow_projection import project_flow_tool_rounds

    projected = project_flow_tool_rounds([_canonical(tool, meta)])[0]
    assert projected['toolCallId'] == 'flow-tool-occurrence'
    assert projected['taskId'] == 'task-parent'
    assert projected['agentId'] == 'agent-worker-1'
    assert projected['toolArgs'] == {'path': 'src/app.py'}
    assert projected['status'] == 'done'
    assert projected['toolContent'] == 'canonical result'
    assert projected['results'][0] == meta
    assert projected['results'][0]['source'] != 'Flow'


def test_canonical_snapshot_sanitizes_private_and_heavy_fields():
    from lib.swarm.agent import flow_canonical_public_round

    round_entry = {
        'roundNum': 1, 'toolName': 'run_command', 'status': 'done',
        'results': [{
            'toolName': 'run_command', 'source': 'Project', 'command': 'pytest',
            'output': 'x' * 20_000, 'todoState': {'history': ['private']},
            'badge': 'ok',
        }],
    }
    snapshot = flow_canonical_public_round(
        round_entry, agent_id='agent-1', tool_call_id='flow-tool-1',
        tool_args={'command': 'pytest', 'credentials': ['vault-name']},
        tool_content='y' * 20_000)
    assert snapshot['agentId'] == 'agent-1'
    assert snapshot['toolCallId'] == 'flow-tool-1'
    assert len(snapshot['toolContent']) == 2_000
    assert snapshot['toolArgs']['credentials'] == ['vault-name']
    assert len(snapshot['results'][0]['output']) == 2_000
    assert 'todoState' not in snapshot['results'][0]
    assert snapshot['results'][0]['badge'] == 'ok'


def test_canonical_snapshot_preserves_valid_rich_renderer_metadata():
    import base64

    from lib.swarm.agent import flow_canonical_public_round

    png = b'\x89PNG\r\n\x1a\n' + b'public-token=not-redacted'
    uri = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
    result = {
        'output': 'command output',
        'content': 'renderer content',
        'imageDataUris': [
            {'uri': uri, 'filename': 'card.png', 'authToken': 'secret'},
            {'uri': 'https://example.test/card.png', 'filename': 'remote.png'},
            {'uri': '/api/images/card.png'},
            {'uri': 'images/card.png'},
            {'uri': 'blob:renderer-id'},
        ],
        'qrImages': [{'uri': uri, 'source': 'terminal'}],
    }
    snapshot = flow_canonical_public_round(
        {'roundNum': 1, 'results': [result]}, agent_id='agent-1',
        tool_call_id='call-1', tool_args={}, tool_content='')
    copied = snapshot['results'][0]
    assert copied['output'] == 'command output'
    assert copied['content'] == 'renderer content'
    assert [item['uri'] for item in copied['imageDataUris']] == [
        uri, 'https://example.test/card.png', '/api/images/card.png',
        'images/card.png', 'blob:renderer-id',
    ]
    assert copied['imageDataUris'][0]['authToken'] == 'secret'
    assert copied['qrImages'][0]['uri'] == uri


def test_canonical_snapshot_rejects_bad_or_oversized_image_data_uris():
    import base64

    from lib.swarm import agent as agent_module

    fake_png = 'data:image/png;base64,' + base64.b64encode(
        b'<svg>not png</svg>').decode('ascii')
    malformed = 'data:image/png;base64,%%%'
    oversized = ('data:image/png;base64,' +
                 'A' * agent_module._FLOW_ROUND_MAX_IMAGE_URI_CHARS)
    snapshot = agent_module.flow_canonical_public_round(
        {'roundNum': 1, 'results': [{'imageDataUris': [
            {'uri': fake_png}, {'uri': malformed}, {'uri': oversized},
            {'uri': 'javascript:alert(1)'},
        ]}]}, agent_id='agent-1', tool_call_id='call-1', tool_args={},
        tool_content='')
    assert snapshot['results'][0]['imageDataUris'] == []


def test_canonical_snapshot_preserves_secret_named_fields():
    from lib.swarm.agent import flow_canonical_public_round

    snapshot = flow_canonical_public_round(
        {'roundNum': 1, 'results': [{
            'nested': {'clientSecret': 'one', 'access_token': 'two'},
            'token_count': 7,
        }]}, agent_id='agent-1', tool_call_id='call-1',
        tool_args={'password': 'three'}, tool_content='')
    assert snapshot['toolArgs']['password'] == 'three'
    assert snapshot['results'][0]['nested'] == {
        'clientSecret': 'one', 'access_token': 'two',
    }
    assert snapshot['results'][0]['token_count'] == 7


def test_legacy_row_still_uses_flow_fallback_projection():
    from lib.orchestration.chat_flow_projection import project_flow_tool_rounds

    projected = project_flow_tool_rounds([{
        'round': 1, 'tool': 'read_files', 'args_brief': 'a.py',
        'preview': 'content', 'preview_full_chars': 7,
    }])[0]
    assert projected['results'][0]['source'] == 'Flow'
    assert projected['toolContent'] == 'content'


def test_canonical_snapshot_and_projection_preserve_task_and_fallback_fields():
    from lib.swarm.agent import flow_canonical_public_round
    from lib.orchestration.chat_flow_projection import project_flow_tool_rounds

    snapshot = flow_canonical_public_round(
        {'roundNum': 4, 'toolName': 'read_files', 'status': 'done',
         'results': [{'toolName': 'read_files', 'title': 'a.py'}]},
        agent_id='agent-4', tool_call_id='flow-tool-4',
        tool_args={'path': 'a.py'}, tool_content='file body',
        task_id='task-parent')
    projected = project_flow_tool_rounds([{
        'round': 4, 'tool': 'read_files', 'agentId': 'agent-4',
        'tool_call_id': 'flow-tool-4', 'status': 'failed',
        'timestamp': 1_700_000_000, 'error': 'permission denied',
        'canonical_round': snapshot,
    }])[0]
    assert projected['taskId'] == 'task-parent'
    assert projected['toolArgs'] == {'path': 'a.py'}
    assert projected['results'] == snapshot['results']
    assert projected['toolContent'] == 'file body'
    assert projected['status'] == 'error'
    assert 'tStart' not in projected
    assert projected['tEnd'] == 1_700_000_000_000
