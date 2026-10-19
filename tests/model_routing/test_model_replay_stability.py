"""Provider-free regressions for exact replay and cache-stable request boundaries."""
import copy
import json
import pytest
pytestmark = pytest.mark.unit


@pytest.fixture
def replay_store(monkeypatch, tmp_path):
    from lib.storage.model_replay_cache import ModelReplayCache
    from lib.tasks_pkg import model_replay
    store = ModelReplayCache(root=tmp_path / 'replay', max_bytes=2_000_000)
    monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: store)
    return store


def _turn(key, actor, content, ordinal, *, status='completed'):
    return {'turnId': key, 'actor': actor, 'laneId': 'main', 'ordinal': ordinal,
            'parentTurnId': '', 'status': status, 'projection': {'content': content}}


def test_pre_fix_projection_checkpoint_is_not_restored(replay_store):
    from lib.tasks_pkg.model_replay import restore_prefix, turn_witness
    prior = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    # Archived version-1 policy witness for {'model': 'kimi-k3'}, before
    # replay projection semantics were part of the witness. Old retained
    # working contexts may contain delegated rows promoted to model actions.
    legacy_policy = '3a1ab66f8bdd5859a0917025b4f12df759d66af8998df2d1ad0f681d244ed3de'
    projection = {'policy': legacy_policy,
                  'turns': [turn_witness(row) for row in prior],
                  'messages': [{'role': 'user', 'content': 'work'},
                               {'role': 'assistant', 'content': 'old derived context'}]}
    replay_store.put(user_id=1, conversation_id='conv', lane_id='main', projection=projection)
    config = {'model': 'kimi-k3'}
    pending = _turn('pending', 'assistant', '', 3, status='running')
    assert restore_prefix(prior + [pending], config,
                          conversation_id='conv', user_id=1) == ([], 0)
    assert config['_modelReplayStatus'] == 'policy_changed'
    # Invalidate derived reuse, preserving both the durable transcript and
    # the old checkpoint for diagnosis until normal replacement/retention.
    assert replay_store.get(user_id=1, conversation_id='conv', lane_id='main') == projection


@pytest.mark.parametrize('model', ['kimi-k3', 'claude-opus-4-6', 'gpt-5.6-sol'])
@pytest.mark.parametrize('compacted', [False, True])
def test_turn_boundary_restores_composer_and_compacted_prefix(
        monkeypatch, replay_store, model, compacted):
    from lib import turn_lifecycle, storage
    from lib.tasks_pkg.model_replay import publish_completed_task
    from lib.tasks_pkg.context_composer import ComposeRequest, ContextBlock, render_context
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    rows = [_turn('u1', 'human', 'request', 1),
            _turn('a1', 'assistant', '', 2, status='running')]
    class Client:
        def query(self, operation, payload, **kwargs):
            assert payload['user_id'] == 1
            if operation == 'turn.get':
                return copy.deepcopy(next(row for row in rows if row['turnId'] == payload['turn_id']))
            return copy.deepcopy(rows)
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client())
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: Client())
    config = {'model': model, 'systemPrompt': 'operator policy', 'excludeLast': True}
    live = turn_lifecycle.build_api_messages('conv', 'a1', config, user_id=1)
    live[1]['content'] = 'hook-filtered request'
    block = ContextBlock(id='rules', source='test', content='constant rules',
                         authority='project', placement='tail', stability='conversation',
                         lifecycle='conversation')
    render_context(live, [block], ComposeRequest(model=model))
    rounds = [_row(i, content='readable source line ' * 250) for i in range(43 if compacted else 1)]
    live.extend(_replay(rounds))
    before_compact = copy.deepcopy(live)
    if compacted:
        assert micro_compact(live) > 0
        assert before_compact != live
    sent = _wire(live, model)
    live.append({'role': 'assistant', 'content': 'done'})
    rows[1].update(status='completed', projection={'content': 'done', 'toolRounds': rounds})
    task = {'_userId': 1, 'convId': 'conv', '_turnId': 'a1', 'model': model,
            'config': config, 'messages': live, 'content': 'done'}
    publish_completed_task(task)
    rows[1]['projection']['timingTrace'] = {'browserRenderedAt': 1234}
    rows[1]['projection']['translation'] = {'status': 'completed', 'text': 'display only'}
    rows.extend([_turn('u2', 'human', 'follow-up', 3),
                 _turn('a2', 'assistant', '', 4, status='running')])
    next_config = {'model': model, 'systemPrompt': 'operator policy', 'excludeLast': True}
    rebuilt = turn_lifecycle.build_api_messages('conv', 'a2', next_config, user_id=1)
    assert next_config['_modelReplayStatus'] == 'restored'
    render_context(rebuilt, [block], ComposeRequest(model=model))
    assert _wire(rebuilt, model)[:len(sent)] == sent
    assert rows[1]['projection']['toolRounds'] == rounds
    # User edits invalidate cached redactions/context along with the old epoch.
    rows[0]['projection']['content'] = 'edited request'
    edited_config = {'model': model, 'systemPrompt': 'operator policy', 'excludeLast': True}
    turn_lifecycle.build_api_messages('conv', 'a2', edited_config, user_id=1)
    assert edited_config['_modelReplayStatus'] == 'authority_changed'


