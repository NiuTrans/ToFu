"""Model request preparation is pure, byte-stable and encodes each message once.

Golden receipts were recorded from the tree that still deep-copied whole
bodies and encoded diagnostics separately: sealed payload digests, semantic and
provider-prefix identities, admitted tokens and the stable diagnostic digests
of representative multi-turn requests (tools, images, signed reasoning, opaque
replay state) through build_body, selected-slot adaptation and stream ingress
on Chat, Messages (plain, Claude subscription cloak, CatPaw) and Responses
(default and Codex) wires, plus the non-stream transport.

Receipts are re-recorded only for an intended, documented wire change, never
for an input write: the frozen-input tests below must yield the same receipt.
Re-recorded wire changes: Claude system and tool breakpoints are bare
5-minute markers under the default-off ``cache_extended_ttl``
(docs/LLM_COST_OPTIMIZATION.md, "Stable-head cache TTL"), and Responses
requests carry no server ``context_management`` compaction trigger
(docs/CONTEXT_COMPACTION.md, "One provider-independent compaction owner").

Purity: every protocol converter, slot helper and whole preparation runs on a
deep-frozen input (``settle``), where any in-place write raises
``SettledRowMutation``, and must produce the bytes of a mutable copy.
Counters: no message-bearing structure is deep-copied, each canonical message
is encoded once for the semantic identity and each wire item once for the
sealed payload and its diagnostics; settled messages are not encoded again.
"""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from lib.conversation_sync.settled_rows import is_settled, settle

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = ('lib/0.py',)

URL = 'https://example.invalid/v1'
CATPAW_URL = 'https://mcli.internal.example.com/v1'
_PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGPkqjjBwMDAxMDAwMDAAAA'
        'O5AFORZ0V2QAAAABJRU5ErkJggg==')
SMALL_PNG = 'data:image/png;base64,' + _PNG
# PNG bytes declared as JPEG: validation reconciles the header.
MISLABELED_PNG = 'data:image/jpeg;base64,' + _PNG
# 1700 px wide: Claude adaptation re-encodes it under the long-edge cap.
WIDE_PNG = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAABqQAAAAGCAIAAAAqtmiwAAAATUlEQVR4'
            'nO3YQQ0AIBAEMeB5ipGILEyQkNu0CuY9s/YZAAAAAEB/63cAAAAAAPCG2QcAAAAAIcw+AAAAAAhh9gE'
            'AAABACLMPAAAAAEJcZ88BVimGx/kAAAAASUVORK5CYII=')


def tools():
    return [
        {'type': 'function', 'function': {
            'name': 'read_files', 'description': 'Read files. 读取文件。',
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': 'Relative path'},
                'limit': {'type': 'integer', 'minimum': 1, 'default': 200}},
                'required': ['path'], 'additionalProperties': False}}},
        # Renamed by the Claude subscription cloak.
        {'type': 'function', 'function': {
            'name': 'bash', 'description': 'Run a command.',
            'parameters': {'type': 'object', 'properties': {
                'command': {'type': 'string'}}, 'required': ['command']}}},
    ]


def history(turns=2, *, images=(SMALL_PNG,)):
    """Multi-turn tool loop with images, signed reasoning and escaped text."""
    messages = [{'role': 'system', 'content': 'You are terse.\n规则: 不要编造。'}]
    for turn in range(turns):
        messages += [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': f'Turn {turn}: compare "both" images\\n\t\u2028 🙂'},
                *({'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}
                  for url in images)]},
            {'role': 'assistant', 'content': f'Checking file {turn}.',
             'reasoning_content': f'Think {turn}: compare 上下文 widths\n',
             'thinking_signature': f'sig-{turn}-' + 'a' * 40,
             '_reasoning_continuity': 'completed_turn',
             'tool_calls': [
                 {'id': f'call_{turn}a', 'type': 'function', 'function': {
                     'name': 'read_files',
                     'arguments': json.dumps({'path': f'lib/{turn}.py', 'limit': 200})}},
                 {'id': f'call_{turn}b', 'type': 'function', 'function': {
                     'name': 'bash', 'arguments': json.dumps({'command': 'ls'})}}]},
            {'role': 'tool', 'tool_call_id': f'call_{turn}a',
             'content': f'def f{turn}():\n    return "\\x00\x07"\n' * 8},
            {'role': 'tool', 'tool_call_id': f'call_{turn}b',
             'content': [{'type': 'text', 'text': 'a.py\nb.py'}]},
        ]
    messages.append({'role': 'user', 'content': 'continue'})
    return messages


