"""One owner memo serves every count of a task's request, exactly.

Each model round counts the same append-mostly request several times: the
compaction gate, prompt admission, selected-slot admission, stream ingress and,
for Anthropic routes, the translated recount. Those counts read one task-owned
``RequestTextCounts`` through per-projection views. Every count must equal a
stateless count of the same request (property and differential specs below),
and across rounds each text reaches the tokenizer and the entropy estimator
once, so a round's counting work follows what changed.
"""

from __future__ import annotations

import copy
import json
import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import lib.token_counter.tiktoken_counter as counter
from lib.conversation_sync.settled_rows import is_settled, settle
from lib.token_counter.base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
    count_images,
    iter_message_texts,
)
from lib.token_counter.heuristic import cheap_estimate, cheap_estimate_text

pytestmark = pytest.mark.unit

tiktoken = pytest.importorskip('tiktoken')

_FRAGMENTS = (
    'def run(self, path):\n    return open(path).read()\n',
    '上下文压缩与请求工具', 'ありがとう ', '🙂 café ', 'naïve résumé ',
    '<|endoftext|>', '<|fim_prefix|>', 'lone \ud800 surrogate ',
    'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODkrLw==', 'x' * 64,
    '{"path": "lib/x.py", "start_line": 1}', '\t\n  ', 'The request cache. ',
)


@pytest.fixture(autouse=True)
def _isolated_shared_cache():
    counter._reset_text_count_cache_for_tests()
    yield
    counter._reset_text_count_cache_for_tests()


def _encoder(model):
    return tiktoken.get_encoding(counter.encoding_for_model(model))


def _reference_tokens(messages, model, *, system=None, tools=None):
    encoder = _encoder(model)
    texts = iter_message_texts(messages, system, tools)
    return (sum(len(encoder.encode(text, disallowed_special=())) for text in texts)
            + 4 * len(messages) + 800 * count_images(messages) + 400)


def _text(rng, low=0, high=6):
    return ''.join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(low, high)))


def _message(rng, index):
    role = rng.choice(('user', 'assistant', 'tool', 'system'))
    message = {'role': role}
    shape = rng.randrange(4)
    if shape == 0:
        message['content'] = _text(rng)
    elif shape == 1:
        blocks = []
        for _ in range(rng.randint(0, 4)):
            kind = rng.choice(('text', 'input_text', 'output_text', 'image_url',
                               'tool_result', 'thinking', 'input_audio'))
            if kind == 'image_url':
                blocks.append({'type': kind, 'image_url': {
                    'url': 'data:image/png;base64,' + 'A' * rng.randint(1, 40),
                    'detail': rng.choice(('low', 'high', 'auto'))}})
            elif kind == 'tool_result':
                blocks.append({'type': kind, 'content': rng.choice((
                    _text(rng), [{'type': 'text', 'text': _text(rng)}]))})
            elif kind == 'input_audio':
                blocks.append({'type': kind, 'input_audio': {
                    'data': 'AAAA' * rng.randint(1, 5), 'format': 'wav'}})
            else:
                blocks.append({'type': kind, 'text': _text(rng)})
        message['content'] = blocks
    elif shape == 2:
        message['content'] = None
    if role == 'assistant' and rng.random() < 0.6:
        message['reasoning_content'] = rng.choice((
            _text(rng, 1, 8),
            [{'type': 'text', 'text': _text(rng)}, {'type': 'thinking', 'text': _text(rng)}]))
        message['tool_calls'] = [{
            'id': f'call_{index}_{call}', 'type': 'function', 'function': {
                'name': rng.choice(('read_files', 'run_command', '读取')),
                'arguments': json.dumps({'path': _text(rng, 0, 2)}, ensure_ascii=rng.random() < 0.5)}}
            for call in range(rng.randint(1, 2))]
    return message