def test_replay_repository_bounds_ownership_expiry_and_restart(tmp_path):
    from lib.storage.model_replay_cache import ModelReplayCache
    clock = [100.0]
    store = ModelReplayCache(root=tmp_path, max_bytes=1800, clock=lambda: clock[0])
    for n in range(30):
        assert store.put(user_id=1, conversation_id=str(n), lane_id='main',
                         projection={'content': 'x' * 600})
    assert sum(p.stat().st_size for p in tmp_path.glob('*.json')) <= 1800
    other = ModelReplayCache(root=tmp_path, max_bytes=1800, clock=lambda: clock[0])
    assert other.get(user_id=1, conversation_id='29', lane_id='main') == {'content': 'x' * 600}
    assert other.get(user_id=2, conversation_id='29', lane_id='main') is None
    assert other.get(user_id=1, conversation_id='29', lane_id='branch') is None
    assert not store.put(user_id=1, conversation_id='huge', lane_id='main',
                         projection={'content': 'x' * 2000})
    clock[0] += 86401
    assert other.get(user_id=1, conversation_id='29', lane_id='main') is None


@pytest.mark.parametrize('changed', [
    {'model': 'gpt-4o'}, {'systemPrompt': 'new policy'},
    {'grantedToolPermissions': ['new-scope']}, {'memoryEnabled': False},
    {'responses': {'imageDetail': 'original'}}, {'orchestration': {'multiAgent': 'read_only'}},
])
def test_policy_changes_invalidate_retained_views(replay_store, changed):
    from lib.tasks_pkg.model_replay import policy_witness, restore_prefix, turn_witness
    prior = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'kimi-k3'}
    replay_store.put(user_id=1, conversation_id='conv', lane_id='main', projection={
        'policy': policy_witness(original), 'turns': [turn_witness(row) for row in prior],
        'messages': [{'role': 'user', 'content': 'work'}, {'role': 'assistant', 'content': 'done'}],
    })
    config = {**original, **changed}
    messages, covered = restore_prefix(prior + [_turn('a2', 'assistant', '', 3)], config,
                                       conversation_id='conv', user_id=1)
    assert messages == [] and covered == 0
    assert config['_modelReplayStatus'] == 'policy_changed'


def test_replay_restores_latch_without_claiming_declaration_authority(monkeypatch, replay_store):
    from lib.mcp import tool_search
    from lib.storage.mcp_declarations import MCPDeclarationStore
    from lib.tasks_pkg.model_replay import policy_witness, restore_prefix, turn_witness

    def no_declaration_access(*args, **kwargs):
        pytest.fail('Replay must neither read nor overwrite conversation declarations')

    monkeypatch.setattr(MCPDeclarationStore, 'get', no_declaration_access)
    monkeypatch.setattr(MCPDeclarationStore, 'freeze', no_declaration_access)
    monkeypatch.setattr(tool_search, 'freeze_wire_definitions', no_declaration_access)
    tools = [{'type': 'function', 'function': {'name': 'mcp__test__read',
              'description': 'legacy replay copy', 'parameters': {'type': 'object'}}}]
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    cfg = {'model': 'kimi-k3'}
    replay_store.put(user_id=1, conversation_id='mcp-test', lane_id='main', projection={
        'policy': policy_witness(cfg), 'turns': [turn_witness(row) for row in rows],
        'messages': [{'role': 'assistant', 'content': 'done'}], 'mcpWire': tools,
        'toolLatch': {'programmaticCalling': 'on'},
    })
    restored, covered = restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                      conversation_id='mcp-test', user_id=1)
    assert restored == [{'role': 'assistant', 'content': 'done'}]
    assert covered == 2
    assert cfg['_modelReplayStatus'] == 'restored'
    assert cfg['_modelReplayToolLatch'] == {'programmaticCalling': 'on'}