def anthropic_replay_body():
    """Canonical body carrying Anthropic-owned replay blocks and stale markers."""
    messages = history(1)
    messages[2] = {
        'role': 'assistant', 'content': [{'type': 'text', 'text': 'Checking file 0.',
                                          'cache_control': {'type': 'ephemeral'}}],
        'tool_calls': messages[2]['tool_calls'],
        '_reasoning_continuity': 'completed_turn',
        '_anthropic_content_blocks': [
            {'type': 'thinking', 'thinking': 'native order', 'signature': 'c2ln' * 8},
            {'type': 'text', 'text': 'Checking file 0.'},
            {'name': 'read_files', 'type': 'tool_use', 'id': 'call_0a',
             'input': {'path': 'lib/0.py', 'limit': 200}}],
    }
    messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_9', 'type': 'function', 'function': {
            'name': 'bash', 'arguments': {'command': 'pwd'}}}]})
    messages.append({'role': 'tool', 'tool_call_id': 'call_9', 'content': '/work'})
    return {'model': 'claude-sonnet-4-6', 'stream': True, 'max_tokens': 4096,
            'messages': messages, 'tools': tools(),
            'tool_choice': {'type': 'tool', 'name': 'bash'},
            'thinking': {'type': 'enabled', 'budget_tokens': 2048},
            '_task_id': 'task-golden', '_conv_id': 'conv-golden'}


def responses_replay_body():
    """Canonical body whose assistant turn replays opaque Responses items."""
    messages = history(1)
    messages[2] = dict(messages[2], _responses_items=[
        {'type': 'reasoning', 'id': 'rs_1', 'summary': [],
         'encrypted_content': 'gAAAAB' + 'x' * 64}])
    return {'model': 'gpt-5.6', 'stream': True, 'max_tokens': 32000,
            'messages': messages, 'tools': tools(), 'tool_choice': 'auto',
            '_responses_feature_profile': 'openai', '_task_id': 'task-golden',
            '_conv_id': 'conv-golden', '_working_set_tokens': 120000}


def slot(model, protocol, *, provider='bench', responses_profile=''):
    return SimpleNamespace(
        model=model, logical_model=model, provider_id=provider,
        routing_provider_id=provider, route_offering_id='', route_deployment_id='',
        protocol=protocol, max_output_tokens=0, thinking_format='',
        inference_profile=None, responses_profile=responses_profile,
        context_window=1_000_000)


# name: (source model, slot, prepare_request keyword arguments)
SLOT_CASES = {
    'chat-gpt': ('gpt-4o', slot('gpt-4o', 'openai'), {}),
    'chat-claude-markers': ('claude-sonnet-4-6', slot('claude-sonnet-4-6', 'openai'), {}),
    'chat-glm-markers': ('glm-5', slot('glm-5', 'openai'), {}),
    'chat-gemini-signatures': ('gemini-3-pro', slot('gemini-3-pro', 'openai'), {}),
    'messages-claude': ('claude-sonnet-4-6', slot('claude-sonnet-4-6', 'anthropic'), {}),
    'messages-claude-cloak': ('claude-sonnet-4-6', slot('claude-sonnet-4-6', 'anthropic'),
                              {'oauth': 'claude'}),
    'messages-catpaw': ('kimi-k3', slot('kimi-k3', 'anthropic'), {'base_url': CATPAW_URL}),
    'responses-gpt': ('gpt-5.6', slot('gpt-5.6', 'responses', responses_profile='openai'), {}),
    'responses-codex': ('gpt-5.6', slot('gpt-5.6', 'responses'), {'oauth': 'codex'}),
    # A prebuilt body swapped onto Claude re-validates and re-scales images.
    'swap-gpt-to-claude': ('gpt-4o', slot('claude-sonnet-4-6', 'openai'), {}),
}
DIRECT_CASES = {
    'direct-messages-replay': (anthropic_replay_body, {'api_protocol': 'anthropic'}),
    'direct-messages-replay-cloak': (anthropic_replay_body,
                                     {'api_protocol': 'anthropic', 'oauth': 'claude'}),
    'direct-chat-replay': (anthropic_replay_body, {'api_protocol': 'openai'}),
    'direct-responses-replay': (responses_replay_body, {'api_protocol': 'responses'}),
}
NONSTREAM_CASES = {
    'nonstream-chat': ('gpt-4o', 'openai', ''),
    'nonstream-messages-cloak': ('claude-sonnet-4-6', 'anthropic', 'claude'),
    'nonstream-responses': ('gpt-5.6', 'responses', ''),
}


