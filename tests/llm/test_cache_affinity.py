"""Prompt-cache lineage key and provider-declared affinity carriers.

FRIDAY (your-llm-gateway.example.com) serves each model from several upstream accounts and
keeps caches per account; it hashes ``Mt-Session-Id`` to pin one session to one
account. Without a stable lineage key, consecutive rounds of one conversation
landed on different accounts: byte-identical prompts, ``cache_read`` 0 or an
OLDER round's entry (conversation mvywcu5qqy5ocy, 2026-11-02).
"""
import io
import json
import logging
from pathlib import Path

import pytest

from lib.llm._sse_core import SSEAccumulator, prepare_request
from lib.llm.cache_affinity import (
    CACHE_AFFINITY_SCOPE_KEY,
    cache_affinity_evidence,
    cache_affinity_key,
    merge_cache_affinity_headers,
)
from lib.llm.route_features import route_feature
from lib.tasks_pkg.wire_fingerprint import routing_fingerprint
from lib.tasks_pkg.wire_fingerprint_diff import diff_routing

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
FRIDAY_ANTHROPIC = 'https://api.openai.com/v1/anthropic'
CARRIER = 'cache_affinity_mt_session_id'
DECLARED = {'features': {CARRIER: 'supported'}}


def _round(scope, *, profile=DECLARED, user='inspect the cache'):
    body = {
        'model': 'kunlunbedrock.claude-opus-5.5-m17',
        'max_tokens': 1024,
        'stream': True,
        'messages': [{'role': 'user', 'content': user}],
        '_conv_id': 'conv-1',
        '_task_id': 'task-1',
    }
    if scope is not None:
        body[CACHE_AFFINITY_SCOPE_KEY] = scope
    if profile is not None:
        body['_route_inference_profile'] = profile
    return body


def _plan(body, base_url=FRIDAY_ANTHROPIC):
    return prepare_request(body, base_url=base_url, api_key='test-key',
                           api_protocol='anthropic')


def _header(plan, name):
    return next((value for key, value in plan.hdrs.items()
                 if key.lower() == name.lower()), None)


def test_live_conversation_keys_do_not_rotate():
    """The shared owner derives the digest Codex/Responses already sent."""
    fixture = json.loads((ROOT / 'packages/tofu-runtime/tests/fixtures/'
                          'python_responses_request.json').read_text())
    entry = next(item for item in fixture
                 if item['name'] == 'codex_cache_affinity')
    assert (cache_affinity_key({'_conv_id': 'owned-conversation'})
            == entry['body']['prompt_cache_key'])


def test_lineage_is_the_explicit_scope_then_conversation_then_task():
    child = cache_affinity_key({CACHE_AFFINITY_SCOPE_KEY: 'invocation:7:t2',
                                '_conv_id': 'conv-1', '_task_id': 't2'})
    root = cache_affinity_key({'_conv_id': 'conv-1', '_task_id': 't1'})
    assert child != root
    assert root == cache_affinity_key({CACHE_AFFINITY_SCOPE_KEY: 'conv-1'})
    assert (cache_affinity_key({'_task_id': 't1'})
            == cache_affinity_key({CACHE_AFFINITY_SCOPE_KEY: 't1'}))
    assert cache_affinity_key({}) == ''
    assert 'conv-1' not in root and 'invocation' not in child


@pytest.mark.parametrize('protocol',
                         ['openai', 'anthropic', 'responses', 'openai_responses'])
def test_gateway_carrier_binding_holds_on_every_wire_face(protocol):
    assert route_feature(DECLARED, CARRIER, protocol=protocol) is True
    assert route_feature({'features': {}}, CARRIER, protocol=protocol) is False
    assert route_feature({'features': {CARRIER: 'unsupported'}}, CARRIER,
                         protocol=protocol) is False
    # Subscription routes speak first-party dialects, never gateway carriers.
    assert route_feature(DECLARED, CARRIER, protocol=protocol,
                         oauth='codex') is False
    assert route_feature(None, CARRIER, protocol=protocol) is None


