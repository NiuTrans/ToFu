"""Exact lazy result transport, immutable revision and explicit owner fences."""
import copy
import json

import pytest

pytest_plugins = ("tests._chat_sidecar",)

from lib.conversation_sync.tool_results import turns_with_lazy_tool_results
from lib.turn_projection_segments import public_value_with_stable_segments, snapshot_with_reference_tool_segments


def _snapshot(status='completed', tool_name='read_files', content=None):
    text = content if content is not None else ('完整输出 <script>原样</script>\n' * 400)
    round_record = {'toolCallId': 'call', 'toolName': tool_name, 'toolArgs': {'files': ['a.py']},
                    'status': 'done', 'toolContent': text, 'results': [{'path': 'a.py'}]}
    turn = {'conversationId': 'c', 'turnId': 't', 'presentationId': 't', 'status': status,
            'projectionRevision': 3, 'projection': {'toolRounds': [round_record], 'segments': [
                {'type': 'tool_use', 'blockId': 'call', 'id': 'call', 'name': tool_name,
                 'input': round_record['toolArgs'], 'result': {'status': 'done', 'content': text}}
            ]}}
    return {'conversationId': 'c', 'turns': public_value_with_stable_segments([turn])}


@pytest.mark.unit
def test_lazy_output_is_explicit_and_does_not_mutate_raw_or_refs_projection():
    source = _snapshot(); before = copy.deepcopy(source)
    refs = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24,
                                                lazy_tool_results=True)
    assert source == before
    assert refs['turns'][0]['projection']['toolRounds'][0]['toolContent'] == before['turns'][0]['projection']['toolRounds'][0]['toolContent']
    projected = lazy['turns'][0]['projection']; round_record = projected['toolRounds'][0]
    assert 'toolContent' not in round_record
    assert projected['segments'][0]['roundRef'] == 'call'
    reference = round_record['_snapshotToolResult']
    assert reference == {'conversationId': 'c', 'turnId': 't', 'toolCallId': 'call',
                         'roundIndex': 0, 'projectionRevision': 3, 'ownerScope': 'a' * 24,
                         'preview': before['turns'][0]['projection']['toolRounds'][0]['toolContent'][:240],
                         'chars': len(before['turns'][0]['projection']['toolRounds'][0]['toolContent'])}
    assert len(json.dumps(lazy)) < len(json.dumps(refs)) / 2


@pytest.mark.unit
@pytest.mark.parametrize('status,tool,content', [
    ('pending', 'read_files', None), ('running', 'read_files', None),
    ('failed', 'read_files', None), ('interrupted', 'read_files', None),
    ('completed', 'run_command', None), ('completed', 'read_files', ''),
    ('completed', 'read_files', None),
])
def test_lazy_output_preserves_patch_bases_specialized_cards_and_ambiguous_mirrors(status, tool, content):
    source = _snapshot(status, tool, content)
    if status == 'completed' and tool == 'read_files' and content is None:
        # A distinct segment result is a separate authority and cannot be elided.
        source['turns'][0]['projection']['segments'][0]['result']['content'] = 'different result'
    refs = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24,
                                                lazy_tool_results=True)
    assert '_snapshotToolResult' not in refs['turns'][0]['projection']['toolRounds'][0]
    assert refs['turns'][0]['projection']['toolRounds'][0]['toolContent'] == source['turns'][0]['projection']['toolRounds'][0]['toolContent']


@pytest.mark.unit
def test_duplicate_call_identity_cannot_publish_a_detail_reference():
    source = _snapshot(); source['turns'][0]['projection']['toolRounds'] *= 2
    transformed = turns_with_lazy_tool_results(source['turns'], conversation_id='c', owner_scope='a' * 24)
    assert transformed == source['turns']