def _count(messages, **kwargs):
    """Deterministic local count that still depends on the counted input."""
    text = json.dumps([messages, kwargs.get('system'), kwargs.get('tools')],
                      ensure_ascii=False, sort_keys=True, default=str)
    return {'tokens': len(text) // 4}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', _count)
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 1_000_000)
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',
                        lambda oauth, body, headers, **kw: ('synthetic', dict(headers or {}), body))
    monkeypatch.setattr('lib.oauth.outbound._fake_user_id', lambda: 'user_' + '0' * 64
                        + '_account_00000000-0000-0000-0000-000000000000'
                        + '_session_00000000-0000-0000-0000-000000000000')
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=repr).encode()).hexdigest()


def plan_receipt(plan):
    sealed, dumper = plan.body, plan.raw_dumper
    return {'payload': hashlib.sha256(sealed.payload).hexdigest(),
            'semantic': sealed.semantic_id, 'prefix': sealed.provider_prefix_id,
            'tokens': sealed.input_tokens,
            # Stable digests only; keyed runtime hashes are process-local.
            'diagnostics': _digest([dumper.wire_static, dumper.wire_region,
                                    dumper.wire_system, dumper.wire_tool_surface])}


def built_body(model, *, turns=2, images=(SMALL_PNG,)):
    from lib.llm import build_body
    body = build_body(model, history(turns, images=images), max_tokens=8192,
                      thinking_enabled=True, thinking_depth='medium', tools=tools(),
                      stream=True, provider_id='bench', defer_reasoning=True)
    body['_task_id'] = 'task-golden'
    body['_conv_id'] = 'conv-golden'
    return body


def adapt(target, body):
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    return _adapt_stream_body_for_slot(
        target, body, True, tools=None, max_tokens=None, temperature=None,
        thinking_enabled=None, preset=None, effort=None)


def run_slot_case(name, *, freeze=False):
    from lib.llm._sse_core import prepare_request
    source_model, target, options = SLOT_CASES[name]
    body = built_body(source_model)
    adapted = adapt(target, settle(body) if freeze else body)
    adapted_digest = _digest(adapted)
    options = {'base_url': URL, **options}
    plan = prepare_request(dict(settle(adapted)) if freeze else adapted,
                           api_protocol=target.protocol, **options)
    return {'adapted': adapted_digest, **plan_receipt(plan)}


def run_direct_case(name, *, freeze=False):
    from lib.llm._sse_core import prepare_request
    build, options = DIRECT_CASES[name]
    body = build()
    plan = prepare_request(dict(settle(body)) if freeze else body,
                           base_url=URL, **options)
    return plan_receipt(plan)


def run_nonstream_case(name, monkeypatch):
    import importlib
    chat_module = importlib.import_module('lib.llm.chat')
    model, protocol, oauth = NONSTREAM_CASES[name]
    sent = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            if protocol == 'anthropic':
                return {'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
                        'usage': {'input_tokens': 10, 'output_tokens': 2}}
            if protocol == 'responses':
                return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'ok'}]}],
                        'usage': {'input_tokens': 10, 'output_tokens': 2}}
            return {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}

    def post(url, **kwargs):
        sent.append(kwargs.get('data', kwargs.get('content')))
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    chat_module.chat(history(2), model, extra={'tools': tools(), '_task_id': 'task-golden',
                                               '_conv_id': 'conv-golden'},
                     base_url=URL, api_key='synthetic', api_protocol=protocol,
                     oauth=oauth, max_retries=0)
    return {'payload': hashlib.sha256(sent[0]).hexdigest()}


def receipts(monkeypatch):
    found = {name: run_slot_case(name) for name in SLOT_CASES}
    found.update({name: run_direct_case(name) for name in DIRECT_CASES})
    found.update({name: run_nonstream_case(name, monkeypatch) for name in NONSTREAM_CASES})
    return found