def test_cache_write_failure_keeps_previous_entry_and_count_is_bounded(monkeypatch, tmp_path):
    from lib.storage import model_replay_cache as module
    monkeypatch.setattr(module, 'MAX_ENTRIES', 3)
    store = module.ModelReplayCache(root=tmp_path, max_bytes=100_000)
    for i in range(12):
        assert store.put(user_id=1, conversation_id=str(i), lane_id='main', projection={'i': i})
    assert len(list(tmp_path.glob('*.json'))) == 3
    def fail(*args, **kwargs):
        raise OSError('injected disk failure')
    monkeypatch.setattr(module, 'write_bytes_atomic', fail)
    with pytest.raises(OSError):
        store.put(user_id=1, conversation_id='11', lane_id='main', projection={'i': 'new'})
    assert store.get(user_id=1, conversation_id='11', lane_id='main') == {'i': 11}


def test_valid_json_cache_corruption_is_a_cold_miss(tmp_path):
    from lib.storage.model_replay_cache import ModelReplayCache
    store = ModelReplayCache(root=tmp_path, max_bytes=10_000)
    store.put(user_id=1, conversation_id='conv', lane_id='main', projection={'text': 'original'})
    entry = next(tmp_path.glob('*.json'))
    damaged = entry.read_bytes().replace(b'original', b'tampered')
    json.loads(damaged)  # Parse success alone is insufficient integrity evidence.
    entry.write_bytes(damaged)
    assert store.get(user_id=1, conversation_id='conv', lane_id='main') is None


def test_local_image_version_is_pinned_in_the_task_copy(monkeypatch, tmp_path):
    from lib.runtime import runtime_paths
    from lib.tasks_pkg.model_images import pin_model_images
    monkeypatch.setattr(runtime_paths, 'uploads_root', lambda: str(tmp_path))
    folder = tmp_path / 'images'
    folder.mkdir()
    path = folder / 'input.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'A' * 100)
    source = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
        'url': '/api/images/input.png'}}]}]
    working = copy.deepcopy(source)
    pin_model_images(working, model='kimi-k3', user_id=1)
    sent = _wire(working, 'kimi-k3')
    path.unlink()
    pin_model_images(working, model='kimi-k3', user_id=1)
    assert _wire(working, 'kimi-k3') == sent
    assert source[0]['content'][0]['image_url']['url'] == '/api/images/input.png'


def test_read_only_hooks_cannot_mutate_nested_history(monkeypatch):
    from lib.tasks_pkg import tool_hooks
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'original'}]}]
    task = {'messages': messages, 'toolRounds': [{'toolContent': 'evidence'}]}
    before = copy.deepcopy(task)
    def observer(view, carrier):
        carrier['messages'][0]['content'][0]['text'] = 'mutated'
        carrier['toolRounds'][0]['toolContent'] = 'mutated'
        view.clear()
    monkeypatch.setattr(tool_hooks, '_pre_compact_hooks', [observer])
    tool_hooks.run_pre_compact_hooks(messages, task)
    assert task == before


def test_screenshot_model_blocks_survive_settlement_segments_and_continue(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tasks_pkg import segments
    from lib.tool_results.tool_history_projection import build_tool_history_round
    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    row = _row(1, 'browser_screenshot')
    task = {'id': 'transient', 'model': 'gpt-4o', 'config': {}, 'toolRounds': [row]}
    shot = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,AAA',
            '_text_fallback': 'Screenshot captured.'}
    pipeline._prepare_screenshot_replay(task, row, shot, 'gpt-4o', 'browser_screenshot', row['toolCallId'])
    live = []
    pipeline._append_screenshot_message(live, row['toolCallId'], shot)
    task['_userId'] = 1
    pipeline._settle_tool_result(task, 'browser_screenshot', row['toolCallId'], {}, 1,
        row, 'Screenshot captured.', idempotent_tools=frozenset(), cache={},
        tid='transient', round_num=1, settled_results={})
    assert _replay([row])[-1]['content'] == live[0]['content']
    assert build_tool_history_round([row])['toolResults'][0]['content'] == live[0]['content']
    segment = segments.tool_use_segment_from_round(row, 0)
    assert segments.reconstruct_tool_messages_from_segments([segment])[-1]['content'] == live[0]['content']