@pytest.mark.unit
def test_unicode_preview_respects_browser_string_bound_and_preserves_full_output():
    original = ('😀' * 5000) + 'original ending'
    source = _snapshot(content=original)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24,
                                                lazy_tool_results=True)
    ref = lazy['turns'][0]['projection']['toolRounds'][0]['_snapshotToolResult']
    assert len(ref['preview'].encode('utf-16-le')) <= 480
    assert ref['chars'] == len(original)
    assert source['turns'][0]['projection']['toolRounds'][0]['toolContent'] == original


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_lazy_tool_result_http_reads_exact_bytes_and_rejects_wrong_owner_and_stale_revision(
    flask_client, chat_sidecar, monkeypatch,
):
    import uuid
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as routes
    client = get_storage_client(write=True); cid = 'lazy-result-' + uuid.uuid4().hex
    service = ConversationSyncService(SidecarConversationSyncRepository(client_factory=lambda write=False: client))
    monkeypatch.setattr(routes, '_service', service)
    client.command('conversation.create', {'conv_id': cid, 'user_id': 1, 'title': 'Lazy result',
                   'created_at': 1, 'updated_at': 1, 'settings': {}}, cid + '-create')
    try:
        content = 'Exact output <script>never execute</script>\n' * 500
        round_record = {'toolCallId': 'call', 'toolName': 'read_files', 'status': 'done',
                        'toolArgs': {'files': ['file.py']}, 'toolContent': content}
        client.command('turn.append_settled', {'conversation_id': cid, 'user_id': 1,
                       'command_id': cid + '-append', 'actor': 'assistant', 'kind': 'fixture',
                       'projection': {'toolRounds': [round_record]}, 'created_at': 2}, cid + '-append')
        root = f'/api/v4/conversations/{cid}'
        full = flask_client.get(root + '/sync').get_json()
        assert full['turns'][0]['projection']['toolRounds'][0]['toolContent'] == content
        lazy_response = flask_client.get(root + '/sync', query_string={'segmentPayload': 'refs', 'toolPayload': 'lazy'})
        assert lazy_response.status_code == 200, lazy_response.get_json()
        lazy = lazy_response.get_json()
        ref = lazy['turns'][0]['projection']['toolRounds'][0]['_snapshotToolResult']
        page = flask_client.get(root + '/turns/history', query_string={
            'laneId': 'main', 'syncSeq': lazy['syncSeq'], 'limit': 1,
            'segmentPayload': 'refs', 'toolPayload': 'lazy',
        })
        assert page.status_code == 200, page.get_json()
        assert page.get_json()['turns'][0]['projection']['toolRounds'][0]['_snapshotToolResult'] == ref
        complete = flask_client.get(f"{root}/turns/{ref['turnId']}")
        assert complete.status_code == 200, complete.get_json()
        assert complete.get_json()['turn']['projection']['toolRounds'][0]['toolContent'] == content
        assert '_snapshotToolResult' not in complete.get_json()['turn']['projection']['toolRounds'][0]
        assert complete.headers['Cache-Control'] == 'private, no-store'
        detail = f"{root}/turns/{ref['turnId']}/tool-results/0"
        fence = {'projectionRevision': ref['projectionRevision'], 'ownerScope': ref['ownerScope']}
        response = flask_client.get(detail, query_string=fence)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()['toolContent'] == content
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert flask_client.get(detail, query_string={**fence, 'ownerScope': '0' * 24}).status_code == 404
        assert flask_client.get(detail, query_string={**fence, 'projectionRevision': ref['projectionRevision'] + 1}).status_code == 409
        assert flask_client.get(detail).status_code == 400
        assert flask_client.get(root + '/sync?toolPayload=lazy').status_code == 400
        assert flask_client.get(root + '/sync?segmentPayload=refs&toolPayload=lazy&toolPayload=full').status_code == 400
        missing = flask_client.get(detail.rsplit('/', 1)[0] + '/999', query_string=fence)
        assert missing.status_code == 404
        other = service._repository.turn(cid, ref['turnId'], 2)
        assert other is None
    finally:
        client.command('conversation.purge', {'conv_id': cid, 'user_id': 1}, cid + '-purge')