PINNED = {
    'chat-claude-markers': {
        'adapted': '453a5ea3262296f4aa5a2710c94e27eb1fc84d44adaa4e23a86e13c2fcc8ec41',
        'payload': 'dd029b04113c9f2755097dfd4637529f47d2eeba74953d2261dec0ab1699e7da',
        'semantic': '9b3b5b86780fa409f25862ddde06de8589876c05502feccba4c3696c98cccef6',
        'prefix': 'f4f539ef535d5eadc34ff807e6302ab6a81af33ff71a3c6b9ac8ed3375db80fc',
        'tokens': 925,
        'diagnostics': 'd507f4a1effd818274150692e8df3507691e68d9d38483aa3eb37631c9d8e2df',
    },
    'chat-gemini-signatures': {
        'adapted': 'ae7bacaacb037ea178c24ea6be75c85b043510ac565677f35a595d8a3cd07b55',
        'payload': '2219381adad78534864ebc3b2a018d8ff1bc8b466d481dab9d574cbb3f057c13',
        'semantic': 'f2c957a8a3a562f88ae14e934e868ebc370036c84b9c33480f4adb955e9c0567',
        'prefix': '9b6cf164df63e2ead18c13d894d80d830568bd3d59318b7384950acc153ce479',
        'tokens': 821,
        'diagnostics': 'f6dae1db69ca0849decaea5a488638604ace041b1581e84f255e5ddd0f9d911b',
    },
    'chat-glm-markers': {
        'adapted': '749693617bf1935f7ccfb9e24df4a320a97e8d3567dedda132e19c8caa0a7f4d',
        'payload': '618ef1238908b2363747f5feb0a0f87e95c82e0ae3d2ea7e03813718d075de6a',
        'semantic': '526d2cf64bffe80d5d2088e7f240694499cfeb7f0de6a833bfdcc5f6f93007b4',
        'prefix': 'f671ebb7687f8cdf00e04f2be72235fb4db1ce8e5cd632b330917c16c96866c0',
        'tokens': 909,
        'diagnostics': '5099bcdea03943982308860dfed5bfc1796cca0c4bed11b2459b3620f05098ba',
    },
    'chat-gpt': {
        'adapted': '58cd65ee00f88c0e38b96396f28ae67685e2828814078885ca9fcce5e34a126c',
        'payload': '559cd90b259a1ace6e02e2dce09e1a73c01dc963332684f50159d4ec75ca02eb',
        'semantic': '98a3456782ac2249bf920b5b508cfc0ade3f38febf25ab749e1a55cc8f55e359',
        'prefix': '8251d2ee2efed514c09aa5c4eb3d4832b3548bc8e7fe7c70694c0e528bb85ff7',
        'tokens': 777,
        'diagnostics': 'f6dae1db69ca0849decaea5a488638604ace041b1581e84f255e5ddd0f9d911b',
    },
    'direct-chat-replay': {
        'payload': '374e40e9fcc40ad5bf24dba17631b0c70ff1eccc82f4e6bac1a7931a8b3c0002',
        'semantic': '1988a4b3909a78e08da484bc2d9b59a75909fa5818a8c08af5d5298bf4053e2d',
        'prefix': '08271b4566f3d265f65afca2df6d5f044a2d2e49f7f90339c87b367acd0806da',
        'tokens': 621,
        'diagnostics': 'ce90a5ffa3f78e86230d5a9adce887caeb40c46fc191a3b1a9766e1b6d3ff3c0',
    },
    'direct-messages-replay': {
        'payload': 'f4263352782888fecbf759a562124ddbe377a73c5a57c4c40c136fada85bfed1',
        'semantic': 'f3768547b812af0b9a6d70bf5af4febb3d600776b83813079c05ce5b8ad8a174',
        'prefix': '07c8f3bd763a5d7088077709b09fe7df122f86fa6bfe3cc8cb209bc4196e2580',
        'tokens': 529,
        'diagnostics': '8b0a49cf929d130731e9b8de7f4598e3c7b32ed2961cddff19258b50d6abfded',
    },
    'direct-messages-replay-cloak': {
        'payload': '803e327095b30ab501a324eef133b7c1ca05772545fe1ee465cf711980cbd96a',
        'semantic': 'f3768547b812af0b9a6d70bf5af4febb3d600776b83813079c05ce5b8ad8a174',
        'prefix': '3fee5fb7f0fb2cdac10ee6e04bfe115144039a349f87191329bfe0cbc8833f07',
        'tokens': 2248,
        'diagnostics': 'd595ba10c0d4f0b608731aaa94717624a678450d12b96b840dfad8d0983f900d',
    },
    'direct-responses-replay': {
        'payload': 'e66abdc2e5ee6b121abc88bd8ccf6e20b2eac72e44bfa959819ea97d7a9ef4ee',
        'semantic': 'd33f5df27252b227f4141ecc758db1a315f0dea8afaff98d16c8fd114a9d0b0f',
        'prefix': '2a43ac7c5e0b322817b6ceeef0b4d9ec495b9139c5ae993a47287caecb4c7bf0',
        'tokens': 508,
        'diagnostics': '163c0aa4de37b7ab0e79bb0e47e2d3a8c91510095bf8ce2391fbe1f604932a87',
    },
    'messages-catpaw': {
        'adapted': '2c1ab5b582ebddb2e0a3180f1ce07c3500653bf6ceddbb4e8253770dde72135b',
        'payload': '640ff68ca5d1993a7a925103532e314bb48be44144ec45d3d9a186c673b4e599',
        'semantic': '54e003f457b7ca6270d1b057c8bd9e2decd86a16aef4ae4cdd91d2c13c78bca8',
        'prefix': '3e62e11240c7372d777eac5129f15e32467823c0f71d52262cfe289a26b309f3',
        'tokens': 682,
        'diagnostics': 'bc84be495ab65a9b6db46789c3fa5120dd8d0c280671ceec81fd9e6290ebfee4',
    },
    'messages-claude': {
        'adapted': '23a8b67178a1801be057c10b2fe30b70b108dbba9cd67fadd8b91b5b8533e59b',
        'payload': '749f8def2667241b8b50d3c9e0103dad4e14595f1e15236596224fde1748334d',
        'semantic': '9b3b5b86780fa409f25862ddde06de8589876c05502feccba4c3696c98cccef6',
        'prefix': '6c8c510ecca880856afafa62327a09f945cc86741e90b45180988f77d32c9c3d',
        'tokens': 721,
        'diagnostics': 'e9d7b829580146f94746813b84306d1b1d417d367db74fb839af193408936cdd',
    },
    'messages-claude-cloak': {
        'adapted': '23a8b67178a1801be057c10b2fe30b70b108dbba9cd67fadd8b91b5b8533e59b',
        'payload': '9b8823ad68c0cff798fbea3248870b7c40b442162d0a9eeb6e2f9833894dd0ec',
        'semantic': '9b3b5b86780fa409f25862ddde06de8589876c05502feccba4c3696c98cccef6',
        'prefix': 'a5db3fb8c7def24700145871a917a539bc4debaa69e5f2b275c5d55fb343b4e9',
        'tokens': 2440,
        'diagnostics': 'ed074e934f719269258a30914f81bb8aa6b1da26dbe43a105357e85f0a6c0425',
    },
    'nonstream-chat': {
        'payload': '0239ef3ae3f3f0f2a5200463ab974aef391dd2c804c3e32c1c531b85816a61f8',
    },
    'nonstream-messages-cloak': {
        'payload': '6a69abd19d9ae50affbc6cdd806a456a33b5aa7b73a9a7cdd6a134d285c54675',
    },
    'nonstream-responses': {
        'payload': 'e2b96d129df00eb590571ac3f417e709e41a5959513c8e8dda840c303f5c9175',
    },
    'responses-codex': {
        'adapted': 'e525d3d75c970d6705ca32dcdc21e1ff936e679c1e759143b97dd8af705c35b9',
        'payload': '3d4ed3b234627aa99f522c2e7142a053c9a7e63bde6b0e999fcd94875b0c1ddd',
        'semantic': 'ac13db8fd59cf2c21590c70af1e81c6f84c6483ddbec041543ceb1e83ced75ae',
        'prefix': '568ddbd5d9eefce3db33cc90db3207be39ed73a6788350f46e118749bf75f6ac',
        'tokens': 777,
        'diagnostics': '820cbd039f433d4f4fcaeab1d195b31ecabf2ed0d41c4cefb1b26c2b9013a840',
    },
    'responses-gpt': {
        'adapted': '631efa9993029d3c48f307574e5e10181e6ea549b783dfde3abe942c5ed7d1b3',
        'payload': '7382a004edc4cff74d5e741284f3800762b67c8b53910d3dd37ff492e91a291c',
        'semantic': 'ac13db8fd59cf2c21590c70af1e81c6f84c6483ddbec041543ceb1e83ced75ae',
        'prefix': '8ba203ed3983339a2210a473c7804a391349022bb2f7c6db134fce3d687800bd',
        'tokens': 777,
        'diagnostics': '820cbd039f433d4f4fcaeab1d195b31ecabf2ed0d41c4cefb1b26c2b9013a840',
    },
    'swap-gpt-to-claude': {
        'adapted': '4d7967431b90d5e96520721d3b52079f965d2b1cd5e2d063d920974fac292bfb',
        'payload': 'a6a21a997b698ef1e9ca98baa75b3c824e2b42717362e4e27a487ef15a2e6b15',
        'semantic': '2b3ac024deb1063e4a813ecc7ec5e61059c8f042a107c6dfe3f434514b394b79',
        'prefix': '58681cb06a9d3f6f25ab633a1ce7038519b20630d759d8c0d5a7d95cb85be1c8',
        'tokens': 849,
        'diagnostics': 'd507f4a1effd818274150692e8df3507691e68d9d38483aa3eb37631c9d8e2df',
    },
}