def test_screenshot_retains_owned_images_without_copying_base64_into_turns(monkeypatch, tmp_path):
    import base64
    from lib import media_attachments, knowledge
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tasks_pkg.model_images import pin_model_images
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (2, 3), 'blue').save(buffer, format='PNG')
    raw = buffer.getvalue()
    path = tmp_path / 'source.png'
    path.write_bytes(raw)
    def ingest(data, mime, **kwargs):
        assert data == raw and kwargs['user_id'] == 1
        return {'attachmentId': 'owned', 'sourceTool': kwargs['source_tool'],
                'toolCallId': kwargs['tool_call_id']}
    monkeypatch.setattr(media_attachments, 'ingest_mcp_image', ingest)
    monkeypatch.setattr(knowledge, 'read_source_path',
                        lambda key, *, user_id: path if key == 'owned' and user_id == 1 else None)
    task = {'id': 'test', '_userId': 1, 'config': {}}
    row = _row(1, 'browser_screenshot')
    shot = {'__screenshot__': True,
            'dataUrl': 'data:image/png;base64,' + base64.b64encode(raw).decode()}
    pipeline._prepare_screenshot_replay(task, row, shot, 'gpt-4o', 'browser_screenshot', row['toolCallId'])
    assert 'base64,' not in json.dumps(row)
    assert not task.get('_mcpImages')  # no duplicate assistant image gallery
    live = []
    pipeline._append_screenshot_message(live, row['toolCallId'], shot)
    replayed = _replay([row])
    pin_model_images(live, model='kimi-k3', user_id=1)
    pin_model_images(replayed, model='kimi-k3', user_id=1)
    assert live[0]['content'] == replayed[-1]['content']
    foreign = _replay([row])
    pin_model_images(foreign, model='kimi-k3', user_id=2)
    assert all(block['type'] == 'text' for block in foreign[-1]['content'])


def test_image_store_failure_has_a_bounded_replay_fallback_without_blinding_live_model(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tasks_pkg import mcp_result_media
    monkeypatch.setattr(mcp_result_media, 'capture_mcp_result_images', lambda *args, **kwargs: 0)
    task = {'_userId': 1}
    inline_chars = 0
    for index in range(8):
        row = _row(index, 'browser_screenshot')
        shot = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,' + 'A' * 200_000}
        pipeline._prepare_screenshot_replay(task, row, shot, 'gpt-4o', 'browser_screenshot', row['toolCallId'])
        inline_chars += sum(len(block['image_url']['url']) for block in row['toolModelContent']
                            if block.get('type') == 'image_url')
        live = []
        pipeline._append_screenshot_message(live, row['toolCallId'], shot)
        assert live[0]['content'][0]['type'] == 'image_url'
    assert inline_chars <= 1024 * 1024
    assert row['toolModelContent'][0]['type'] == 'text'
    assert 'cold replay' in row['toolModelContent'][0]['text']


def test_transient_screenshots_do_not_require_media_storage(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tasks_pkg import mcp_result_media
    def forbidden(*args, **kwargs):
        pytest.fail('transient image invoked durable media storage')
    monkeypatch.setattr(mcp_result_media, 'capture_mcp_result_images', forbidden)
    task = {'_userId': 1, '_transientRuntime': True}
    row = _row(1, 'browser_screenshot')
    shot = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,AAA'}
    pipeline._prepare_screenshot_replay(task, row, shot, 'gpt-4o', 'browser_screenshot', row['toolCallId'])
    assert row['toolModelContent'][0]['type'] == 'image_url'


def _row(index, name='run_command', content='result'):
    return {'toolCallId': f'audit_call_{index}', 'toolName': name, 'toolArgs': json.dumps({'audit_index': index}), 'toolContent': content, 'llmRound': index, 'roundNum': index, 'status': 'done', 'taskId': 'synthetic-audit-task', 'assistantContent': f'step {index}'}

def _replay(rows):
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    return _reconstruct_tool_call_messages(rows) or []

def _wire(messages, model):
    from lib.llm import build_body
    body = build_body(model, messages, max_tokens=256, provider_id='audit-offline')
    if model.startswith('claude'):
        from lib.llm.anthropic_outbound import openai_body_to_anthropic
        return openai_body_to_anthropic(body)['messages']
    if model.startswith('gpt'):
        from lib.llm.responses_outbound import openai_body_to_responses
        converted, _ = openai_body_to_responses(body)
        return converted['input']
    return body['messages']

def _first_difference(before, after):
    return next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))