@pytest.mark.api
@pytest.mark.auth_mode('open')
@pytest.mark.parametrize('status', ['completed', 'interrupted', 'truncated', 'failed'])
def test_refs_view_edit_round_trip_keeps_server_only_evidence(
    flask_client, chat_sidecar, monkeypatch, status,
):
    """A whole-projection edit built from the slim browser view loses nothing."""
    import uuid
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as routes
    client = get_storage_client(write=True); cid = 'refs-edit-' + uuid.uuid4().hex
    service = ConversationSyncService(SidecarConversationSyncRepository(client_factory=lambda write=False: client))
    monkeypatch.setattr(routes, '_service', service)
    client.command('conversation.create', {'conv_id': cid, 'user_id': 1, 'title': 'Refs edit',
                   'created_at': 1, 'updated_at': 1, 'settings': {}}, cid + '-create')
    signature = 'sig-' + 's' * 4_000
    try:
        round_record = {'toolCallId': 'call', 'toolName': 'search_files', 'status': 'done',
                        'toolArgs': {'q': 'x'}, 'toolContent': 'hit', 'llmRound': 0,
                        'thinking': 'plan', 'thinkingSignature': signature,
                        '_anthropicContentBlocks': [{'type': 'thinking', 'thinking': 'plan',
                                                     'signature': signature}]}
        projection = {
            'content': 'answer',
            'toolRounds': [round_record],
            'segments': [
                {'type': 'thinking', 'blockId': 'thinking:llm-0', 'text': 'plan',
                 'signature': signature, 'llmRound': 0},
                {'type': 'tool_use', 'blockId': 'tool:call', 'id': 'call', 'name': 'search_files',
                 'input': {'q': 'x'}, 'result': {'content': 'hit', 'status': 'done'}, 'llmRound': 0},
                {'type': 'text', 'blockId': 'text:terminal', 'text': 'answer', 'terminal': True},
            ],
            'apiRounds': [{'round': 1, 'usage': {'prompt_tokens': 3},
                           'promptAdmission': {'action': 'admit', 'totalTokens': 3,
                                               'measurementVersion': 'tofu.prompt-admission/v2'}}],
        }
        client.command('turn.append_settled', {'conversation_id': cid, 'user_id': 1,
                       'command_id': cid + '-append', 'actor': 'assistant', 'kind': 'fixture',
                       'projection': projection, 'status': status, 'created_at': 2}, cid + '-append')
        root = f'/api/v4/conversations/{cid}'
        snapshot = flask_client.get(root + '/sync', query_string={
            'segmentPayload': 'refs', 'toolPayload': 'lazy'}).get_json()
        turn = snapshot['turns'][0]
        assert turn['status'] == status
        browser = turn['projection']
        assert 'signature' not in json.dumps(browser['segments'])
        assert 'thinkingSignature' not in browser['toolRounds'][0]
        assert browser['apiRounds'][0]['promptAdmission'] == {'action': 'admit', 'totalTokens': 3}
        # The generated client restores roundRef segments before TurnStore.
        materialized = [
            {**segment, 'input': browser['toolRounds'][0]['toolArgs'],
             'result': {'content': 'hit', 'status': 'done'}}
            if segment.get('roundRef') else segment
            for segment in browser['segments']
        ]
        for segment in materialized:
            segment.pop('roundRef', None)
        edited = {**browser, 'segments': materialized, 'content': 'edited answer'}
        response = flask_client.patch(f"{root}/turns/{turn['turnId']}", json={
            'expectedProjectionRevision': turn['projectionRevision'], 'projection': edited})
        assert response.status_code == 200, response.get_json()
        stored = flask_client.get(f"{root}/turns/{turn['turnId']}").get_json()['turn']['projection']
        assert stored['content'] == 'edited answer'
        thinking = next(s for s in stored['segments'] if s['type'] == 'thinking')
        assert thinking['signature'] == signature
        assert stored['toolRounds'][0]['thinkingSignature'] == signature
        assert stored['toolRounds'][0]['_anthropicContentBlocks'] == round_record['_anthropicContentBlocks']
        assert stored['apiRounds'][0]['promptAdmission']['measurementVersion'] == 'tofu.prompt-admission/v2'
    finally:
        client.command('conversation.purge', {'conv_id': cid, 'user_id': 1}, cid + '-purge')


def _command_snapshot(**metadata):
    source = _snapshot(tool_name='run_command', content='successful stdout\n' * 500)
    source['turns'][0]['projection']['toolRounds'][0]['results'] = [{
        'exitCode': 0, 'command': 'printf test', 'description': 'test command',
        'output': 'structured stdout <script>never execute</script>\n' * 500,
        **metadata,
    }]
    return source


def _producer_command_snapshot(tool):
    """The exact result shape run_command/code_exec producers persist."""
    source = _snapshot(tool_name=tool, content='$ make test\n' + 'ok\n' * 2_000 + '[exit code: 0]')
    round_record = source['turns'][0]['projection']['toolRounds'][0]
    round_record['results'] = [{
        'toolName': tool, 'command': 'make test', 'description': 'run tests',
        'timedOut': False, 'interrupted': False, 'output': 'ok\n' * 2_000,
        'exitCode': '0', 'snippet': '$ make test', 'badge': 'done',
    }]
    source['turns'][0]['projection']['segments'][0]['name'] = tool
    return source


