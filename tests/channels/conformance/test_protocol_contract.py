"""The channel protocol contract and its codecs (contracts/channel_protocol_v1.schema.json).

Specifies what the conformance cases take for granted: the contract is valid
and internally complete, every in-repository descriptor resolves against the
platform facts of channels_v1 and agrees with its channel traits, the codecs
reproduce today's thread keys, event IDs and outbox vocabulary exactly, no
envelope can carry an authority field, versions negotiate as the policy says,
and the stdio binding fails a pending request when its process goes away.
"""
from __future__ import annotations

import json
import sys

import pytest

from lib.channels.model import CONTRACT as CHANNELS_CONTRACT
from lib.channels.model import ChannelEvent, ChannelMedia, ThreadKey
from lib.channels.protocol import codec
from lib.channels.protocol.contract import (
    CONTRACT, ERRORS, HEALTH_CODES, LEGACY, METHODS, SUPPORTED_VERSIONS, WATCHDOG_STATES, ProtocolError,
    adapter_channels, adapter_descriptor, negotiate_version, validate,
)
from tests.channels.conftest import OWNER

pytestmark = pytest.mark.unit
DEFS = CONTRACT['$defs']


def test_the_contract_is_a_valid_schema_with_a_complete_health_vocabulary():
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(CONTRACT)
    assert set(DEFS['HealthCode']['enum']) == set(HEALTH_CODES)
    account_states = set(CHANNELS_CONTRACT['$defs']['AccountState']['enum'])
    remedies = {key for key in CONTRACT['x-remedies'] if key != 'description'}
    assert remedies == set(DEFS['Remedy']['enum'])
    for code, entry in HEALTH_CODES.items():
        assert entry['state'] in DEFS['HealthState']['enum'], code
        assert entry['remedy'] in remedies, code
        assert entry['retry'] in ('core_tick', 'resume_at', 'owner', 'none'), code
        assert entry['legacyState'] in account_states, code
    assert WATCHDOG_STATES <= set(DEFS['HealthState']['enum'])
    assert set(LEGACY['accountStateHealth']) == account_states
    assert all(entry is None or entry['code'] in HEALTH_CODES for entry in LEGACY['accountStateHealth'].values())


def test_every_method_names_its_definitions_and_capability():
    descriptor = DEFS['CapabilityDescriptor']['properties']
    for name, spec in METHODS.items():
        assert spec['direction'] in ('core_to_adapter', 'adapter_to_core'), name
        assert spec['params'] in DEFS, name
        assert spec['kind'] == 'notification' or spec['result'] in DEFS, name
        if spec.get('capability'):
            group, field = spec['capability'].split('.')
            assert field in descriptor[group]['properties'], name
    codes = [code for key, code in ERRORS.items()]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize('channel', adapter_channels())
def test_in_repository_descriptors_resolve_and_agree_with_the_channel_traits(channel):
    descriptor = adapter_descriptor(channel)
    traits = CHANNELS_CONTRACT['x-channelTraits'][channel]
    assert channel in CHANNELS_CONTRACT['$defs']['Channel']['enum']
    assert set(descriptor['threadKinds']) <= set(CHANNELS_CONTRACT['$defs']['ThreadKind']['enum'])
    assert descriptor['commandChat'] == traits['commandChat']
    identities = descriptor['identities'].values()
    assert any(identity['quoteReply'] for identity in identities) == traits['quoteReply']
    assert any(identity['attachments'] for identity in identities) == traits['attachments']
    for identity in identities:
        assert all(isinstance(limit['limit'], int) and isinstance(limit['windowMs'], int)
                   for limit in identity['rateLimits'])


def test_platform_limits_come_from_the_channels_contract_not_copies():
    """A descriptor number is a $fact into channels_v1; only a window's unit (per second, minute, hour)
    and GitHub's one mutation per spacing are written out."""
    literals = []

    def walk(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, name)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, int) and not isinstance(value, bool) and key != 'windowMs':
            literals.append((key, value))
    walk({name: entry for name, entry in CONTRACT['x-adapters'].items() if name != 'description'})
    assert literals == [('limit', 1)]
    assert adapter_descriptor('wecom')['identities']['bot']['maxTextBytes'] == CHANNELS_CONTRACT['x-wecom']['markdownBytes']
    assert adapter_descriptor('dx')['identities']['bot']['maxTextBytes'] == \
        CHANNELS_CONTRACT['x-providerLimits']['dxRobotTextBytes']
    assert adapter_descriptor('qq')['outbound']['replyWindows'] == CHANNELS_CONTRACT['x-qq']['passiveReply']