@pytest.mark.parametrize('model', ['kimi-k3', 'claude-opus-4-6', 'gpt-5.6-sol'])
def test_todo_replay_preserves_previously_sent_calls(model):
    rows = [_row(i, 'todo_write' if i % 2 else 'run_command') for i in range(1, 7)]
    original = copy.deepcopy(rows)
    live = [{'role': 'user', 'content': 'work'}]
    for row in rows:
        live.extend(_replay([row]))
    rebuilt = [{'role': 'user', 'content': 'work'}] + _replay(rows)
    before, after = (_wire(live, model), _wire(rebuilt, model))
    assert after == before
    assert rows == original
    assert sum((r['toolName'] == 'todo_write' for r in rows)) == 3

def test_attachment_append_preserves_earlier_document(monkeypatch):
    from lib import media_attachments as media
    from lib import knowledge
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    monkeypatch.setattr(media, 'resolve_client_refs', lambda items, **kw: copy.deepcopy(items))
    monkeypatch.setattr(media, 'document_text_request_cap', lambda model: 1200)
    monkeypatch.setattr(media, 'document_text_budget', lambda model: 1200)
    monkeypatch.setattr(knowledge, 'list_document_assets', lambda *a, **kw: [])
    budgets = []

    def chunks(attachment_id, *, char_cap, **kwargs):
        budgets.append((attachment_id, char_cap))
        return ([{'content': 'X' * char_cap}], [], 'full', char_cap, 1200)
    monkeypatch.setattr(media, '_bounded_chunks', chunks)

    def user(attachment_id):
        return {'role': 'user', 'content': 'read document', 'attachments': [{'attachmentId': attachment_id, 'status': 'ready', 'kind': 'document', 'name': attachment_id + '.txt'}]}
    first = _transform_messages([user('doc1')], {'model': 'kimi-k2.5'}, user_id=1)
    second = _transform_messages([user('doc1'), {'role': 'assistant', 'content': 'ok'}, user('doc2')], {'model': 'kimi-k2.5'}, user_id=1)
    assert first[0] == second[0]
    assert budgets == [('doc1', 1200), ('doc1', 1200)]

def test_settlement_replay_preserves_budget_compliant_source_result(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    monkeypatch.setattr(pipeline, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, '_tool_result_recovery_policy', lambda *a, **kw: 'source')
    row = _row(1, 'read_files')
    task = {'id': 'offline-audit', '_userId': 1, 'model': 'kimi-k2.5', 'config': {}, 'toolRounds': [row]}
    live = pipeline._settle_tool_result(task, 'read_files', row['toolCallId'], {'paths': ['audit.txt']}, 1, row, 'readable source line\n' * 3300, idempotent_tools=frozenset(), cache={}, tid='offline-audit', round_num=1, settled_results={})
    assert len(live) > 50000
    stored = row['toolContent']
    assert stored == live
    assert _replay([row])[-1]['content'] == stored

def test_oauth_preserves_policy_with_unrelated_reminders():
    from lib.oauth.outbound import apply_claude_cloak
    original = {'model': 'claude-opus-4-6', 'system': 'UNIQUE OPERATOR RULE', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'work'}]}]}
    first, _ = apply_claude_cloak(copy.deepcopy(original))
    second_source = copy.deepcopy(original)
    second_source['messages'].extend([{'role': 'assistant', 'content': [{'type': 'text', 'text': 'progress'}]}, {'role': 'user', 'content': [{'type': 'text', 'text': '<system-reminder>new context</system-reminder>'}]}])
    second, _ = apply_claude_cloak(second_source)
    assert 'UNIQUE OPERATOR RULE' in json.dumps(first['messages'])
    assert 'UNIQUE OPERATOR RULE' in json.dumps(second)