@pytest.mark.unit
@pytest.mark.parametrize('tool', ['run_command', 'code_exec'])
def test_producer_string_zero_exit_command_is_deferred_with_exact_detail(tool):
    """Producers stamp the decimal they parsed from the executor marker."""
    from lib.conversation_sync.tool_results import tool_result_from_turn
    source = _producer_command_snapshot(tool)
    before = copy.deepcopy(source)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24, lazy_tool_results=True)
    value = lazy['turns'][0]['projection']['toolRounds'][0]
    assert value['_snapshotToolResult']['fields'] == ['toolContent', 'results']
    assert 'toolContent' not in value and 'output' not in value['results'][0]
    assert value['results'][0]['exitCode'] == '0'
    assert value['results'][0]['badge'] == 'done'
    detail = tool_result_from_turn(source['turns'][0], conversation_id='c', turn_id='t',
                                   round_index=0, projection_revision=3)
    assert detail['toolContent'] == before['turns'][0]['projection']['toolRounds'][0]['toolContent']
    assert detail['results'] == before['turns'][0]['projection']['toolRounds'][0]['results']
    assert source == before


@pytest.mark.unit
@pytest.mark.parametrize('exit_code', ['1', '-1', 'timeout', 'not-run', 'background', '?', ' 0', '00', '0.0'])
def test_producer_string_nonzero_or_unknown_exit_stays_inline(exit_code):
    source = _producer_command_snapshot('run_command')
    source['turns'][0]['projection']['toolRounds'][0]['results'][0]['exitCode'] = exit_code
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24, lazy_tool_results=True)
    value = lazy['turns'][0]['projection']['toolRounds'][0]
    assert '_snapshotToolResult' not in value
    assert value['results'][0]['output'] == 'ok\n' * 2_000


@pytest.mark.unit
@pytest.mark.parametrize('tool', ['run_command', 'code_exec'])
def test_successful_command_defers_both_output_mirrors_and_restores_exact_results(tool):
    from lib.conversation_sync.tool_results import tool_result_from_turn
    source = _command_snapshot()
    turn = source['turns'][0]
    round_record = turn['projection']['toolRounds'][0]
    round_record['toolName'] = tool
    turn['projection']['segments'][0]['name'] = tool
    before = copy.deepcopy(source)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24, lazy_tool_results=True)
    value = lazy['turns'][0]['projection']['toolRounds'][0]
    assert 'toolContent' not in value
    assert 'output' not in value['results'][0]
    assert value['results'][0]['exitCode'] == 0
    assert value['results'][0]['description'] == 'test command'
    assert value['_snapshotToolResult']['fields'] == ['toolContent', 'results']
    assert len(json.dumps(lazy)) < len(json.dumps(source)) / 3
    detail = tool_result_from_turn(turn, conversation_id='c', turn_id='t', round_index=0, projection_revision=3)
    assert detail['toolContent'] == round_record['toolContent']
    assert detail['results'] == round_record['results']
    assert source == before


@pytest.mark.unit
@pytest.mark.parametrize('metadata', [
    {'exitCode': 1}, {'exitCode': '1'}, {'exitCode': True}, {'exitCode': None},
    {'exitCode': 0.0}, {'exitCode': False},
    {'timedOut': True}, {'interrupted': True}, {'notRun': True},
    {'backgroundCompleted': True}, {'status': 'error'}, {'rejected': True},
    {'error': 'execution failed'}, {'_rejected': True}, {'rejection': 'approval denied'},
])
def test_command_failures_and_ambiguous_status_never_become_deferred_success(metadata):
    source = _command_snapshot(**metadata)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24, lazy_tool_results=True)
    value = lazy['turns'][0]['projection']['toolRounds'][0]
    assert '_snapshotToolResult' not in value
    assert value['toolContent'] == source['turns'][0]['projection']['toolRounds'][0]['toolContent']
    assert value['results'][0]['output'] == source['turns'][0]['projection']['toolRounds'][0]['results'][0]['output']


@pytest.mark.unit
@pytest.mark.parametrize('tool', ['read_tool_artifact', 'search_tool_artifact', 'repo_map'])
def test_generic_evidence_tools_use_existing_exact_text_reference(tool):
    source = _snapshot(tool_name=tool)
    lazy = snapshot_with_reference_tool_segments(source, owner_cache_scope='a' * 24, lazy_tool_results=True)
    assert '_snapshotToolResult' in lazy['turns'][0]['projection']['toolRounds'][0]
