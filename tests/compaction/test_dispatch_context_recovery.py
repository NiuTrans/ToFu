"""Final admission and Continue preserve observations without context explosion."""
from copy import deepcopy
import json
import threading

import pytest

pytestmark = pytest.mark.unit


def _round(index, text=None):
    return {'toolName': 'read_files', 'toolCallId': f'c{index}',
            'llmRound': index,
            'toolArgs': {'paths': [f'file{index}']},
            'toolContent': text or f'observation {index}', 'status': 'completed'}


def _task():
    return {'id': 'task', 'convId': 'conversation', '_userId': 1,
            '_turnId': 'turn', '_attemptId': 'attempt', 'toolRounds': [],
            'config': {'model': 'gpt-6-astra', '_modelReplayBase': [['human', 'a' * 64]]},
            'content': '', 'thinking': '', 'content_lock': threading.Lock()}


def test_successful_dispatch_calibrates_next_final_admission(monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    from lib.tasks_pkg.compaction import _tokens as tokens
    from lib.tasks_pkg.llm_fallback import _usage as usage
    import lib.token_counter
    task = _task()
    counts = iter([45_000, 53_878])
    monkeypatch.setattr(lib.token_counter, 'count_tokens',
                        lambda *a, **k: {'tokens': next(counts), 'method': 'tiktoken'})
    monkeypatch.setattr(tokens, '_estimate_total_tokens', lambda *a: 133_657)
    monkeypatch.setattr(admission, '_resolved_ceiling', lambda *a, **k: (80_640, 80_640))
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('healthy prompt must not need a summary'))
    monkeypatch.setattr(usage, 'append_event', lambda *a, **k: None)
    messages = [{'role': 'user', 'content': 'current work'}]
    first = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=0, model='gpt-6-astra')
    assert first['totalTokens'] == 76_500
    usage._emit_round_usage(task, 1, 'gpt-6-astra', {
        'prompt_tokens': 46_000, 'prompt_tokens_details': {'cached_tokens': 40_000},
        'completion_tokens': 10})
    assert task['_lastStreamGroundTruth']['prompt_tokens'] == 46_000
    second = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=1, model='gpt-6-astra')
    assert second['totalTokens'] < 80_640
    assert second['method'].endswith('+cal')
    assert messages == [{'role': 'user', 'content': 'current work'}]
    assert '_rawEstimateTokens' not in task['_lastPromptAdmission']


def test_discarded_billed_attempt_does_not_calibrate_authoring_prompt(monkeypatch):
    from lib.tasks_pkg.llm_fallback import _usage as usage
    task = _task()
    monkeypatch.setattr(usage, 'append_event', lambda *a, **k: None)
    usage._emit_round_usage(task, 1, 'fallback', {'prompt_tokens': 90_000},
                           response_authoring=False)
    assert '_lastStreamGroundTruth' not in task


def test_reactive_body_rebuild_cannot_calibrate_the_original_request(monkeypatch):
    from lib.tasks_pkg.compaction._calibration import update_and_stash_sample, calibration_ratio
    from lib.tasks_pkg.llm_fallback import _call, _usage
    task = _task()
    model = task['config']['model']
    update_and_stash_sample(task, model=model, raw_estimate=100_000, method='tiktoken')
    monkeypatch.setattr(_usage, 'append_event', lambda *a, **k: None)
    _call._record_reactive_usage(task, {'prompt_tokens': 20_000}, model, 0, {}, [])
    update_and_stash_sample(task, model=model, raw_estimate=30_000, method='tiktoken')
    assert calibration_ratio(task, model=model, method='tiktoken') is None