def test_l1_preserves_the_entire_unseen_image_batch():
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    image = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAA'}}]
    calls = [{'id': f'shot_{i}', 'type': 'function', 'function': {'name': 'browser_screenshot', 'arguments': '{}'}} for i in range(3)]
    messages = [{'role': 'user', 'content': 'compare all three screenshots'}, {'role': 'assistant', 'tool_calls': calls}]
    messages.extend(({'role': 'tool', 'tool_call_id': call['id'], 'content': copy.deepcopy(image)} for call in calls))
    micro_compact(messages)
    assert isinstance(messages[2]['content'], list)
    assert isinstance(messages[3]['content'], list)
    messages.append({'role': 'user', '_contextComposer': True, 'content': 'new context'})
    micro_compact(messages)
    assert isinstance(messages[2]['content'], list)
    messages.append({'role': 'assistant', 'content': 'I have examined the images.'})
    micro_compact(messages)
    assert isinstance(messages[2]['content'], str)

@pytest.mark.parametrize('model', ['glm-5', 'qwen-max', 'deepseek-v3'])
def test_final_wire_text_shape_survives_marker_movement(model):
    from lib.llm import build_body
    from lib.llm._sse_core import prepare_request
    original = [{'role': 'system', 'content': 'fixed policy'}, {'role': 'user', 'content': 'first question'}]
    later = original + [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'follow-up'}]

    def wire(messages):
        body = build_body(model, copy.deepcopy(messages), max_tokens=256, provider_id='audit-offline')
        return prepare_request(body, api_key='offline-placeholder', api_protocol='openai', base_url='https://example.invalid/v1').body['messages']
    first, second = (wire(original), wire(later))
    assert isinstance(first[1]['content'], list)
    assert isinstance(second[1]['content'], list)


def test_completed_replay_keeps_child_context_without_delivery_authority(replay_store, monkeypatch):
    from lib.tasks_pkg.model_replay import policy_witness, restore_prefix, turn_witness
    from lib.tasks_pkg.message_input_receipts import remember, stream_with_receipts
    from lib.swarm.run_messages import confirm_input
    from lib.swarm.run_messages import RunMessageRepository
    prior = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    cfg = {'model': 'kimi-k3'}
    carrier = {'role': 'user', '_isMeta': True,
               'content': '<agent-message>nonhuman historical evidence</agent-message>',
               '_swarmMessageIds': ['old-message'], '_swarmMessageMailbox': 'old-run'}
    projection = {'policy': policy_witness(cfg), 'turns': [turn_witness(row) for row in prior],
                  'messages': [carrier, {'role': 'assistant', 'content': 'done'}]}
    replay_store.put(user_id=1, conversation_id='timer-conv', lane_id='main', projection=projection)
    def no_mailbox_io(*args, **kwargs):
        pytest.fail('Historical projection must not consume or rebind a mailbox')
    monkeypatch.setattr(RunMessageRepository, 'load', no_mailbox_io)
    monkeypatch.setattr(RunMessageRepository, 'confirm', no_mailbox_io)
    rebuilt, covered = restore_prefix(prior + [_turn('a2', 'assistant', '', 3)], cfg,
                                      conversation_id='timer-conv', user_id=1)
    assert covered == 2
    assert rebuilt == [{key: value for key, value in carrier.items()
                        if key not in {'_swarmMessageIds', '_swarmMessageMailbox'}},
                       {'role': 'assistant', 'content': 'done'}]
    task = {'id': 'new-timer-root'}
    remember(task, rebuilt)
    seen = []
    def provider(task, body):
        seen.extend(body['messages'])
        from lib.llm.stream_result import ProviderStreamResult
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'continued'}, 'stop', None)
    assert stream_with_receipts(provider, task, {'messages': rebuilt}).is_verified_complete
    confirm_input(task, round_num=0)
    assert seen == rebuilt
    assert '_swarmMessageBinding' not in task
    assert task['_verifiedMessageInputIds'] == {'swarm': [], 'project': []}
    assert replay_store.get(user_id=1, conversation_id='timer-conv', lane_id='main') == projection