def _tools(rng, size):
    return [{'type': 'function', 'function': {
        'name': f'tool_{index}', 'description': _text(rng, 1, 4),
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': _text(rng, 0, 2)}}}}}
        for index in range(size)]


def _evolve(rng, messages, index):
    """Append a turn and sometimes rewrite, strip or drop earlier history."""
    messages.append(_message(rng, index))
    messages.append(_message(rng, index + 1))
    if messages and rng.random() < 0.3:
        target = rng.randrange(len(messages))
        messages[target] = {**messages[target], 'content': _text(rng, 1, 3)}
    if messages and rng.random() < 0.2:
        target = rng.randrange(len(messages))
        messages[target]['reasoning_content'] = ''
    if len(messages) > 4 and rng.random() < 0.15:
        del messages[1:3]


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=2**32 - 1),
       model=st.sampled_from(('gpt-4', 'gpt-4o', 'claude-opus-4')))
def test_every_view_count_equals_the_stateless_count_across_rounds(seed, model):
    rng = random.Random(seed)
    memo = counter.RequestTextCounts()
    task_tools = _tools(rng, rng.randint(0, 3))
    round_tools = task_tools[:rng.randint(0, len(task_tools))]
    system = rng.choice((None, _text(rng, 1, 3), [{'type': 'text', 'text': _text(rng)}]))
    messages = [_message(rng, index) for index in range(rng.randint(0, 6))]
    token_counter = counter.TiktokenCounter()
    for round_index in range(4):
        _evolve(rng, messages, 10 * round_index)
        projections = (
            ('request', messages, task_tools, None, True),
            ('admission', messages, round_tools, None, True),
            ('provider', copy.deepcopy(messages), round_tools, system, False),
            ('provider:anthropic', copy.deepcopy(messages), round_tools, system, False),
        )
        for label, request, tools, request_system, reuse in projections:
            view = memo.view(label, reuse_tool_schema_identity=reuse)
            expected = _reference_tokens(request, model, system=request_system, tools=tools)

            remembered = token_counter.count(request, model=model, system=request_system,
                                             tools=tools, text_count_memo=view)
            stateless = token_counter.count(request, model=model, system=request_system,
                                            tools=tools)

            assert remembered == stateless == expected
            assert view.cheap_estimate(request, request_system, tools) == cheap_estimate(
                request, system=request_system, tools=tools)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_compaction_estimates_inside_a_view_equal_the_shared_estimator(seed):
    from lib.tasks_pkg.compaction._tokens import (
        _estimate_msg_tokens, _estimate_total_tokens)

    rng = random.Random(seed)
    memo = counter.RequestTextCounts()
    messages = [_message(rng, index) for index in range(rng.randint(1, 8))]
    for round_index in range(3):
        _evolve(rng, messages, 10 * round_index)
        expected_total = _estimate_total_tokens(messages)
        expected_rows = [_estimate_msg_tokens(message) for message in messages]
        view = memo.view('request')

        with view.estimating():
            total = _estimate_total_tokens(messages)
            rows = [_estimate_msg_tokens(message) for message in messages]

        assert total == expected_total
        assert rows == expected_rows


def test_settled_rows_count_from_their_memo_slots_without_digests(monkeypatch):
    rng = random.Random(5)
    plain = [_message(rng, index) for index in range(12)]
    settled = settle(copy.deepcopy(plain))
    assert all(is_settled(row) for row in settled)
    tools = _tools(rng, 2)
    expected = _reference_tokens(plain, 'gpt-4o', tools=tools)
    memo = counter.RequestTextCounts()
    token_counter = counter.TiktokenCounter()

    first = token_counter.count(settled, model='gpt-4o', tools=tools,
                                text_count_memo=memo.view('request'))
    first_estimate = memo.view('request').cheap_estimate(settled, None, tools)
    digests = []
    original = counter._text_digest
    monkeypatch.setattr(counter, '_text_digest',
                        lambda text: digests.append(text) or original(text))
    second = token_counter.count(settled, model='gpt-4o', tools=tools,
                                 text_count_memo=memo.view('request'))
    second_estimate = memo.view('request').cheap_estimate(settled, None, tools)

    assert first == second == expected
    assert first_estimate == second_estimate == cheap_estimate(plain, tools=tools)
    # Only the plain tool-schema text is digested again; settled message rows
    # answer from their own immutable memo slots.
    assert digests == [json.dumps(tools, ensure_ascii=False)] * 2