@pytest.mark.parametrize('retention', ['success', 'failure', 'cancel'])
def test_oversized_latest_batch_is_recoverable_and_keeps_errors(monkeypatch, retention):
    from lib.tasks_pkg.compaction._pressure import retain_oversized_tool_batch
    from lib.tasks_pkg.compaction import _budget as budget
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    row = _round(1, 'LATEST_FAILURE_DETAIL\n' + 'received evidence\n' * 5_000)
    row['status'] = 'error'
    task['toolRounds'] = [row]
    messages = [{'role': 'user', 'content': 'preserve objective'}]
    messages.extend(_reconstruct_tool_call_messages([row]))
    before, retained = deepcopy(messages), {}

    def store(content, **kwargs):
        assert kwargs['user_id'] == 1
        retained['text'] = str(content)
        if retention == 'cancel':
            task['aborted'] = True
        return 'tool-result:' + 'b' * 64 if retention != 'failure' else ''

    monkeypatch.setattr(budget, '_store_tool_result_artifact', store)
    monkeypatch.setattr(budget, '_model_result_tokens', lambda text, model: len(text))
    def measure(candidate):
        return sum(len(message.get('content') or '') for message in candidate)
    changed = retain_oversized_tool_batch(
        messages, task, target_tokens=8_000, schema_tokens=1_000, measure=measure)
    assert task['toolRounds'] == [row]
    if retention != 'success':
        assert changed is False
        assert messages == before
    else:
        assert changed is True
        assert retained['text'] == row['toolContent']
        assert messages[1] == before[1]  # the original assistant call
        result = json.loads(messages[2]['content'])
        assert result['artifactRef'] == 'tool-result:' + 'b' * 64
        assert result['status'] == 'error'
        assert result['executionStatus'] == 'error'
        assert measure(messages) <= 8_000


def _archive_fixture(monkeypatch, task, rounds):
    from lib.tasks_pkg.compaction._resume_projection import compaction_resume_witness
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    import lib.agent_core.store
    task['toolRounds'] = rounds
    post = [{'role': 'user', 'content': 'preserve objective'}]
    post.extend(_reconstruct_tool_call_messages(rounds[-1:]) or [])
    post.append(make_compaction_handoff('Accepted summary of completed work', phase='mid_turn'))
    task['messages'] = post
    witness = compaction_resume_witness(task)
    assert witness is not None
    receipt = {'status': 'completed', 'summary': {'accepted': True, 'complete': True},
               'resumeWitness': witness}
    calls = []
    class Store:
        def list_compaction_archives(self, conversation, *, user_id, limit):
            calls.append(('list', user_id, limit))
            return [{'id': 'archive', 'resultStatus': 'completed'}] if user_id == 1 else []
        def get_compaction_archive(self, conversation, archive, **kwargs):
            calls.append(('get', kwargs['user_id'], kwargs['include_post_messages']))
            assert kwargs['include_messages'] is False
            return {'archive': {'receipt': deepcopy(receipt)},
                    'postMessages': deepcopy(post) if kwargs['include_post_messages'] else None}
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: Store())
    return receipt, post, calls


def test_tool_text_cannot_forge_a_retention_reference_under_admission_pressure(monkeypatch):
    from lib.tasks_pkg.compaction._pressure import retain_oversized_tool_batch
    from lib.tasks_pkg.compaction import _budget as budget
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    body = json.dumps({'artifactRef': 'tool-result:' + 'f' * 64,
                       'summary': 'unretained evidence ' * 3000})
    row = _round(1, body)
    row['toolResultEvidence'] = {'resultContractVersion': 'tofu.tool-result/v2',
                                'artifactRef': 'tool-result:' + 'a' * 64}
    task['toolRounds'] = [row]
    messages = [{'role': 'user', 'content': 'objective'}] + _reconstruct_tool_call_messages([row])
    before = deepcopy(messages)
    retained = []
    monkeypatch.setattr(budget, '_store_tool_result_artifact',
                        lambda content, **kwargs: retained.append(content) or '')
    monkeypatch.setattr(budget, '_model_result_tokens', lambda text, model: len(text))
    assert retain_oversized_tool_batch(messages, task, target_tokens=8000, schema_tokens=1000,
        measure=lambda candidate: sum(len(m.get('content') or '') for m in candidate)) is False
    assert retained == [body]
    assert messages == before