def test_declared_carrier_is_added_and_an_owner_header_wins():
    key = cache_affinity_key({'_conv_id': 'conv-1'})
    headers = {}
    merge_cache_affinity_headers(headers, DECLARED, protocol='anthropic',
                                 oauth='', key=key)
    assert headers == {'Mt-Session-Id': key}

    owner = {'mt-session-id': 'pinned-by-owner'}
    merge_cache_affinity_headers(owner, DECLARED, protocol='anthropic',
                                 oauth='', key=key)
    assert owner == {'mt-session-id': 'pinned-by-owner'}

    for profile, request_key, oauth in ((None, key, ''),
                                        ({'features': {}}, key, ''),
                                        (DECLARED, '', ''),
                                        (DECLARED, key, 'claude')):
        untouched = {}
        merge_cache_affinity_headers(untouched, profile, protocol='anthropic',
                                     oauth=oauth, key=request_key)
        assert untouched == {}


def test_one_lineage_rides_one_session_id_and_identity_stays_off_the_wire():
    first = _plan(_round('conv-1'))
    second = _plan(_round('conv-1', user='continue'))
    child = _plan(_round('invocation:7:task-2'))
    key = cache_affinity_key({CACHE_AFFINITY_SCOPE_KEY: 'conv-1'})

    assert _header(first, 'Mt-Session-Id') == key
    assert _header(second, 'Mt-Session-Id') == key
    assert _header(child, 'Mt-Session-Id') not in (None, key)
    wire = json.dumps(first.body.wire_body(), ensure_ascii=False)
    assert key not in wire
    assert 'conv-1' not in wire and CACHE_AFFINITY_SCOPE_KEY not in wire


def test_a_route_without_the_declaration_sends_no_carrier():
    plan = _plan(_round('conv-1', profile=None),
                 base_url='https://api.anthropic.com/v1')
    assert _header(plan, 'Mt-Session-Id') is None


def test_carrier_evidence_reaches_usage_without_the_key():
    body = _round('conv-1')
    plan = _plan(body)
    accumulator = SSEAccumulator(plan.body, plan.trace_id, plan.raw_dumper,
                                 plan.wire_translator, plan.t0)
    _, _, usage = accumulator.finalize()
    affinity = usage['_wire_routing']['affinity']
    assert affinity.split('@')[0] == 'Mt-Session-Id'
    assert cache_affinity_key(body) not in affinity


def test_evidence_names_every_slot_that_carried_the_key():
    key = cache_affinity_key({'_conv_id': 'c'})
    headers = {'session-id': key, 'thread-id': key, 'Authorization': 'Bearer x'}
    evidence = cache_affinity_evidence(key, headers, {'prompt_cache_key': key})
    assert evidence.split('@')[0] == 'session-id,thread-id,prompt_cache_key'
    assert cache_affinity_evidence('', headers, {}) == ''
    assert cache_affinity_evidence(key, {'Authorization': 'x'}, {}) == ''


def test_a_changed_or_lost_carrier_is_a_routing_change():
    before = routing_fingerprint(key_hash='k', endpoint='e',
                                 affinity='Mt-Session-Id@aaaa')
    assert diff_routing(before, dict(before)) == []
    assert diff_routing(before, routing_fingerprint(
        key_hash='k', endpoint='e', affinity='Mt-Session-Id@bbbb')) == ['<ns>affinity']
    assert diff_routing(before, routing_fingerprint(
        key_hash='k', endpoint='e')) == ['<ns>affinity']
    # A baseline captured before the field existed carried no carrier.
    legacy = {'key': 'k', 'beta': '', 'endpoint': 'e'}
    assert diff_routing(legacy, routing_fingerprint(key_hash='k', endpoint='e')) == []


def test_round_record_names_the_carrier():
    from lib.tasks_pkg.cache_tracking import _state as state
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'affinity-record-conv'
    logger = logging.getLogger('lib.tasks_pkg.cache_tracking._detect')
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        detect_cache_break(
            conv, [{'role': 'user', 'content': 'u'}], None, 'claude-opus-5.5',
            usage={'cache_read_input_tokens': 0,
                   'cache_creation_input_tokens': 30000,
                   '_wire_routing': routing_fingerprint(
                       key_hash='k', endpoint='e',
                       affinity='Mt-Session-Id@0123456789ab')},
            user_id=1)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        with state._cache_lock:
            for key in [key for key in state._cache_states if key[1] == conv]:
                del state._cache_states[key]
    records = [json.loads(line.split('[CacheRoundRecord]', 1)[1])
               for line in buffer.getvalue().splitlines()
               if '[CacheRoundRecord]' in line]
    assert records and records[-1]['affinity'] == 'Mt-Session-Id'