def test_tool_schema_text_is_reused_by_identity_only_in_loop_views(monkeypatch):
    tools = _tools(random.Random(3), 3)
    dumps = []
    original = json.dumps

    def recording_dumps(value, *args, **kwargs):
        if isinstance(value, list) and value and value[0] is tools[0]:
            dumps.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'dumps', recording_dumps)
    memo = counter.RequestTextCounts()
    messages = [{'role': 'user', 'content': 'hello'}]
    token_counter = counter.TiktokenCounter()

    for _round in range(3):
        token_counter.count(messages, model='gpt-4o', tools=list(tools),
                            text_count_memo=memo.view('request', reuse_tool_schema_identity=True))
    assert len(dumps) == 1

    # A replaced schema object is a different tool set.
    replaced = list(tools)
    replaced[-1] = {**tools[-1], 'function': {**tools[-1]['function'], 'cache_control': {'type': 'ephemeral'}}}
    loop_count = token_counter.count(messages, model='gpt-4o', tools=replaced,
                                     text_count_memo=memo.view('request', reuse_tool_schema_identity=True))
    assert len(dumps) == 2
    assert loop_count == _reference_tokens(messages, 'gpt-4o', tools=replaced)

    # Provider bodies recount schemas from content: an in-place edit counts.
    provider_tools = copy.deepcopy(tools)
    before = token_counter.count(messages, model='gpt-4o', tools=provider_tools,
                                 text_count_memo=memo.view('provider'))
    provider_tools[0]['function']['description'] += ' 新的说明 with more words'
    after = token_counter.count(messages, model='gpt-4o', tools=provider_tools,
                                text_count_memo=memo.view('provider'))
    assert before == _reference_tokens(messages, 'gpt-4o', tools=tools)
    assert after == _reference_tokens(messages, 'gpt-4o', tools=provider_tools) > before


def test_views_keep_only_their_latest_request_and_no_text():
    memo = counter.RequestTextCounts()
    token_counter = counter.TiktokenCounter()
    dropped = 'private compacted-away evidence ' * 10
    kept = 'private kept evidence'

    token_counter.count([{'role': 'user', 'content': dropped},
                         {'role': 'user', 'content': kept}],
                        model='gpt-4o', text_count_memo=memo.view('provider'))
    token_counter.count([{'role': 'user', 'content': kept}],
                        model='gpt-4o', text_count_memo=memo.view('request'))
    token_counter.count([{'role': 'user', 'content': kept}],
                        model='gpt-4o', text_count_memo=memo.view('provider'))

    assert list(memo.counts.values()) == [len(_encoder('gpt-4o').encode(kept))]
    assert 'private' not in repr(memo._views)
    for snapshot in memo._views.values():
        assert not any(isinstance(key, str) for key in snapshot.entries)
        assert len(snapshot.entries) == 1


def test_owner_memo_and_views_are_shared_never_copied():
    memo = counter.RequestTextCounts()
    view = memo.view('provider')
    body = {'messages': [], '_text_count_view': view}

    assert copy.deepcopy(memo) is memo
    assert copy.copy(view) is view
    assert copy.deepcopy(body)['_text_count_view'] is view


class _RecordingTokenizer:
    """Records every text the counter hands to the tokenizer or estimator."""

    def __init__(self, monkeypatch):
        self.tokenized: list[str] = []
        self.estimated: list[str] = []
        self.recording = True
        encode_batch = counter._encode_batch_counts
        estimate = counter._cheap_estimate_text

        def record_batch(encoder, texts):
            if self.recording:
                self.tokenized.extend(texts)
            return encode_batch(encoder, texts)

        def record_estimate(text):
            if self.recording:
                self.estimated.append(text)
            return estimate(text)

        monkeypatch.setattr(counter, '_encode_batch_counts', record_batch)
        monkeypatch.setattr(counter, '_cheap_estimate_text', record_estimate)
        # Prove the owner memo alone gives reuse: the shared digest cache
        # admits nothing here.
        monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 10**9)
        monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 10**9)


def _round_history(rounds):
    rng = random.Random(11)
    history = [{'role': 'system', 'content': 'You are an agent. ' * 30},
               {'role': 'user', 'content': '整理上下文 and fix the bug 🙂'}]
    for index in range(rounds):
        call = f'call_{index}'
        history.append({
            'role': 'assistant', 'content': _text(rng, 0, 2),
            'reasoning_content': f'thinking about step {index} ' + _text(rng, 1, 4),
            'tool_calls': [{'id': call, 'type': 'function', 'function': {
                'name': 'read_files',
                'arguments': json.dumps({'path': f'lib/m{index}.py'})}}]})
        history.append({'role': 'tool', 'tool_call_id': call,
                        'content': f'result {index}: ' + _text(rng, 2, 9)})
    return history