@pytest.mark.parametrize('channel, ref, key', [
    ('dx', {'kind': 'gid', 'id': '500'}, 'dx:gid:500'),
    ('dx', {'kind': 'pid', 'id': '9009'}, 'dx:pid:9009'),
    ('github', {'kind': 'issue', 'id': 'Octo/Repo#12'}, 'github:octo/repo#12'),
    ('wecom', {'kind': 'uid', 'id': 'OwnerUser01'}, None),
    ('wechat', {'kind': 'uid', 'id': 'owner@im.wechat'}, None),
])
def test_thread_keys_keep_their_current_form(channel, ref, key):
    from lib.channels.bots import ids
    thread = codec.thread_key(channel, ref)
    assert str(thread) == (key or str(ids.thread_key(channel, ref['kind'], ref['id'])))
    assert codec.thread_key(channel, codec.thread_ref(thread)) == thread


def test_external_ids_reproduce_todays_event_ids(store):
    from lib.channels.bots import ids
    assert CONTRACT['x-externalIds']['secondsDigits'] == ids._SECONDS_DIGITS
    assert CONTRACT['x-externalIds']['digestDigits'] == ids._DIGEST_DIGITS
    assert codec.external_id('dx', {'id': '7123456789012345678', 'seq': '7123456789012345678'}) == '7123456789012345678'
    assert codec.external_id('qq', {'id': 'm1', 'timeMs': 1_793_934_000_999}) == ids.external_id(1_793_934_000, 'm1')
    first = codec.external_id('wecom', {'id': 'msg-1'}, store=store, clock=lambda: 1_793_934_000)
    again = codec.external_id('wecom', {'id': 'msg-1'}, store=store, clock=lambda: 1_793_934_999)
    assert first == again == ids.external_id(1_793_934_000, 'msg-1')


def test_a_legacy_event_survives_the_envelope_round_trip():
    event = ChannelEvent(
        thread=ThreadKey.parse('dx:gid:500'), external_id='7123456789012345678', sender_id='2002',
        sender_name='Ann', sender_kind='bot', text='hi @Owner', sent_at_ms=1_761_000_000_000, thread_title='Team',
        mentioned_ids=('1001',), mentions_all=True, reply_to_external_id='7123456789012345000',
        reply_to_sender_id='1001', client_id='uu-1', outbox_marker='ob1', own_connection=True, host_sent=True,
        url='', media=(ChannelMedia('file', 'deck.pptx', mime_type='application/x', size_bytes=3, source='s', token='t'),))
    envelope = codec.envelope_from_event(event, locator=lambda item: 'handle-1')
    validate('InboundEvent', envelope)
    assert 'token' not in json.dumps(envelope) and '"s"' not in json.dumps(envelope)
    back = codec.event_from_envelope('dx', envelope)
    assert back.media[0].source == 'handle-1'
    assert back == event  # media compare by facts; locators never compare


# ChannelEvent facts deliberately not carried: the platform's raw message type (parts carry the content).
_NOT_CARRIED = {'message_type'}


def test_every_channel_event_fact_crosses_the_protocol():
    """A new ChannelEvent fact (an echo fact such as host_sent) must get an envelope field and a codec
    mapping in the same change; the round trip below then covers it."""
    import dataclasses
    carried = {'thread', 'external_id', 'sender_id', 'sender_name', 'sender_kind', 'text', 'sent_at_ms', 'thread_title',
               'mentioned_ids', 'mentions_all', 'reply_to_external_id', 'reply_to_sender_id', 'client_id',
               'outbox_marker', 'own_connection', 'host_sent', 'url', 'media'}
    fields = {field.name for field in dataclasses.fields(ChannelEvent)}
    assert fields - _NOT_CARRIED == carried, (
        f'map {sorted(fields - _NOT_CARRIED - carried)} in lib/channels/protocol/codec.py (and EchoFacts or '
        'the envelope in contracts/channel_protocol_v1.schema.json)')


@pytest.mark.parametrize('field', ['owner', 'authority', 'isOwner', 'disposition'])
def test_no_envelope_can_carry_an_authority_field(field):
    envelope = codec.envelope_from_event(ChannelEvent(
        thread=ThreadKey.parse('dx:uid:2002'), external_id='7123456789012345678', sender_id='2002',
        sender_name='Ann', text='approve the draft', sent_at_ms=1))
    for target in (envelope, envelope['sender']):
        forged = json.loads(json.dumps(envelope))
        (forged if target is envelope else forged['sender'])[field] = True
        with pytest.raises(ProtocolError):
            validate('InboundEvent', forged)