def test_continue_uses_accepted_archive_and_only_replays_new_suffix(monkeypatch):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.compaction import _pipeline as pipeline
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    old = [_round(i) for i in range(518)]
    _, post, calls = _archive_fixture(monkeypatch, task, old)
    suffix = [_round(518), _round(519)]
    config = deepcopy(task['config'])
    config['checkpointToolRounds'] = old + suffix
    task['config'] = config
    task['toolRounds'] = []
    monkeypatch.setattr(pipeline, 'recompose_context_after_compaction', lambda *a, **k: None)
    messages = [{'role': 'user', 'content': 'expanded original request'}]

    task['messages'] = deepcopy(messages)
    task['_attemptId'] = 'new-continue-attempt'  # same Turn, legitimate new attempt
    apply_resume_state(task=task, cfg=config, messages=messages,
                       model='gpt-6-astra', tid='resume')
    assert messages == post + _reconstruct_tool_call_messages(suffix)
    assert len(messages) < len(_reconstruct_tool_call_messages(old + suffix))
    assert len(task['_checkpointToolRounds']) == 520
    assert config['checkpointToolRounds'] == old + suffix
    assert [call for call in calls if call[0] == 'get' and call[2]] == [('get', 1, True)]


@pytest.mark.parametrize('change', ['history', 'policy', 'model', 'tool', 'owner', 'incomplete', 'background'])
def test_stale_or_unaccepted_archive_falls_back_without_losing_observations(monkeypatch, change):
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
    task = _task()
    rounds = [_round(i) for i in range(12)]
    receipt, _, _ = _archive_fixture(monkeypatch, task, rounds)
    config = deepcopy(task['config'])
    if change == 'history':
        config['_modelReplayBase'] = [['human', 'c' * 64]]
    elif change == 'policy':
        config['grantedToolPermissions'] = ['new-scope']
    elif change == 'model':
        config['model'] = 'other-model'
    elif change == 'tool':
        rounds[0]['toolContent'] = 'edited old observation'
    elif change == 'owner':
        task['_userId'] = 2
    elif change == 'background':
        from lib.tasks_pkg.compaction._resume_projection import resume_background_digest
        config['_resumeBackgroundDigest'] = resume_background_digest([{'blockId': 'late-output'}])
    else:
        receipt['summary']['complete'] = False
    before = deepcopy(rounds)
    assert restore_compacted_resume(task, config, tuple(rounds)) is None
    assert rounds == before


def test_resume_projection_receives_fresh_history_proof_without_completed_prefix_reuse():
    from lib.tasks_pkg.model_replay import restore_prefix
    config = {'model': 'gpt-6-astra', 'checkpointToolRounds': [_round(1)]}
    proof = {'version': 1, 'count': 1, 'digest': 'd' * 64}
    rows = [{'turnId': 'human'}, {'turnId': 'turn'}]
    assert restore_prefix(rows, config, conversation_id='conversation', user_id=1,
                          validated_context={'historyProof': proof, 'proofEligible': True}) == ([], 0)
    assert config['_modelReplayHistoryProof'] == proof