@pytest.mark.parametrize('model', ['gpt-4o', 'claude-opus-4'])
def test_each_text_is_tokenized_and_estimated_once_across_rounds(monkeypatch, model):
    """Gate, admission, slot admission, ingress and recount share one memo."""
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.llm.request import detach_input, recount_anthropic_input
    from lib.tasks_pkg.compaction import _tokens
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY

    monkeypatch.setattr(_tokens, 'resolve_model_context_limit',
                        lambda *_args, **_kwargs: 1_000_000)
    recorder = _RecordingTokenizer(monkeypatch)
    tools = _tools(random.Random(2), 4)
    task = {'id': 'reuse', 'config': {'model': model}, '_tool_schema': tools}
    history = _round_history(6)
    messages = [dict(message) for message in history[:4]]
    per_round = []
    for round_index in range(6):
        messages.extend(dict(message) for message in history[len(messages):4 + 2 * round_index])
        recorder.tokenized.clear()
        recorder.recording = True
        stateless_task = {**task, '_requestTextCountMemo': None}

        gate = _tokens._count_tokens_authoritative(messages, task)
        admission = _tokens._count_tokens_authoritative(
            messages, task, tool_schema=tools, measurement_out={},
            collect_reusable_text_counts=True)
        memo = task['_requestTextCountMemo']
        body = {'model': model, 'messages': [dict(message) for message in messages],
                'tools': tools, 'max_tokens': 4096,
                TEXT_COUNT_VIEW_KEY: memo.view('provider')}
        adapted = enforce_body_input_limit(dict(body), model=model)
        detach_input(body)
        ingress = enforce_body_input_limit(body, model=model)
        translated = {'messages': [
            {'role': message['role'], 'content': message.get('content') or ''}
            for message in body['messages']], 'tools': tools}
        recount = recount_anthropic_input(
            translated, model=model, text_count_view=body[TEXT_COUNT_VIEW_KEY])
        per_round.append(list(recorder.tokenized))
        recorder.recording = False

        assert gate == _tokens._count_tokens_authoritative(messages, stateless_task)
        assert admission == _tokens._count_tokens_authoritative(
            messages, stateless_task, tool_schema=tools)
        assert adapted == ingress == _reference_tokens(messages, model, tools=tools)
        assert recount == _reference_tokens(translated['messages'], model, tools=tools)

    assert all(per_round), 'every round tokenizes at least its new turn'
    all_texts = list(iter_message_texts(messages, None, tools))
    tokenized = [text for texts in per_round for text in texts]
    assert sorted(tokenized) == sorted(set(tokenized))
    assert set(all_texts) <= set(tokenized)
    # Rounds after the first tokenize only that round's new turn.
    for round_index, texts in enumerate(per_round[1:], start=1):
        new_turn = history[4 + 2 * (round_index - 1):4 + 2 * round_index]
        assert set(texts) <= set(iter_message_texts(new_turn))
    assert sorted(recorder.estimated) == sorted(set(recorder.estimated))