def test_receipts_keep_the_outbox_vocabulary():
    from lib.channels.outbox import DEFERRED_SEND_CODES, DEFINITE_SEND_FAILURES
    assert DEFINITE_SEND_FAILURES <= set(DEFS['ReceiptCode']['enum'])
    assert DEFERRED_SEND_CODES == set(LEGACY['deferredSendCodes'])
    deferred = {'intentId': 'ob1', 'identity': 'self', 'status': 'deferred', 'code': 'rate_limited',
                'retryAfterMs': 1500}
    assert codec.legacy_from_receipt(validate('Receipt', deferred))['retryAfterMs'] == 1500
    intent = {'intentId': 'ob1', 'idempotencyKey': 'ob1', 'thread': {'kind': 'gid', 'id': '1'}, 'identity': 'self',
              'part': {'type': 'text', 'format': 'markdown', 'text': 'x'}}
    cases = [({'ok': True, 'externalId': '9', 'clientId': 'u'}, 'sent', None),
             ({'ok': False, 'error': {'code': 'forbidden'}}, 'failed', 'forbidden'),
             ({'ok': False, 'error': {'code': 'rate_limited'}}, 'deferred', 'rate_limited'),
             ({'ok': False, 'error': {'code': 'bridge_error', 'message': 'lost'}}, 'unknown', 'answer_lost')]
    for result, status, code in cases:
        receipt = validate('Receipt', codec.receipt_from_legacy(intent, result))
        assert (receipt['status'], receipt.get('code')) == (status, code)
        legacy = codec.legacy_from_receipt(receipt)
        assert legacy['ok'] == result['ok'] and (legacy['ok'] or legacy['error']['code'] in (code, 'unknown'))


def test_every_legacy_account_state_maps_to_typed_health_and_back():
    for state, entry in LEGACY['accountStateHealth'].items():
        health = codec.health_from_legacy(state, 'detail', at_ms=1)
        if entry is None:
            assert health is None
            continue
        validate('Health', health)
        if not entry['approximate']:
            assert codec.legacy_from_health(health)[0] == state
    assert codec.health_from_legacy('kicked', at_ms=1)['state'] == 'displaced'


def test_versions_negotiate_n_and_n_minus_one_and_refuse_the_rest(store, router):
    from lib.channels.protocol.adapter import ProtocolAdapter
    from lib.channels.protocol.host import AdapterHost, InProcessLink
    assert negotiate_version([SUPPORTED_VERSIONS[0], 99]) == SUPPORTED_VERSIONS[0]
    with pytest.raises(ProtocolError) as refused:
        negotiate_version([99])
    assert refused.value.code == ERRORS['unsupportedProtocolVersion']

    class Future(ProtocolAdapter):
        protocol_versions = (99,)

        def capabilities(self):
            return adapter_descriptor('dx')

    host = AdapterHost(OWNER, 'dx', InProcessLink(Future()), store=store)
    with pytest.raises(ProtocolError):
        host.open()
    assert (host.last_health['state'], host.last_health['code']) == ('runtime_missing', 'protocol_version_unsupported')
    assert store.account('dx')['status']['state'] == 'error'


def test_the_host_calls_only_declared_capabilities(store):
    from lib.channels.protocol.adapter import ProtocolAdapter
    from lib.channels.protocol.host import AdapterHost, InProcessLink

    class Minimal(ProtocolAdapter):
        def capabilities(self):
            return adapter_descriptor('wechat')

        def start(self, params):
            return {}

    host = AdapterHost(OWNER, 'wechat', InProcessLink(Minimal()), store=store)
    with pytest.raises(ProtocolError) as early:
        host.call('history', {'thread': {'kind': 'uid', 'id': 'x'}})
    assert early.value.code == ERRORS['notInitialized']
    host.open()
    with pytest.raises(ProtocolError) as missing:
        host.call('history', {'thread': {'kind': 'uid', 'id': 'x'}})
    assert missing.value.code == ERRORS['capabilityMissing']
    assert host.sender().history_since(codec.thread_key('wechat', {'kind': 'uid', 'id': 'x'}), 0) == []


def test_a_stdio_request_fails_when_the_adapter_process_ends():
    from lib.channels.protocol.stdio import StdioLink
    link = StdioLink([sys.executable, '-c', 'import sys; sys.stdin.readline()'])
    try:
        with pytest.raises(ProtocolError) as ended:
            link.request('send', {})
        assert ended.value.code == ERRORS['internalError']
    finally:
        assert link.close(5.0)


def test_a_stdio_adapter_answers_malformed_lines_and_unknown_methods():
    import io
    import threading
    from lib.channels.protocol.adapter import ProtocolAdapter
    from lib.channels.protocol.stdio import serve
    lines = b'not json\n' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'nope', 'params': {}}).encode() + b'\n'
    out = io.BytesIO()
    thread = threading.Thread(target=serve, args=(ProtocolAdapter(),), kwargs={'stdin': io.BytesIO(lines), 'stdout': out})
    thread.start()
    thread.join(5)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [answer['error']['code'] for answer in answers] == [ERRORS['parseError'], ERRORS['methodNotFound']]