def test_golden_receipts_are_byte_identical(offline, monkeypatch):
    assert receipts(monkeypatch) == PINNED


def _outcome(function, *args, **kwargs):
    """Wire JSON of a result (insertion order kept), or the typed failure."""
    from lib.llm.conversion import ProtocolConversionError
    try:
        result = function(*args, **kwargs)
    except ProtocolConversionError as error:
        return ('refused', error.code)
    return json.dumps(result, ensure_ascii=False, default=repr)


def frozen(value):
    """Deep-immutable twin: every nested in-place write raises."""
    result = settle(value)
    assert is_settled(result)
    return result


def _converters():
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.catpaw_gateway import apply_catpaw_shape
    from lib.llm.openai_outbound import openai_body_to_chat
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.oauth.outbound import apply_claude_cloak
    return {
        'chat': openai_body_to_chat,
        'messages': openai_body_to_anthropic,
        'messages-cloak': lambda body: apply_claude_cloak(openai_body_to_anthropic(body)),
        'messages-catpaw': lambda body: apply_catpaw_shape(openai_body_to_anthropic(body)),
        'responses': lambda body: openai_body_to_responses(body, stream=True),
        'responses-codex': lambda body: openai_body_to_responses(
            body, profile='codex', stream=True),
    }


@pytest.mark.parametrize('name', ['chat', 'messages', 'messages-cloak', 'messages-catpaw',
                                  'responses', 'responses-codex'])
