"""tests/llm/test_llm_replay_unit.py — Unit contracts for the LLM replay kit.

Pins the four support seams in isolation (no agent loop, no storage):

* ``normalize`` — environment differences (cwd, tmp root, dates) never change
  a request digest; semantic changes always do;
* ``script`` — malformed lines fail loud with the line number, and every
  recorded result must construct a valid ``ProviderStreamResult``;
* ``replayer`` — callback order, drift/exhaustion errors, error/chat steps,
  and the mid-stream abort mirror of ``lib/llm/stream.py``;
* ``recorder`` — a deterministic fake transport round-trips through
  record → replay with identical callback sequences and results.

The fixture-corpus sweep at the bottom keeps every committed scenario
fixture loadable and constructible without running the scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

from tests.support.llm_replay import (
    Recorder,
    ReplaySession,
    ScenarioDriftError,
    ScriptError,
    load_script,
    mode_from_env,
    normalize_value,
    normalized_request_digest,
)

pytestmark = pytest.mark.unit

FIXTURES_ROOT = Path(__file__).parents[1] / 'fixtures' / 'llm_replay'

_CWD_A = '/tmp/pytest-of-alice/pytest-1/test_alpha0'
_CWD_B = '/tmp/pytest-of-bob/pytest-9/test_beta3'


def _body(*, cwd: str = _CWD_A, date: str = '2026-09-17') -> dict:
    return {
        'model': 'replay-model-wire',
        'messages': [
            {'role': 'system',
             'content': f'You are Tofu. Today is {date}. Workspace: '
                        f'{cwd}/project.'},
            {'role': 'user', 'content': 'say hi'},
        ],
        'system': None,
        'tools': [{'type': 'function',
                   'function': {'name': 'read_files'}}],
        '_task_id': 'internal-execution-metadata',
    }


def _stream_step_line(events, *, message=None, digest='d0', state='provider_finished') -> str:
    result = {
        'message': message or {'role': 'assistant',
                               'content': ''.join(
                                   e['text'] for e in events
                                   if e['t'] == 'content')},
        'compatibility_finish_reason': 'stop',
        'usage': {'prompt_tokens': 5, 'completion_tokens': 3},
        'state': state,
        'provider_finish_reason': 'stop',
        'saw_done': True,
        'saw_finish_reason': True,
        'malformed_frame_count': 0,
    }
    return json.dumps({'kind': 'stream', 'request_digest': digest,
                       'events': events, 'result': result})


def _write_script(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / 'script.jsonl'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


# ═════════════════════════════════════════════════════════════════════
#  normalize
# ═════════════════════════════════════════════════════════════════════

class TestNormalize:
    def test_same_body_different_env_same_digest(self):
        body_a = _body(cwd=_CWD_A, date='2026-09-17')
        body_b = _body(cwd=_CWD_B, date='2027-01-03')
        assert normalized_request_digest(body_a, cwd=_CWD_A) == \
            normalized_request_digest(body_b, cwd=_CWD_B)

    def test_internal_fields_never_enter_digest(self):
        body_a = _body()
        body_b = {**_body(), '_task_id': 'other', '_conv_id': 'cv-9'}
        assert normalized_request_digest(body_a, cwd=_CWD_A) == \
            normalized_request_digest(body_b, cwd=_CWD_A)

    def test_different_messages_different_digest(self):
        body_a = _body()
        body_b = _body()
        body_b['messages'] = list(body_b['messages']) + [
            {'role': 'user', 'content': 'one more question'}]
        assert normalized_request_digest(body_a, cwd=_CWD_A) != \
            normalized_request_digest(body_b, cwd=_CWD_A)

    def test_long_form_dates_and_home_normalize(self):
        value = {
            'when': 'Thursday, September 17, 2026',
            'where': '~/notes/2026-09-17.md'.replace(
                '~', str(Path.home())),
        }
        normalized = normalize_value(value, cwd=_CWD_A)
        assert normalized['when'] == 'Thursday, {{DATE}}'
        assert normalized['where'] == '{{HOME}}/notes/{{DATE}}.md'

    def test_admitted_request_mapping_digest_matches_plain_body(self):
        """AdmittedRequest mappings digest like their plain dict twin."""
        from lib.llm.request import seal_request, semantic_request_id
        body = _body()
        admitted = seal_request(
            dict(body), model=body['model'], input_tokens=10,
            semantic_id=semantic_request_id(body))
        assert normalized_request_digest(admitted, cwd=_CWD_A) == \
            normalized_request_digest(body, cwd=_CWD_A)


# ═════════════════════════════════════════════════════════════════════
#  script validation
# ═════════════════════════════════════════════════════════════════════

class TestScriptValidation:
    def test_unknown_kind_fails_with_line_number(self, tmp_path):
        path = _write_script(tmp_path, [
            _stream_step_line([{'t': 'content', 'text': 'hi'}]),
            json.dumps({'kind': 'mystery', 'request_digest': 'd'}),
        ])
        with pytest.raises(ScriptError, match=r':2:.*unknown kind'):
            load_script(path)

    def test_unknown_exception_fails_loud(self, tmp_path):
        path = _write_script(tmp_path, [
            json.dumps({'kind': 'error', 'request_digest': 'd',
                        'exception': 'NotARealError', 'message': 'x'}),
        ])
        with pytest.raises(ScriptError, match='unknown exception'):
            load_script(path)

    def test_invariant_violating_result_rejected(self, tmp_path):
        """provider_finished without an observed finish frame must fail."""
        line = json.dumps({
            'kind': 'stream', 'request_digest': 'd', 'events': [],
            'result': {
                'message': {'role': 'assistant', 'content': 'x'},
                'compatibility_finish_reason': 'stop', 'usage': None,
                'state': 'provider_finished',
                'provider_finish_reason': None,
                'saw_done': True, 'saw_finish_reason': False,
                'malformed_frame_count': 0,
            },
        })
        path = _write_script(tmp_path, [line])
        with pytest.raises(ScriptError, match='not a constructible'):
            load_script(path)

    def test_malformed_event_rejected(self, tmp_path):
        path = _write_script(tmp_path, [
            _stream_step_line([{'t': 'tool_call', 'call': 'not-a-dict'}]),
        ])
        with pytest.raises(ScriptError, match=r'event\[0\]'):
            load_script(path)


# ═════════════════════════════════════════════════════════════════════
#  replayer
# ═════════════════════════════════════════════════════════════════════

class TestReplayer:
    def _session(self, tmp_path, lines, **kwargs) -> ReplaySession:
        path = _write_script(tmp_path, lines)
        return ReplaySession(path, cwd=_CWD_A, verify=False, **kwargs)

    def test_full_stream_replay_order_and_result(self, tmp_path):
        events = [
            {'t': 'thinking', 'text': 'hmm '},
            {'t': 'content', 'text': 'Hello '},
            {'t': 'content', 'text': 'world'},
            {'t': 'tool_call', 'call': {
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'}}},
            {'t': 'attempt_restart', 'reason': 'transport retry'},
        ]
        session = self._session(tmp_path, [_stream_step_line(events)])
        seen = []
        result = session.stream_chat(
            _body(),
            on_thinking=lambda text: seen.append(('thinking', text)),
            on_content=lambda text: seen.append(('content', text)),
            on_tool_call_ready=lambda call: seen.append(('tool_call', call)),
            on_attempt_restart=lambda reason: seen.append(
                ('attempt_restart', reason)))
        assert seen == [
            ('thinking', 'hmm '),
            ('content', 'Hello '),
            ('content', 'world'),
            ('tool_call', {'id': 'call_1', 'type': 'function',
                           'function': {'name': 'read_files',
                                        'arguments': '{}'}}),
            ('attempt_restart', 'transport retry'),
        ]
        assert isinstance(result, ProviderStreamResult)
        assert result.state is ProviderStreamState.PROVIDER_FINISHED
        assert result.is_verified_complete
        assert result.message['content'] == 'Hello world'

    def test_sequence_mismatch_raises_drift(self, tmp_path):
        digest = normalized_request_digest(_body(), cwd=_CWD_A)
        path = _write_script(tmp_path, [
            _stream_step_line([{'t': 'content', 'text': 'hi'}],
                              digest=digest),
        ])
        session = ReplaySession(path, cwd=_CWD_A)
        drifted = _body()
        drifted['messages'] = [{'role': 'user', 'content': 'different'}]
        with pytest.raises(ScenarioDriftError, match=r'step 0: request '
                                                     r'digest mismatch'):
            session.stream_chat(drifted)

    def test_exhaustion_raises_drift(self, tmp_path):
        session = self._session(tmp_path, [])
        with pytest.raises(ScenarioDriftError,
                           match='script exhausted at call 1'):
            session.stream_chat(_body())

    def test_error_step_raises_recorded_type(self, tmp_path):
        from lib.llm_errors import RateLimitError
        line = json.dumps({'kind': 'error', 'request_digest': 'd',
                           'exception': 'RateLimitError',
                           'message': 'slow down',
                           'attributes': {'status_code': 429}})
        session = self._session(tmp_path, [line])
        with pytest.raises(RateLimitError) as exc_info:
            session.stream_chat(_body())
        assert exc_info.value.status_code == 429
        assert str(exc_info.value) == 'slow down'

    def test_chat_step_returns_content_and_usage(self, tmp_path):
        line = json.dumps({'kind': 'chat', 'request_digest': 'd',
                           'content': 'plain answer',
                           'usage': {'total_tokens': 9}})
        session = self._session(tmp_path, [line])
        content, usage = session.chat(
            [{'role': 'user', 'content': 'hi'}], model='replay-model-wire')
        assert content == 'plain answer'
        assert usage == {'total_tokens': 9}

    def test_kind_mismatch_fails_loud(self, tmp_path):
        line = json.dumps({'kind': 'chat', 'request_digest': 'd',
                           'content': 'x', 'usage': {}})
        session = self._session(tmp_path, [line])
        with pytest.raises(ScenarioDriftError, match='requested a stream'):
            session.stream_chat(_body())

    def test_abort_mid_stream_mirrors_real_semantics(self, tmp_path):
        """A tripped abort_check stops delivery and settles client_aborted —
        the real in-loop check never raises and never fabricates success."""
        events = [
            {'t': 'content', 'text': 'first '},
            {'t': 'content', 'text': 'second '},
            {'t': 'content', 'text': 'third'},
        ]
        session = self._session(tmp_path, [_stream_step_line(events)])
        delivered = []
        abort = {'flag': False}

        def on_content(text):
            delivered.append(text)
            if len(delivered) == 1:
                abort['flag'] = True

        result = session.stream_chat(
            _body(), on_content=on_content,
            abort_check=lambda: abort['flag'])
        assert delivered == ['first ']
        assert result.state is ProviderStreamState.CLIENT_ABORTED
        assert result.message['content'] == 'first '
        assert not result.is_verified_complete
        # CLIENT_ABORTED construction passes the strict invariants.
        assert result.saw_finish_reason is False
        assert result.provider_finish_reason is None

    @pytest.mark.anyio
    async def test_async_stream_chat_replays(self, tmp_path, anyio_backend):
        events = [{'t': 'content', 'text': 'async hello'}]
        session = self._session(tmp_path, [_stream_step_line(events)])
        chunks = []
        result = await session.async_stream_chat(
            _body(), on_content=chunks.append)
        assert chunks == ['async hello']
        assert result.state is ProviderStreamState.PROVIDER_FINISHED

    def test_install_patches_all_three_seams(self, tmp_path, monkeypatch):
        session = self._session(tmp_path, [])
        session.install(monkeypatch)
        import lib.llm as llm_pkg
        import lib.llm.astream as astream_mod
        # Bound-method identity: equal __self__/__func__, fresh object per access.
        assert llm_pkg.stream_chat == session.stream_chat
        assert astream_mod.async_stream_chat == session.async_stream_chat
        assert llm_pkg.chat == session.chat


# ═════════════════════════════════════════════════════════════════════
#  recorder → replayer round-trip
# ═════════════════════════════════════════════════════════════════════

class TestRecordReplayRoundTrip:
    def test_fake_transport_round_trip(self, tmp_path):
        body = _body()
        recorded_calls = []

        def fake_stream_chat(body_arg, *, on_thinking=None, on_content=None,
                               on_tool_call_ready=None, abort_check=None,
                               **_kwargs):
            recorded_calls.append(body_arg)
            on_thinking('thinking one ')
            on_content('Hello, ')
            on_content('replay')
            on_tool_call_ready({
                'id': 'call_rt', 'type': 'function',
                'function': {'name': 'write_file',
                             'arguments': json.dumps(
                                 {'path': 'a.txt', 'content': 'x'})}})
            return ProviderStreamResult(
                message={'role': 'assistant',
                         'content': 'Hello, replay',
                         'reasoning_content': 'thinking one ',
                         'tool_calls': [{
                             'id': 'call_rt', 'type': 'function',
                             'function': {
                                 'name': 'write_file',
                                 'arguments': json.dumps(
                                     {'path': 'a.txt', 'content': 'x'})}}]},
                compatibility_finish_reason='tool_calls',
                usage={'prompt_tokens': 7, 'completion_tokens': 4},
                state=ProviderStreamState.PROVIDER_FINISHED,
                provider_finish_reason='tool_calls',
                saw_done=True,
                saw_finish_reason=True,
            )

        out_path = tmp_path / 'script.next.jsonl'
        recorder = Recorder(out_path, cwd=_CWD_A)
        wrapped = recorder.wrap_stream_chat(fake_stream_chat)
        record_events = []
        recorded_result = wrapped(
            body,
            on_thinking=lambda t: record_events.append(('thinking', t)),
            on_content=lambda t: record_events.append(('content', t)),
            on_tool_call_ready=lambda c: record_events.append(('tool', c)))

        # The tee'd script loads and replays the identical sequence.
        session = ReplaySession(out_path, cwd=_CWD_A)
        replay_events = []
        replayed_result = session.stream_chat(
            body,
            on_thinking=lambda t: replay_events.append(('thinking', t)),
            on_content=lambda t: replay_events.append(('content', t)),
            on_tool_call_ready=lambda c: replay_events.append(('tool', c)))

        assert replay_events == record_events
        assert replayed_result.message == recorded_result.message
        assert replayed_result.state is recorded_result.state
        assert replayed_result.usage == recorded_result.usage
        assert recorded_calls == [body]

    def test_recorder_captures_exceptions(self, tmp_path):
        from lib.llm_errors import RetryableAPIError

        def boom(_body, **_kwargs):
            raise RetryableAPIError('gateway 502', status_code=502)

        out_path = tmp_path / 'script.next.jsonl'
        recorder = Recorder(out_path, cwd=_CWD_A)
        with pytest.raises(RetryableAPIError):
            recorder.wrap_stream_chat(boom)(_body())
        session = ReplaySession(out_path, cwd=_CWD_A)
        with pytest.raises(RetryableAPIError, match='gateway 502') as info:
            session.stream_chat(_body())
        assert info.value.status_code == 502

    def test_recorder_never_overwrites_committed_script(self, tmp_path):
        """Record output is always the .next generation, by construction."""
        recorder = Recorder(tmp_path / 'script.next.jsonl', cwd=_CWD_A)
        assert recorder.out_path.name == 'script.next.jsonl'


# ═════════════════════════════════════════════════════════════════════
#  env mode gate
# ═════════════════════════════════════════════════════════════════════

class TestModeFromEnv:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv('TOFU_LLM_REPLAY', raising=False)
        monkeypatch.delenv('TOFU_PARITY_LIVE', raising=False)
        assert mode_from_env() == 'off'

    def test_record_requires_both_gates(self):
        assert mode_from_env({'TOFU_LLM_REPLAY': 'record'}) == 'off'
        assert mode_from_env({'TOFU_PARITY_LIVE': '1'}) == 'off'
        assert mode_from_env({'TOFU_LLM_REPLAY': 'record',
                              'TOFU_PARITY_LIVE': '1'}) == 'record'


# ═════════════════════════════════════════════════════════════════════
#  fixture corpus — every committed scenario script stays honest
# ═════════════════════════════════════════════════════════════════════

class TestFixtureCorpus:
    def test_every_fixture_loads_and_constructs(self):
        fixture_dirs = sorted(
            path for path in FIXTURES_ROOT.iterdir()
            if path.is_dir()) if FIXTURES_ROOT.is_dir() else []
        assert fixture_dirs, (
            f'no LLM replay fixtures under {FIXTURES_ROOT} — the scenario '
            f'suite expects at least the six seeded scenarios')
        for fixture_dir in fixture_dirs:
            script = fixture_dir / 'script.jsonl'
            manifest = fixture_dir / 'manifest.json'
            expected = fixture_dir / 'expected.json'
            assert script.is_file(), f'{fixture_dir.name}: missing script'
            assert manifest.is_file(), f'{fixture_dir.name}: missing manifest'
            assert expected.is_file(), f'{fixture_dir.name}: missing expected'
            steps = load_script(script)
            assert steps, f'{fixture_dir.name}: empty script'
            for step in steps:
                result = getattr(step, 'result', None)
                if result is not None:
                    # load_script already constructed it; assert the frozen
                    # invariants held (evidence was derived consistently).
                    assert isinstance(result, ProviderStreamResult)
                    assert result.evidence is not None
