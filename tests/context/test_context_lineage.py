"""Source occurrences survive real sanitization and provider projection off-wire."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from lib.llm.body import build_body
from lib.llm.request import seal_request, semantic_request_id
from lib.tasks_pkg.context_composer import append_injection

pytestmark = pytest.mark.unit


def prepare(messages, provider='test'):
    return build_body('claude-sonnet-4', messages, max_tokens=1024,
                      provider_id=provider, precomputed_input_tokens=100,
                      context_window=200000)


def text_at(body, location):
    content = (body[location['region']][location['message']]['content']
               if location['region'] in ('messages', 'input') else body[location['region']])
    text = content if isinstance(content, str) else content[location['part']]['text']
    return text[location['start']:location['end']]


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_identical_merged_events_keep_distinct_positions_through_provider_projection(protocol):
    messages = []
    append_injection(messages, 'worker_policy', 'policy')
    messages.append({'role': 'user', 'content': 'request'})
    for _ in range(2):
        append_injection(messages, 'tool_progress', 'private event')
    original = deepcopy(messages)
    body = prepare(messages)
    semantic_id = semantic_request_id(body)
    if protocol == 'anthropic':
        from lib.llm.anthropic_outbound import openai_body_to_anthropic
        wire = openai_body_to_anthropic(body)
    elif protocol == 'responses':
        from lib.llm.responses_outbound import openai_body_to_responses
        wire, _ = openai_body_to_responses(body)
    else:
        from lib.llm.openai_outbound import openai_body_to_chat
        wire = openai_body_to_chat(body)
    lineage = wire['_context_lineage']
    sources = lineage['sources']
    events = [row for row in sources if row['id'] == 'tool_progress']
    assert len(events) == 2 and len({row['eventId'] for row in events}) == 2
    assert events[0]['output'] != events[1]['output']
    for row in sources:
        assert row['status'] == 'located'
        assert hashlib.sha256(text_at(wire, row['output']).encode()).hexdigest() == row['spanHash']
    if protocol == 'anthropic':
        assert sources[0]['output']['region'] == 'system'
    sealed = seal_request(wire, model='claude-sonnet-4', protocol=protocol,
                          input_tokens=100, semantic_id=semantic_id)
    evidence = sealed.context_diagnostics()
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/context_lineage_v1.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(evidence)
    assert evidence['requestIdentity'] == sealed.diagnostics()
    assert evidence['wireBodyHash'] == hashlib.sha256(sealed.payload).hexdigest()
    assert '_context_lineage' not in sealed.payload.decode()
    assert '_contextLineageSpans' not in sealed.payload.decode()
    assert 'private event' not in json.dumps(evidence)
    assert messages == original


def test_gateway_rewrite_preserves_exact_event_range_and_names_its_stage():
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'FLG 中文 event')
    body = prepare(messages, 'sankuai_test')
    row, = body['_context_lineage']['sources']
    assert row['status'] == 'located'
    assert text_at(body, row['output']) == 'F\u200bLG 中文 event'
    assert row['changedAt'] == 'gateway_sanitize'
    assert row['input']['start'] == 0
    assert row['output']['start'] == len('request\n\n')


def test_blank_block_normalization_preserves_owned_section_positions():
    from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [context_block('user_context', 'preference')], ComposeRequest())
    messages[-1]['content'].insert(0, {'type': 'text', 'text': '\n '})
    body = prepare(messages)
    row, = body['_context_lineage']['sources']
    assert row['status'] == 'located'
    assert row['input']['part'] == 1
    assert text_at(body, row['output']).startswith('<!-- tofu-context:user_context:start -->')


def test_unmapped_projection_reports_loss_without_guessing_a_duplicate(monkeypatch):
    import lib.llm.body._build as builder
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event')
    actual = builder._merge_consecutive_same_role
    def drop_origin(rows):
        result = actual(rows)
        for row in result:
            row.pop('_contextLineageSpans', None)
        return result
    monkeypatch.setattr(builder, '_merge_consecutive_same_role', drop_origin)
    body = prepare(messages)
    row, = body['_context_lineage']['sources']
    assert row['status'] == 'unmapped'
    assert row['lostAt'] == 'same_role_merge'
    assert 'output' not in row


def test_inspector_keeps_exact_event_occurrences_after_sanitization():
    from lib.tasks_pkg.round_evidence import build_round_evidence
    from lib.tasks_pkg.context_composer.inspection import inspect_context
    task = {'id': 'task'}
    messages = [{'role': 'user', 'content': 'request'}]
    for _ in range(2):
        append_injection(messages, 'tool_progress', 'event', task=task)
    event = build_round_evidence(task, messages, ref={'roundNum': 1, 'turn': 'turn'},
                                 kind='request', model='model')
    report = inspect_context(event)
    origins = report['messages'][0]['origins']
    assert len(origins) == 2 and len({row['eventId'] for row in origins}) == 2
    assert {row['evidence'] for row in origins} == {'transformation_lineage'}
    assert not report['unlocatedEventReceipts']
    assert not report['lineage']['losses']


def test_wire_bundle_binds_cache_record_to_exact_request_and_transport_occurrence():
    from lib.llm.openai_outbound import openai_body_to_chat
    from lib.tasks_pkg.context_composer.inspection import inspect_context
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event')
    body = prepare(messages)
    sealed = seal_request(openai_body_to_chat(body), model='m', input_tokens=100,
                          semantic_id=semantic_request_id(body))
    evidence = {**sealed.context_diagnostics(), 'traceId': 'transport-a'}
    record = {'request_identity': sealed.diagnostics(), 'trace_id': 'transport-a',
              'bucket': 'warm', 'cache_read': 1200, 'cache_write': 0}
    # The archive sorts keys; positions bind content without claiming original encoding.
    request = json.loads(json.dumps(sealed.wire_body(), sort_keys=True))
    bundle = {'request': request, 'evidence': {'contextLineage': evidence}, 'cacheRecord': record}
    report = inspect_context(bundle, wire=True)
    assert report['lineage']['status'] == 'verified_content'
    assert report['cacheObservation']['status'] == 'matched'
    assert report['cacheObservation']['cache_read'] == 1200
    record['trace_id'] = 'another-identical-request'
    assert inspect_context(bundle, wire=True)['cacheObservation']['status'] == 'request_mismatch'
    request['messages'][0]['content'] += ' changed outside the event'
    rejected = inspect_context(bundle, wire=True)
    assert rejected['lineage']['status'] == 'request_mismatch'
    assert not rejected['messages'][0]['origins']
    assert rejected['cacheObservation']['status'] == 'unbound'


def test_same_body_retries_require_explicit_transport_identity():
    from lib.llm.openai_outbound import openai_body_to_chat
    from lib.tasks_pkg.context_composer.inspection import inspect_context
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event')
    body = prepare(messages)
    sealed = seal_request(openai_body_to_chat(body), model='m', input_tokens=100,
                          semantic_id=semantic_request_id(body))
    projections = [{'contextLineage': {**sealed.context_diagnostics(), 'traceId': trace}}
                   for trace in ('first', 'retry')]
    bundle = {'request': sealed.wire_body(), 'evidence': {'wireProjections': projections}}
    assert inspect_context(bundle, wire=True)['lineage']['status'] == 'ambiguous'
    bundle['traceId'] = 'retry'
    assert inspect_context(bundle, wire=True)['lineage']['status'] == 'verified_content'


@pytest.mark.parametrize('stage', ['canonical_ingress', 'request_sealing'])
@pytest.mark.parametrize('inside_source', [True, False])
def test_unobserved_request_mutation_cannot_reuse_source_positions(stage, inside_source):
    from lib.llm.openai_outbound import openai_body_to_chat
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event')
    body = prepare(messages)
    if stage == 'canonical_ingress':
        body['messages'][0]['content'] = ('different event' if inside_source
                                         else body['messages'][0]['content'] + ' changed')
    wire = openai_body_to_chat(body)
    if stage == 'request_sealing':
        wire['messages'][0]['content'] = ('different event' if inside_source
                                         else wire['messages'][0]['content'] + ' changed')
    sealed = seal_request(wire, model='m', input_tokens=100, semantic_id=semantic_request_id(body))
    source, = sealed.context_diagnostics()['sources']
    if not inside_source:
        # An append outside the event preserves the actual source span.
        assert source['status'] == 'located'
    else:
        assert source['status'] == 'unmapped' and source['lostAt'] == stage


def test_malformed_exported_lineage_is_rejected_without_guessing():
    from lib.llm.openai_outbound import openai_body_to_chat
    from lib.tasks_pkg.context_composer.inspection import inspect_context
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event')
    body = prepare(messages)
    sealed = seal_request(openai_body_to_chat(body), model='m', input_tokens=100,
                          semantic_id=semantic_request_id(body))
    trace = sealed.context_diagnostics()
    trace['requestIdentity'] = 'invalid'
    bundle = {'request': sealed.wire_body(), 'evidence': {'contextLineage': trace},
              'cacheRecord': {'trace_id': 'test'}}
    report = inspect_context(bundle, wire=True)
    assert report['lineage']['status'] == 'invalid'
    assert report['cacheObservation']['status'] == 'unbound'
    assert not report['messages'][0]['origins']


def test_source_verification_does_not_rescan_unrelated_content_parts():
    from lib.llm_sanitize.lineage import observed_span
    class CountedPart(dict):
        reads = 0
        def get(self, key, default=None):
            type(self).reads += 1
            return super().get(key, default)
    parts = [CountedPart(type='text', text='source') for _ in range(500)]
    document = {'messages': [{'role': 'user', 'content': parts}]}
    for index in range(len(parts)):
        assert observed_span(document, {'region': 'messages', 'message': 0,
                                        'part': index, 'start': 0, 'end': 6}) == 'source'
    # Each positional lookup reads only its selected part, not all siblings.
    assert CountedPart.reads <= 3 * len(parts)


def test_snapshot_content_reduction_marks_source_loss_at_its_owner():
    from lib.tasks_pkg.round_evidence import build_round_evidence
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'event ' * 20000)
    body = prepare(messages)
    snapshot = build_round_evidence(
        {}, messages, ref={'roundNum': 1, 'turn': 'turn'}, kind='request', model='m',
        prepared_messages=body['messages'], prepared_lineage=body['_context_lineage'])
    source, = snapshot['contextLineage']['sources']
    assert source['status'] == 'unmapped' and source['lostAt'] == 'snapshot_projection'
    assert body['_context_lineage']['sources'][0]['status'] == 'located'


def test_text_only_image_projection_preserves_distinct_equal_events(monkeypatch):
    import lib.llm.body._build as builder
    monkeypatch.setattr(builder, 'model_supports_vision', lambda _: False)
    messages = [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/one.png'}},
        {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/two.png'}},
    ]}]
    for _ in range(2):
        append_injection(messages, 'tool_progress', 'same event')
    body = prepare(messages)
    sources = body['_context_lineage']['sources']
    assert len(sources) == 2 and len({row['eventId'] for row in sources}) == 2
    assert [row['status'] for row in sources] == ['located', 'located']
    assert [row['output']['part'] for row in sources] == [1, 2]
    assert [text_at(body, row['output']) for row in sources] == ['same event', 'same event']