@pytest.mark.parametrize('build', [anthropic_replay_body, responses_replay_body],
                         ids=['anthropic-replay', 'responses-replay'])
def test_protocol_converters_never_write_their_input(offline, name, build):
    convert = _converters()[name]
    expected = _outcome(convert, copy.deepcopy(build()))
    assert _outcome(convert, frozen(build())) == expected


def _image_history():
    messages = history(1, images=(SMALL_PNG, MISLABELED_PNG,
                                  'data:image/png;base64,' + 'A' * 80,
                                  WIDE_PNG, 'https://example.invalid/a.png'))
    messages[2] = {key: value for key, value in messages[2].items()
                   if key != 'thinking_signature'}
    messages.append({'role': 'assistant', 'content': 'prefill tail'})
    return messages


def _slot_helpers():
    from lib.llm.body import (
        _downscale_oversized_images, _inject_gemini_thought_signatures,
        _strip_trailing_assistant_for_claude, _validate_image_blocks,
    )
    return {
        'claude-prefill': lambda messages: _strip_trailing_assistant_for_claude(
            messages, 'claude-sonnet-4-6'),
        'image-validation': _validate_image_blocks,
        'image-downscale': lambda messages: _downscale_oversized_images(
            messages, 'claude-sonnet-4-6'),
        'gemini-signatures': lambda messages: _inject_gemini_thought_signatures(
            messages, 'gemini-3-pro'),
    }


@pytest.mark.parametrize('name', ['claude-prefill', 'image-validation', 'image-downscale',
                                  'gemini-signatures'])
def test_slot_message_helpers_return_adapted_copies(offline, name):
    helper = _slot_helpers()[name]
    source = _image_history()
    expected = helper(copy.deepcopy(source))
    assert isinstance(expected, list) and expected != source
    assert _outcome(helper, frozen(source)) == json.dumps(
        expected, ensure_ascii=False, default=repr)