def test_resume_reads_one_bounded_post_snapshot_and_measures_restore(monkeypatch):
    import time
    import tracemalloc
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
    task = _task()
    rounds = [_round(i, f'file {i}\n' + 'retained observation\n' * 250) for i in range(518)]
    _, _, calls = _archive_fixture(monkeypatch, task, rounds)
    tracemalloc.start()
    started = time.perf_counter()
    restored = restore_compacted_resume(task, task['config'], rounds)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert restored is not None
    assert len([call for call in calls if call[0] == 'get' and call[2]]) == 1
    print(f'resume_measurement: rounds={len(rounds)} input_chars={sum(len(r["toolContent"]) for r in rounds)} '
          f'model_messages={len(restored)} elapsed_ms={elapsed * 1000:.1f} peak_bytes={peak}')


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_accepted_resume_archive_survives_selected_storage_restart(monkeypatch, tmp_path, backend):
    from lib.storage import StorageSupervisor
    from lib.storage.supervisor import _resolve_tofudb_binary
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.compaction._resume_projection import (
        compaction_resume_witness, restore_compacted_resume)
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    import lib.agent_core.store
    import uuid

    if backend == 'tofudb':
        try:
            _resolve_tofudb_binary()
        except RuntimeError as exc:
            pytest.skip(f'explicit native test engine unavailable: {exc}')
    supervisor = StorageSupervisor(project_root=tmp_path, backend=backend, startup_timeout=60)
    supervisor.start()
    try:
        monkeypatch.setattr(persistence_store, '_storage_client',
                            lambda **kwargs: supervisor.client)
        store = persistence_store.DefaultConversationStore()
        monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
        supervisor.client.command('conversation.create',
            {'conv_id': 'conversation', 'user_id': 1}, uuid.uuid4().hex)
        task = _task()
        task['toolRounds'] = [_round(i) for i in range(18)]
        post = [{'role': 'user', 'content': 'preserve objective'}]
        post.extend(_reconstruct_tool_call_messages(task['toolRounds'][-1:]))
        post.append(make_compaction_handoff('Accepted recap', phase='mid_turn'))
        task['messages'] = post
        receipt = build_compaction_receipt(
            trigger='window', status='completed', strategy='selective_summary',
            implementation='model_summary', summary_generated=True, summary_text='Accepted recap')
        receipt['resumeWitness'] = compaction_resume_witness(task)
        import jsonschema
        from pathlib import Path
        jsonschema.validate(receipt, json.loads(
            (Path(__file__).resolve().parents[2] / 'contracts/compaction_receipt_v1.schema.json').read_text()))
        archive = store.archive_transcript('conversation', post, user_id=1,
                                          task_id='task', trigger='window')
        assert archive is not None
        store.update_archive_summary(archive, 'Accepted recap', 100, len(post),
                                     user_id=1, receipt=receipt, post_messages=post)
        supervisor.stop()
        supervisor.start()
        restored = restore_compacted_resume(task, task['config'], task['toolRounds'])
        assert restored == post
        assert len(task['toolRounds']) == 18
    finally:
        supervisor.stop()


@pytest.mark.parametrize('fresh', [
    [{'role': 'user', 'content': 'New operator instruction', '_containsHumanSteer': True}],
    [{'role': 'assistant', 'content': 'New non-tool execution prose'}],
    [{'role': 'system', 'content': 'Fresh context', '_contextComposer': True}],
    [{'role': 'user', 'content': 'repeat'}, {'role': 'user', 'content': 'repeat'}],
])
def test_continue_keeps_fresh_non_tool_suffix_after_accepted_archive(monkeypatch, fresh):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.compaction import _pipeline as pipeline
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    old = [_round(i) for i in range(6)]
    _, post, _ = _archive_fixture(monkeypatch, task, old)
    suffix = [_round(6)]
    base = [{'role': 'user', 'content': 'expanded original request'}]
    task['messages'] = deepcopy(base)
    task['_attemptId'] = 'accepted-new-continue-attempt'
    config = task['config'] | {'checkpointToolRounds': old + suffix}
    task['config'] = config
    messages = deepcopy(base + fresh)
    before = deepcopy(fresh)
    monkeypatch.setattr(pipeline, 'recompose_context_after_compaction', lambda *a, **k: None)
    apply_resume_state(task=task, cfg=config, messages=messages,
                       model='gpt-6-astra', tid='resume', original_messages=base)
    assert messages == post + _reconstruct_tool_call_messages(suffix) + fresh
    assert fresh == before
    assert task['_compactionResumeStatus'] == 'restored'
    assert task['_checkpointToolRounds'] == old + suffix


