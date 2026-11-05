"""scripts/cache_ab_probe.py: single-property prompt-cache A/B arms, safe by default.

On 2026-10-28 a hand-written probe proved what broke root-lane prompt caching on
your-llm-gateway.example.com's Anthropic face: 12-tool requests identical except for their
names read their own cache back when the names joined to 132 or 444 bytes and
re-wrote 12,277 tokens on every send at 552 bytes. The script is the reusable
form of that probe, and its verdict is only as good as its arms: each must hit
its byte target exactly and differ from the others in nothing but the names.
Arming spends real tokens, so the default dry run must resolve the route without
importing transport, sending anything or printing the key. The armed loop runs
here only against fake transport modules.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import socket
import sys
import time
import types

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, '..', '..', 'scripts', 'cache_ab_probe.py')

if not os.path.isfile(_MOD_PATH):
    pytest.skip('scripts/cache_ab_probe.py not shipped in opensource',
                allow_module_level=True)

_KEYS = ['sk-sentinel-never-print-0f3a9e', 'sk-sentinel-never-print-7c21b4']
_KEY_FRAGMENT = 'sk-sentinel'
_BASE_URL = 'https://gateway.example/v1/anthropic'
_PROXIES = {'no_proxy': '*'}
_TTL_1H = {'type': 'ephemeral', 'ttl': '1h'}
_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_TRANSPORT_MODULES = ('lib.llm._transport', 'lib.llm.anthropic_outbound',
                      'lib.llm.anthropic_outbound._url', 'lib.proxy')


def _load(monkeypatch):
    monkeypatch.setattr(sys, 'path', list(sys.path))
    spec = importlib.util.spec_from_file_location('cache_ab_probe', _MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _joined(names):
    return sum(len(name.encode('utf-8')) + 1 for name in names)


def _config(tmp_path):
    path = tmp_path / 'server_config.json'
    path.write_text(json.dumps({'providers': [{
        'id': 'default', 'enabled': True, 'api_keys': _KEYS,
        'base_url': 'https://gateway.example/v1/openai/native',
        'faces': {
            'anthropic': {'protocol': 'anthropic', 'base_url': _BASE_URL},
            'openai': {'protocol': 'openai',
                       'base_url': 'https://gateway.example/v1/openai'},
        },
    }]}), encoding='utf-8')
    return str(path)


def _bodies(probe, nonce='0123456789ab'):
    return probe.build_arm_bodies([132, 444, 552], tools=12,
                                  model='kunlunbedrock.claude-opus-5.5-m17',
                                  prefix_tokens=12_000, nonce=nonce)


def _forbidden(*_args, **_kwargs):
    raise AssertionError('a dry run must not connect or wait')


@pytest.mark.parametrize(('target', 'count', 'arm'), [
    (132, 12, 0), (444, 12, 1), (552, 12, 2),  # the 2026-10-28 arms (defaults)
    (72, 12, 0), (780, 12, 0),                  # stem floor, 64-character ceiling
    (450, 12, 10), (6, 1, 0), (300, 35, 4),
    (1000, 101, 3),                             # stems of two widths
])
def test_name_set_hits_the_joined_byte_target_exactly(monkeypatch, target, count, arm):
    names = _load(monkeypatch).name_set(target, count, arm)
    assert len(names) == count
    assert _joined(names) == target
    assert all(_NAME.match(name) for name in names)
    assert len(set(names)) == count


@pytest.mark.parametrize(('target', 'count', 'reason'), [
    (71, 12, 'stems already need 72 bytes'),
    (781, 12, 'longer than 64 characters'),
    (10, 0, 'at least 1'),
])
def test_infeasible_targets_are_refused(monkeypatch, target, count, reason):
    with pytest.raises(ValueError, match=reason):
        _load(monkeypatch).name_set(target, count, 0)


def test_arm_bodies_differ_only_in_tool_names(monkeypatch):
    bodies = _bodies(_load(monkeypatch))
    assert list(bodies) == ['names=132B', 'names=444B', 'names=552B']
    blanked, all_names = set(), []
    for label, body in bodies.items():
        names = [tool['name'] for tool in body['tools']]
        assert _joined(names) == int(label[len('names='):-1])
        all_names += names
        clone = copy.deepcopy(body)
        for tool in clone['tools']:
            tool['name'] = ''
        blanked.add(json.dumps(clone, ensure_ascii=False))
    assert len(blanked) == 1
    assert len(set(all_names)) == len(all_names)

    body = bodies['names=444B']
    assert set(body) == {'model', 'max_tokens', 'stream', 'system', 'tools', 'messages'}
    assert (body['max_tokens'], body['stream']) == (16, True)
    assert body['messages'] == [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Reply with the single word OK.'}]}]
    assert len({(tool['description'], json.dumps(tool['input_schema']))
                for tool in body['tools']}) == 1
    assert body['tools'][0]['input_schema'] == {
        'type': 'object', 'properties': {'q': {'type': 'string', 'description': 'query'}},
        'required': ['q']}


def test_bodies_are_deterministic_and_keyed_to_the_run_nonce(monkeypatch):
    probe = _load(monkeypatch)
    first, again = _bodies(probe), _bodies(probe)
    other = _bodies(probe, nonce='ba9876543210')
    assert json.dumps(first) == json.dumps(again)
    system = first['names=132B']['system'][0]['text']
    assert '0123456789ab' in system
    assert len(system) >= 12_000 * 4
    assert other['names=132B']['system'] != first['names=132B']['system']
    assert other['names=132B']['tools'] == first['names=132B']['tools']


def test_only_the_last_tool_and_the_system_block_carry_the_1h_breakpoint(monkeypatch):
    for body in _bodies(_load(monkeypatch)).values():
        marked = [i for i, tool in enumerate(body['tools']) if 'cache_control' in tool]
        assert marked == [len(body['tools']) - 1]
        assert body['tools'][-1]['cache_control'] == _TTL_1H
        assert [block.get('cache_control') for block in body['system']] == [_TTL_1H]
        assert 'cache_control' not in json.dumps(body['messages'])


def test_uncacheable_prefixes_and_duplicate_arms_are_refused(monkeypatch):
    probe = _load(monkeypatch)
    with pytest.raises(ValueError, match='2048'):
        probe.build_arm_bodies([444], tools=12, model='m', prefix_tokens=2047, nonce='n')
    with pytest.raises(ValueError, match='duplicate'):
        probe.build_arm_bodies([444, 444], tools=12, model='m', prefix_tokens=2048,
                               nonce='n')


def test_dry_run_prints_the_plan_and_sends_nothing(tmp_path, monkeypatch, capsys):
    for name in _TRANSPORT_MODULES:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(socket.socket, 'connect', _forbidden)
    probe = _load(monkeypatch)
    monkeypatch.setattr(probe, 'time', types.SimpleNamespace(
        sleep=_forbidden, monotonic=time.monotonic))

    assert probe.main(['--config', _config(tmp_path)]) == 0

    out, err = capsys.readouterr()
    assert _KEY_FRAGMENT not in out + err
    assert 'index 0 of 2 (value hidden)' in out
    assert _BASE_URL in out
    for target in (132, 444, 552):
        assert re.search(rf'names={target}B\s+joined {target} B', out)
    assert 'DRY RUN' in out


@pytest.mark.parametrize(('extra', 'message'), [
    (['--face', 'openai'], 'only the Anthropic Messages protocol is supported'),
    (['--provider', 'nope'], "no provider with id 'nope'"),
    (['--key-index', '2'], 'out of range'),
    (['--prefix-tokens', '2047'], '2048'),
])
def test_refusals_are_clear_and_never_print_the_key(tmp_path, monkeypatch, capsys,
                                                    extra, message):
    probe = _load(monkeypatch)
    assert probe.main(['--config', _config(tmp_path), *extra]) == 2
    out, err = capsys.readouterr()
    assert message in err
    assert 'DRY RUN' not in out
    assert _KEY_FRAGMENT not in out + err


@pytest.mark.parametrize(('reads', 'errors', 'verdict'), [
    ([0, 9, 9], [None, None, None], 'REUSED'),
    ([0, 0, 0], [None, None, None], 'NOT REUSED'),
    ([0, 9, 0], [None, None, None], 'MIXED'),
    ([None, 0, 0], ['HTTP 429: busy', None, None], 'ERROR'),
    ([0, None, 0], [None, None, None], 'ERROR'),  # a missing field is unknown, not 0
])
def test_arm_verdict(monkeypatch, reads, errors, verdict):
    sends = [{'send': index + 1, 'read': read, 'error': error}
             for index, (read, error) in enumerate(zip(reads, errors, strict=True))]
    assert _load(monkeypatch).arm_verdict(sends) == verdict


class _Response:
    def __init__(self, status, lines=(), body=b''):
        self.status_code = status
        self.closed = False
        self._lines = list(lines)
        self._body = body

    def iter_lines(self):
        return iter(self._lines)

    def iter_content(self, chunk_size=1):
        return (self._body[i:i + chunk_size] for i in range(0, len(self._body), chunk_size))

    def close(self):
        self.closed = True


def _sse(*, read, write):
    usage = {'input_tokens': 7, 'cache_read_input_tokens': read,
             'cache_creation_input_tokens': write,
             'cache_creation': {'ephemeral_5m_input_tokens': 0,
                                'ephemeral_1h_input_tokens': write}}
    events = [{'type': 'message_start', 'message': {'usage': usage}},
              {'type': 'message_delta', 'usage': {'output_tokens': 2}},
              {'type': 'message_stop'},
              # Nothing after message_stop belongs to this send.
              {'type': 'message_start',
               'message': {'usage': {'cache_read_input_tokens': 999_999}}}]
    return [line for event in events for line in (
        f'event: {event["type"]}'.encode(), b'data: ' + json.dumps(event).encode(), b'')]


def _arm_bytes(body):
    return _joined(tool['name'] for tool in body['tools'])


@pytest.fixture
def fake_transport(monkeypatch):
    wire = types.SimpleNamespace(posts=[], responses=[], respond=None)

    class Session:
        def post(self, url, **kwargs):
            wire.posts.append((url, kwargs))
            response = wire.respond(json.loads(kwargs['data']))
            wire.responses.append(response)
            return response

    fakes = {
        'lib.llm._transport': {'get_sync_session': Session},
        'lib.llm.anthropic_outbound': {
            'anthropic_headers': lambda key, extra=None: {'x-api-key': key, **(extra or {})}},
        'lib.llm.anthropic_outbound._url': {
            'anthropic_messages_url': lambda base_url: base_url + '/v1/messages'},
        'lib.proxy': {'proxies_for': lambda url: dict(_PROXIES)},
    }
    for name, attrs in fakes.items():
        module = types.ModuleType(name)
        vars(module).update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    return wire


def test_armed_run_sends_interleaved_identical_requests_and_names_the_evidence(
        tmp_path, monkeypatch, capsys, fake_transport):
    seen = {}

    def respond(body):
        size = _arm_bytes(body)
        seen[size] = seen.get(size, 0) + 1
        if size == 552 or seen[size] == 1:
            return _Response(200, _sse(read=0, write=12_000 + size))
        return _Response(200, _sse(read=12_000 + size, write=0))

    fake_transport.respond = respond
    probe = _load(monkeypatch)
    sleeps = []
    monkeypatch.setattr(probe, 'time', types.SimpleNamespace(
        sleep=sleeps.append, monotonic=time.monotonic))

    assert probe.main(['--config', _config(tmp_path), '--arm']) == 0

    out, err = capsys.readouterr()
    posts = fake_transport.posts
    assert sleeps == [12.0, 12.0]
    assert [_arm_bytes(json.loads(kwargs['data'])) for _, kwargs in posts] == [132, 444, 552] * 3
    traces = [kwargs['headers']['M-TraceId'] for _, kwargs in posts]
    assert len(set(traces)) == 9
    assert all(re.fullmatch(r'[0-9a-f]{32}', trace) for trace in traces)
    for url, kwargs in posts:
        assert url == _BASE_URL + '/v1/messages'
        assert kwargs['headers']['anthropic-beta'] == 'extended-cache-ttl-2025-04-11'
        assert kwargs['headers']['x-api-key'] == _KEYS[0]
        assert (kwargs['stream'], kwargs['timeout'], kwargs['allow_redirects'],
                kwargs['proxies']) == (True, (10, 300), False, _PROXIES)
        assert kwargs['data'] == json.dumps(
            json.loads(kwargs['data']), ensure_ascii=False).encode('utf-8')
    for arm in range(3):
        assert len({posts[arm + 3 * send][1]['data'] for send in range(3)}) == 1
    assert all(response.closed for response in fake_transport.responses)

    assert 'read 12,444' in out
    assert '999,999' not in out
    assert re.search(r'names=132B\s+REUSED', out)
    assert re.search(r'names=444B\s+REUSED', out)
    assert re.search(r'names=552B\s+NOT REUSED', out)
    assert 'decides prompt-cache reuse' in out
    assert f'{traces[0]} -> {traces[3]}' in out  # REUSED names=132B, sends 1 -> 2
    assert f'{traces[2]} -> {traces[5]}' in out  # NOT REUSED names=552B
    assert _KEY_FRAGMENT not in out + err


def test_armed_failures_are_recorded_redacted_and_judged_as_errors(
        tmp_path, monkeypatch, capsys, fake_transport):
    seen = {}

    def respond(body):
        size = _arm_bytes(body)
        seen[size] = seen.get(size, 0) + 1
        if size == 444:
            raise ConnectionError(f'proxy refused {_KEYS[0]}')
        if size == 552:  # the echoed key straddles the 300-character cut
            return _Response(401, body=('y' * 280 + _KEYS[0] + ' rejected').encode())
        return _Response(200, _sse(read=0 if seen[size] == 1 else 9_000, write=9_000))

    fake_transport.respond = respond
    probe = _load(monkeypatch)
    args = ['--config', _config(tmp_path), '--arm', '--sends', '2', '--gap', '0']

    assert probe.main(args) == 1

    out, err = capsys.readouterr()
    assert len(fake_transport.posts) == 6
    assert 'ConnectionError: proxy refused <redacted>' in out
    assert 'HTTP 401: ' + 'y' * 280 + '<redacted> rejected' in out
    assert re.search(r'names=132B\s+REUSED', out)
    assert re.search(r'names=444B\s+ERROR', out)
    assert re.search(r'names=552B\s+ERROR', out)
    assert 'inconclusive' in out
    assert all(response.closed for response in fake_transport.responses)
    assert _KEY_FRAGMENT not in out + err