@pytest.mark.parametrize('model,protocol', [
    ('claude-sonnet-4-6', 'anthropic'), ('claude-sonnet-4-6', 'openai'),
    ('glm-5', 'openai')])
@pytest.mark.parametrize('build', [anthropic_replay_body, lambda: built_body('claude-sonnet-4-6')],
                         ids=['stale-markers', 'built'])
def test_cache_breakpoints_replace_only_the_envelope(offline, model, protocol, build):
    from lib.llm.cache import add_cache_breakpoints
    source = dict(build(), model=model)
    source.pop('_task_id', None)
    expected = copy.deepcopy(source)
    add_cache_breakpoints(expected, api_protocol=protocol)
    envelope = dict(frozen(source))
    add_cache_breakpoints(envelope, api_protocol=protocol)
    assert json.dumps(envelope, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize('name', sorted(SLOT_CASES))
def test_slot_adaptation_and_ingress_never_write_their_input(offline, name):
    assert run_slot_case(name, freeze=True) == PINNED[name]


@pytest.mark.parametrize('name', sorted(DIRECT_CASES))
def test_ingress_never_writes_its_input(offline, name):
    assert run_direct_case(name, freeze=True) == PINNED[name]


class _DeepCopyLedger:
    """Every deepcopy of a request body or a message history, however imported."""

    def __init__(self):
        self.request_sized = []

    @staticmethod
    def _request_sized(value):
        if isinstance(value, dict):
            return 'messages' in value or 'input' in value
        return (isinstance(value, list) and bool(value) and isinstance(value[0], dict)
                and ('role' in value[0] or 'type' in value[0]))

    def __enter__(self):
        import sys
        target = copy.deepcopy.__code__

        def profile(frame, event, _arg):
            if (event == 'call' and frame.f_code is target
                    and frame.f_back is not None and frame.f_back.f_code is not target
                    and self._request_sized(frame.f_locals.get('x'))):
                self.request_sized.append(frame.f_back.f_code.co_qualname)
        sys.setprofile(profile)
        return self

    def __exit__(self, *_exc):
        import sys
        sys.setprofile(None)


@pytest.mark.parametrize('name', sorted(SLOT_CASES))
def test_preparation_never_deep_copies_a_body_or_history(offline, name):
    from lib.llm._sse_core import prepare_request
    source_model, target, options = SLOT_CASES[name]
    body = built_body(source_model)
    with _DeepCopyLedger() as ledger:
        adapted = adapt(target, body)
        prepare_request(adapted, api_protocol=target.protocol,
                        **{'base_url': URL, **options})
    assert ledger.request_sized == []


class _EncodeCounter:
    """Every value passed to the request encoder, by key order."""

    def __init__(self, monkeypatch):
        import lib.llm.request as request_module
        self.sorted, self.wire = [], []
        encode = request_module._encode

        def counted(value, *, sorted_keys=False):
            (self.sorted if sorted_keys else self.wire).append(value)
            return encode(value, sorted_keys=sorted_keys)

        monkeypatch.setattr(request_module, '_encode', counted)


@pytest.mark.parametrize('name', ['chat-gpt', 'responses-gpt'])
def test_settled_history_is_encoded_once_across_requests(offline, monkeypatch, name):
    """A later request over the same settled history encodes only new messages
    for its semantic identity and reuses settled wire items' sealed bytes,
    with the bytes of a mutable history."""
    from lib.llm._sse_core import prepare_request
    source_model, target, _options = SLOT_CASES[name]
    protocol = target.protocol
    options = {'base_url': URL, 'api_protocol': protocol}
    body = adapt(target, built_body(source_model, turns=3))
    expected = plan_receipt(prepare_request(copy.deepcopy(body), **options))
    rows = settle(body['messages'])
    prepare_request(dict(body, messages=list(rows[:-2])), **options)

    counter = _EncodeCounter(monkeypatch)
    plan = prepare_request(dict(body, messages=list(rows)), **options)

    assert plan_receipt(plan) == expected
    assert [value for value in counter.sorted if is_settled(value)] == list(rows[-2:])
    if protocol == 'openai':
        wire_items = plan.raw_dumper.body['messages']
        reused = [item for item in wire_items[:len(rows) - 2]
                  if any(item is row for row in rows)]
        assert reused, 'settled messages without private fields are their own wire items'
        encoded = {id(value) for value in counter.wire}
        assert [item for item in reused if id(item['content']) in encoded] == []