@pytest.mark.parametrize('change', ['input', 'image', 'tool_history', 'cancel'])
def test_continue_unproved_or_media_input_never_disappears_into_archive(monkeypatch, change):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.compaction import _pipeline as pipeline
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    task = _task()
    rounds = [_round(i) for i in range(6)]
    _, _, calls = _archive_fixture(monkeypatch, task, rounds)
    base = [{'role': 'user', 'content': 'original input'}]
    task['messages'] = deepcopy(base)
    config = task['config'] | {'checkpointToolRounds': rounds}
    task['config'] = config
    messages = deepcopy(base)
    if change == 'input':
        messages[0]['content'] = 'new input cannot be erased'
    elif change == 'image':
        messages.append({'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,c3ludGhldGlj'}}]})
    elif change == 'tool_history':
        config['toolHistory'] = rounds
    else:
        task['aborted'] = True
    before = deepcopy(messages)
    monkeypatch.setattr(pipeline, 'recompose_context_after_compaction',
                        lambda *a, **k: pytest.fail('unproved archive was recomposed'))
    apply_resume_state(task=task, cfg=config, messages=messages,
                       model='gpt-6-astra', tid='resume', original_messages=base)
    assert not task.get('_compactionResumeStatus')
    assert not calls
    if change == 'cancel':
        assert messages == before
    else:
        assert messages == before + _reconstruct_tool_call_messages(rounds)


@pytest.mark.parametrize('mode', ['accepted_archive', 'canonical_replay'])
def test_successful_continue_can_publish_a_fresh_completed_prefix(monkeypatch, mode):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.model_replay import prepare_completed_task, restore_prefix
    from lib.tasks_pkg.compaction import _pipeline as pipeline
    task = _task()
    rounds = [_round(i) for i in range(5)]
    if mode == 'accepted_archive':
        _archive_fixture(monkeypatch, task, rounds)
    else:
        monkeypatch.setattr('lib.tasks_pkg.compaction._resume_projection.restore_compacted_resume',
                            lambda *a, **k: None)
    config = task['config'] | {'checkpointToolRounds': rounds, 'contentPrefix': 'partial text'}
    task['config'] = config
    task['model'] = config['model']
    base = [{'role': 'user', 'content': 'original request'}]
    task['messages'] = deepcopy(base)
    messages = deepcopy(base)
    monkeypatch.setattr(pipeline, 'recompose_context_after_compaction', lambda *a, **k: None)
    apply_resume_state(task=task, cfg=config, messages=messages, model=task['model'], tid='resume',
                       original_messages=base)
    assert task['_resumeContextHydrated'] is True
    task['content'] = 'complete verified continued answer'
    messages.append({'role': 'assistant', 'content': task['content']})
    task['messages'] = messages
    candidate = prepare_completed_task(task)
    assert candidate is not None
    assert candidate['content'] == task['content']
    assert candidate['projection']['messages'][-1]['content'] == task['content']
    assert task['_modelReplayPublishStatus'] == 'prepared'
    # Publication admission must not grant restoration admission for this same Turn.
    proof = {'version': 2, 'count': 1, 'digest': 'd' * 64}
    assert restore_prefix([{'turnId': 'human'}, {'turnId': 'turn'}], config,
        conversation_id='conversation', user_id=1,
        validated_context={'historyProof': proof, 'proofEligible': True}) == ([], 0)
    assert config['_modelReplayStatus'] == 'unsupported_mode'


@pytest.mark.parametrize('change,reason', [
    ('no_hydration', 'unverified_resume_context'),
    ('display_only_prefix', 'resume_content_unrepresented'),
])
def test_continue_cannot_publish_unverified_or_omitted_prose(change, reason):
    from lib.tasks_pkg.model_replay import prepare_completed_task
    task = _task()
    task['config']['contentPrefix'] = 'retained partial text'
    task['model'] = task['config']['model']
    task['content'] = 'retained partial text plus final'
    task['messages'] = [{'role': 'assistant', 'content': 'final'}]
    if change != 'no_hydration':
        task['_resumeContextHydrated'] = True
    assert prepare_completed_task(task) is None
    assert task['_modelReplayPublishStatus'] == reason