def test_one_round_digests_each_text_once_and_keeps_no_text_afterwards(monkeypatch):
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.llm.request import detach_input
    from lib.tasks_pkg.compaction import _tokens
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY

    monkeypatch.setattr(_tokens, 'resolve_model_context_limit',
                        lambda *_args, **_kwargs: 1_000_000)
    tools = _tools(random.Random(4), 2)
    task = {'id': 'round-ledger', 'config': {'model': 'gpt-4o'}, '_tool_schema': tools}
    messages = [dict(message) for message in _round_history(4)]
    digested = []
    original = counter._text_digest
    monkeypatch.setattr(counter, '_text_digest',
                        lambda text: digested.append(text) or original(text))

    def one_round():
        with _tokens.task_round_text_identities(task):
            _tokens._count_tokens_authoritative(messages, task)
            _tokens._count_tokens_authoritative(messages, task, tool_schema=tools)
            body = {'model': 'gpt-4o', 'messages': [dict(m) for m in messages],
                    'tools': tools,
                    TEXT_COUNT_VIEW_KEY: task['_requestTextCountMemo'].view('provider')}
        enforce_body_input_limit(dict(body), model='gpt-4o')
        detach_input(body)
        enforce_body_input_limit(body, model='gpt-4o')

    one_round()
    first_round = list(digested)
    message_texts = set(iter_message_texts(messages))
    assert message_texts <= set(first_round)
    digested_objects = [id(text) for text in first_round if text in message_texts]
    assert len(digested_objects) == len(set(digested_objects)), (
        'each message text object is digested once per round')
    memo = task['_requestTextCountMemo']
    assert memo._round_ledger is None
    assert 'You are an agent' not in repr(memo._views)

    digested.clear()
    messages.append({'role': 'user', 'content': 'one more question'})
    one_round()
    assert digested.count('one more question') == 1


@pytest.mark.parametrize('order', ['nested', 'interleaved'])
def test_round_scopes_sharing_a_memo_never_leave_its_text_behind(order):
    """Shallow task copies share one memo; overlapping rounds must not leak."""
    memo = counter.RequestTextCounts()
    first, second = memo.shared_text_identities(), memo.shared_text_identities()

    first.__enter__()
    memo.view('request').text_digest('first round text')
    second.__enter__()
    memo.view('request').text_digest('second round text')
    if order == 'nested':
        second.__exit__(None, None, None)
        first.__exit__(None, None, None)
    else:
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)

    assert memo._round_ledger is None
    with memo.shared_text_identities():
        assert memo._round_ledger == {}
    assert memo._round_ledger is None


def test_reusable_tool_counts_receipt_survives_the_memo_path():
    memo = counter.RequestTextCounts()
    content = 'tool output ' * 50
    messages = [{'role': 'tool', 'tool_call_id': 'a', 'content': content},
                {'role': 'user', 'content': 'next'}]
    measurement = {}

    counter.TiktokenCounter().count(messages, model='gpt-4o',
                                    measurement_out=measurement,
                                    text_count_memo=memo.view('admission'))

    assert measurement[REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY] == {
        id(content): len(_encoder('gpt-4o').encode(content))}


def test_build_round_request_attaches_a_provider_view_that_never_reaches_the_wire():
    import threading
    from types import SimpleNamespace

    import lib.tasks_pkg.orchestrator._messages_snapshot as snapshot_module
    from lib.llm._sse_core import prepare_request
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY

    task = {'id': 'view-body', 'convId': '', '_userId': 1,
            'config': {'model': 'gpt-4o'}, 'abort_event': threading.Event(),
            'aborted': False}
    state = SimpleNamespace(model='gpt-4o', preset='medium', thinking_enabled=False)
    messages = [{'role': 'user', 'content': 'hello there'}]
    original_append = snapshot_module.append_event
    snapshot_module.append_event = lambda _task, _event: None
    try:
        _tools, body = build_round_request(
            task, state, messages, None, round_num=0, tid='view-bod',
            thinking_depth=None, temperature=1.0, max_tokens=1024,
            response_format=None)
    finally:
        snapshot_module.append_event = original_append

    view = body[TEXT_COUNT_VIEW_KEY]
    assert isinstance(view, counter.RequestTextCountView)
    assert view.memo is task['_requestTextCountMemo']
    assert view.label == 'provider'
    slot = SimpleNamespace(
        model='gpt-4o', logical_model='gpt-4o', provider_id='bench',
        routing_provider_id='bench', route_offering_id='', route_deployment_id='',
        protocol='openai', max_output_tokens=0, thinking_format='',
        inference_profile=None, responses_profile='', context_window=128_000)
    adapted = _adapt_stream_body_for_slot(
        slot, body, True, tools=None, max_tokens=None, temperature=None,
        thinking_enabled=None, preset=None, effort=None)
    assert adapted[TEXT_COUNT_VIEW_KEY] is view
    plan = prepare_request(adapted, base_url='https://bench.invalid/v1',
                           api_protocol='openai')
    assert TEXT_COUNT_VIEW_KEY.encode() not in plan.body.payload
    assert view.label.encode() not in plan.body.payload
