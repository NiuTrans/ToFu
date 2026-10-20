"""tests/llm/test_llm_replay_scenarios.py — LLM record/replay scenario suite.

Scenarios replay recorded provider streams through the REAL root agent
loop (``run_root_agent_loop``), the REAL dispatch chain
(``stream_llm_response`` → ``dispatch_stream`` → slot), the REAL tool
pipeline, and REAL sidecar persistence.  Only the three LLM transport
functions are substituted (``stream_chat`` / ``async_stream_chat`` /
``chat``) — see ``tests/support/llm_replay/``.

Driver: PREFERRED depth from the subsystem spec — no adapter mocking.  The
request is a real ``RootLoopRequest`` whose collaborators all run; the task
is a real ``create_task`` product; dispatch resolves a real single-slot v2
route installed for the personal owner.  Scenario 4 deliberately stops at
dispatch depth (it drives ``async_dispatch_stream`` directly): the async
transport seam is what differs there, and the root loop itself is
transport-agnostic.

Fixtures are replayed by default (keyless, hermetic).  Modes:

* replay  — default; strict-sequence digest matching, drift fails loud;
* author  — ``TOFU_LLM_REPLAY_AUTHOR=1``: the committed ``script.jsonl``
  seed plays through ``ReplaySession(verify=False)`` while a ``Recorder``
  tees REAL request digests into ``script.next.jsonl`` (offline; the seed's
  events/results are the fixture source of truth);
* record  — ``TOFU_LLM_REPLAY=record`` + ``TOFU_PARITY_LIVE=1`` + a real
  key: the Recorder wraps the REAL transports instead (live re-record).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

from tests.support.llm_replay import (  # noqa: E402
    RECORD_OUTPUT_NAME,
    Recorder,
    ReplaySession,
    mode_from_env,
)

FIXTURES_ROOT = Path(__file__).parents[1] / 'fixtures' / 'llm_replay'
_OWNER_USER_ID = 1
_REPLAY_MODEL = 'llm-replay-model'
_REPLAY_MODEL_REF = {'creator_id': 'tofu-llm-replay',
                     'model_id': _REPLAY_MODEL}

_SYSTEM_PROMPT = (
    'You are Tofu, a coding assistant running an LLM replay scenario. '
    'Answer the user directly and use the available tools when asked.')


@pytest.fixture(scope='module', autouse=True)
def _dispatcher_teardown():
    """Leave no replay slots cached in the process-global dispatcher.

    The singleton invalidates lazily; without an explicit reset a later
    test on this xdist worker could inherit slots whose authority (this
    module's sidecar) no longer exists.
    """
    yield
    from lib.llm_dispatch import reset_dispatcher
    reset_dispatcher()


# ═════════════════════════════════════════════════════════════════════
#  Route / task / transport installation
# ═════════════════════════════════════════════════════════════════════

def _install_replay_route() -> None:
    """Install ONE deterministic single-slot v2 route for the personal owner.

    Mirrors ``tests/support/model_routing.py`` but carries exactly one
    connection/deployment (openai protocol) so dispatch can never rotate
    between wire shapes — replay digests depend on the adapted body, which
    is slot-shaped.
    """
    from lib.llm_dispatch import reset_dispatcher
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER_USER_ID)
    current = repository.get(boundary)
    installed = any(
        row.get('creator_id') == _REPLAY_MODEL_REF['creator_id']
        and row.get('model_id') == _REPLAY_MODEL_REF['model_id']
        for row in current.document['models'])
    if not installed:
        model_ref = dict(_REPLAY_MODEL_REF)
        repository.compare_and_swap(
            boundary,
            {
                'contract_version': 'tofu.model-routing/v2',
                'revision': current.revision,
                'creators': [{
                    'creator_id': model_ref['creator_id'],
                    'name': 'Tofu LLM replay scenarios',
                }],
                'models': [{
                    **model_ref,
                    'display_name': 'LLM replay model',
                    'capabilities': ['text'],
                    'context_window': 1_000_000,
                    'quality_rank': 1,
                }],
                'providers': [{
                    'provider_id': 'replay-provider',
                    'name': 'Replay provider',
                    'scope': 'owner',
                }],
                'provider_accesses': [{
                    'provider_access_id': 'replay-access',
                    'provider_id': 'replay-provider',
                    'enabled': True,
                    'quota_policy': {},
                }],
                'connections': [{
                    'connection_id': 'replay-connection',
                    'provider_access_id': 'replay-access',
                    # The transport is substituted before any socket exists;
                    # a public-domain URL avoids self-hosted reachability
                    # probes without a test-only routing branch.
                    'base_url': 'https://api.openai.com/v1',
                    'protocol': 'openai',
                    'enabled': True,
                    'priority': 0,
                    'extra_headers': {},
                }],
                'credentials': [{
                    'credential_id': 'replay-credential',
                    'provider_access_id': 'replay-access',
                    'kind': 'local_identity',
                    'secret_reference': '',
                    'key_hint': '',
                    'enabled': True,
                    'authorization': {
                        'connection_ids': ['replay-connection'],
                        'models': [dict(model_ref)],
                    },
                    'quota_policy': {},
                }],
                'offerings': [{
                    'offering_id': 'replay-offering',
                    'provider_access_id': 'replay-access',
                    'identity_state': 'confirmed',
                    'model': dict(model_ref),
                    'enabled': True,
                    'capabilities': ['text'],
                    'context_window': 1_000_000,
                    'priority': 0,
                }],
                'deployments': [{
                    'deployment_id': 'replay-deployment',
                    'offering_id': 'replay-offering',
                    'connection_id': 'replay-connection',
                    'wire_model_id': f'{_REPLAY_MODEL}-wire',
                    'enabled': True,
                    'identity_confidence': 'high',
                    'probe_status': 'passed',
                    'priority': 0,
                }],
            },
            expected_revision=current.revision,
        )
    # The model-registration admission boundary rejects callable models with
    # incomplete facts ("No slot available"); the shared synthetic-evidence
    # helper keeps the replay route installable instead of weakening the gate.
    from tests.support.model_registration import complete_registration
    boundary_document = repository.get(boundary)
    completed = complete_registration(boundary_document.document)
    if completed != boundary_document.document:
        repository.compare_and_swap(
            boundary, completed, expected_revision=boundary_document.revision)
    # The dispatcher singleton may have been built by an earlier test on this
    # xdist worker; force a slot rebuild from THIS route.
    reset_dispatcher()


def _install_transport(fixture_dir: Path, monkeypatch, cwd: str,
                       after_event=None, *, record_fixture: bool = True) -> str:
    """Install recorder (author/record modes) or replayer (default)."""
    script_path = fixture_dir / 'script.jsonl'
    if os.environ.get('TOFU_LLM_REPLAY_AUTHOR') == '1':
        seed = ReplaySession(script_path, cwd=cwd, verify=False,
                             after_event=after_event)
        if record_fixture:
            Recorder(fixture_dir / RECORD_OUTPUT_NAME, cwd=cwd).install(
                monkeypatch,
                stream_chat=seed.stream_chat,
                async_stream_chat=seed.async_stream_chat,
                chat=seed.chat)
        else:
            seed.install(monkeypatch)
        return 'author'
    if record_fixture and mode_from_env() == 'record':
        Recorder(fixture_dir / RECORD_OUTPUT_NAME, cwd=cwd).install(
            monkeypatch)
        return 'record'
    session = ReplaySession(script_path, cwd=cwd, after_event=after_event)
    session.install(monkeypatch)
    return 'replay'


def _make_task(conv_id: str, user_prompt: str,
               project_path: Path | None):
    """Seed one conversation and create one real registered chat task."""
    from tests._seed import seed_conversation
    from lib.tasks_pkg.manager import create_task
    seed_conversation(conv_id, messages=[
        {'role': 'user', 'content': user_prompt, 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '',
         'toolRounds': [], 'timestamp': 2},
    ], title=f'llm-replay-{conv_id[-8:]}')
    # Recorded requests own their tool surface. An earlier application import
    # can load deployment plugin defaults from .env into this test process.
    config = {'model': _REPLAY_MODEL, 'webSearchEnabled': False, 'plugins': []}
    if project_path is not None:
        config['projectPath'] = str(project_path)
        config['projectPaths'] = [str(project_path)]
    return create_task(
        conv_id,
        [{'role': 'user', 'content': user_prompt}],
        config,
        user_id=_OWNER_USER_ID,
    )


def _run_loop_scenario(name: str, *, tmp_path: Path, monkeypatch,
                       user_prompt: str, with_project: bool = False,
                       after_event=None,
                       prepare=None, record_fixture: bool = True) -> dict:
    """Drive one replayed scenario through the real root agent loop.

    Real prep helpers (model config resolution + tool assembly) build the
    request exactly the way ``run_task``'s prelude does; every loop
    collaborator runs for real.  Returns the observable run state for the
    scenario's assertions.
    """
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
    from lib.tasks_pkg.orchestrator._config_resolution import (
        resolve_and_seed_model_config,
    )
    from lib.tasks_pkg.orchestrator._post_loop import finalize_after_loop
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools,
    )

    fixture_dir = FIXTURES_ROOT / name
    _install_replay_route()
    conv_id = f'cv-lr-{name}-{uuid.uuid4().hex[:8]}'
    project_path = (tmp_path / 'project') if with_project else None
    if project_path is not None:
        project_path.mkdir(parents=True, exist_ok=True)
    task = _make_task(conv_id, user_prompt, project_path)

    mcfg = resolve_and_seed_model_config(task['config'], task)
    if mcfg['project_enabled']:
        # The real one-shot project startup: registers this conversation's
        # workspace roots so read/write tool paths resolve (run_task calls
        # this in its prelude; the loop driver mirrors it).
        from lib.tasks_pkg.orchestrator._startup import setup_project_context
        setup_project_context(
            task, task['config'], mcfg['project_path'],
            mcfg['project_enabled'])
    tool_list, _has_real_tools = assemble_round_tools(
        task['config'], task, mcfg)
    state = RoundState(
        model=mcfg['model'],
        preset=mcfg['preset'],
        thinking_enabled=bool(mcfg['thinking_enabled']),
    )
    seed_messages = [
        {'role': 'system', 'content': _SYSTEM_PROMPT},
        {'role': 'user', 'content': user_prompt},
    ]
    original_messages = list(seed_messages)
    messages = [dict(message) for message in seed_messages]
    request = root_loop.RootLoopRequest(
        task=task,
        state=state,
        messages=messages,
        tool_list=tool_list,
        all_search_results_text=[],
        cfg=task['config'],
        tid=task['id'][:8],
        thinking_depth=mcfg['thinking_depth'],
        temperature=mcfg['temperature'],
        max_tokens=4096,
        response_format=None,
        project_path=mcfg['project_path'] or None,
        project_enabled=bool(mcfg['project_enabled']),
        search_enabled=False,
    )
    mode = _install_transport(fixture_dir, monkeypatch, cwd=str(tmp_path),
                              after_event=after_event, record_fixture=record_fixture)
    if prepare is not None:
        prepare(task)
    loop_result = root_loop.run_root_agent_loop(request)
    # The terminal ROUND_END and DONE settlement are owned by run_task's
    # post-loop tail, not by the loop chassis — mirror the same call with
    # the same parameter mapping (lib/tasks_pkg/orchestrator/_run.py).
    finalize_after_loop(
        task,
        cfg=task['config'], tid=request.tid,
        model=state.model, preset=state.preset,
        thinking_depth=mcfg['thinking_depth'],
        thinking_enabled=state.thinking_enabled,
        temperature=mcfg['temperature'], max_tokens=4096,
        messages=messages, original_messages=original_messages,
        tool_list=tool_list, assistant_msg=state.assistant_msg,
        round_num=loop_result.last_round_num,
        accumulated_usage=state.accumulated_usage,
        api_rounds=state.api_rounds,
        last_finish_reason=state.last_finish_reason,
        last_usage=state.last_usage,
        last_stream_result=state.last_stream_result,
        tool_call_happened=state.tool_call_happened,
        all_search_results_text=[],
        project_path=request.project_path,
        project_enabled=request.project_enabled,
        loop_exit_reason=state.exit_reason,
        abort_detected_phase=state.abort_phase,
    )
    return {
        'task': task,
        'state': state,
        'result': loop_result,
        'messages': messages,
        'project_path': (Path(request.project_path)
                         if request.project_path else None),
        'mode': mode,
    }


def _expected(name: str) -> dict:
    with (FIXTURES_ROOT / name / 'expected.json').open(
            'r', encoding='utf-8') as handle:
        return json.load(handle)


def _durable_events(task) -> list[dict]:
    from lib.tasks_pkg.event_log import read_events
    rows = read_events(str(task['id']), raise_on_error=True)
    events = []
    for row in rows or []:
        payload = row.get('payload') if isinstance(row, dict) else None
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _assert_invariants_clean(task, *, allow_open_tail=False):
    from lib.tasks_pkg.event_invariants import run_task_log_invariants
    violations = run_task_log_invariants(
        str(task['id']), allow_open_tail=allow_open_tail)
    assert violations == (), (
        f'durable log invariant violations: '
        f'{[(v.rule, v.detail) for v in violations]}')


def _tool_result_messages(messages) -> list[dict]:
    return [message for message in messages
            if isinstance(message, dict) and message.get('role') == 'tool']


# ═════════════════════════════════════════════════════════════════════
#  1. text_reply — one stream step, plain answer
# ═════════════════════════════════════════════════════════════════════

def test_text_reply(tmp_path, monkeypatch):
    run = _run_loop_scenario(
        'text_reply', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Confirm the replay harness works.')
    expected = _expected('text_reply')
    task = run['task']
    state = run['state']
    for needle in expected['content_contains']:
        assert needle in task['content'], (
            f'final content missing {needle!r}: {task["content"]!r}')
    assert state.last_finish_reason == 'stop'
    assert run['mode'] in ('replay', 'author', 'record')
    _assert_invariants_clean(task)


# ═════════════════════════════════════════════════════════════════════
#  2. write_file_tool — streamed write_file call, REAL handler on disk
# ═════════════════════════════════════════════════════════════════════

def test_write_file_tool(tmp_path, monkeypatch):
    run = _run_loop_scenario(
        'write_file_tool', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Create the replay marker file, then confirm.',
        with_project=True)
    expected = _expected('write_file_tool')
    task = run['task']
    project_path = run['project_path']
    assert project_path is not None
    # The REAL write_file handler ran against the tmp project root.
    marker = project_path / expected['file']['path']
    assert marker.is_file(), f'write_file produced no {marker}'
    assert marker.read_bytes() == expected['file']['content'].encode('utf-8')
    for needle in expected['content_contains']:
        assert needle in task['content'], (
            f'final content missing {needle!r}: {task["content"]!r}')
    tool_names = [
        call.get('function', {}).get('name')
        for message in run['messages']
        for call in (message.get('tool_calls') or [])
        if isinstance(call, dict)
    ]
    assert 'write_file' in tool_names
    _assert_invariants_clean(task)


def test_incomplete_tool_stream_retries_before_any_write(tmp_path, monkeypatch):
    from lib.tasks_pkg.stream_handler import _budget
    monkeypatch.setenv('TOFU_DEFAULT_TOOL_PLUGINS', '*')
    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *args: None)
    run = _run_loop_scenario(
        'incomplete_tool_stream', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Create the replay marker file, then confirm.',
        with_project=True)
    expected = _expected('incomplete_tool_stream')
    assert not (run['project_path'] / 'unverified-write.txt').exists()
    assert (run['project_path'] / expected['file']['path']).read_text() == expected['file']['content']
    task = run['task']
    assert task['finishReason'] == 'stop'
    assert not task.get('error')
    assert expected['content_contains'][0] in task['content']
    calls = [call['id'] for message in run['messages']
             for call in message.get('tool_calls', [])]
    assert calls == ['call_write_1']
    assert len(_tool_result_messages(run['messages'])) == 1
    assert len(run['state'].api_rounds) == 3  # Includes the paid incomplete attempt.
    assert any(event.get('bucket') == 'incomplete_tool_stream'
               for event in _durable_events(task))
    completed = [event for event in _durable_events(task)
                 if event.get('type') == 'tool_complete'
                 and event.get('toolCallId') == 'call_unverified']
    assert len(completed) == 1
    assert completed[0]['status'] == 'aborted'
    assert completed[0]['toolContent'] == ''
    _assert_invariants_clean(task)


def test_incomplete_tool_stream_exhaustion_closes_announcements_and_round(tmp_path, monkeypatch):
    from lib.tasks_pkg.stream_handler import _budget
    def exhaust(task):
        task['_premature_retry_count_phase'] = _budget._PREMATURE_RETRY_MAX_CLASSIC
    run = _run_loop_scenario(
        'incomplete_tool_stream', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Create the replay marker file, then confirm.',
        with_project=True, prepare=exhaust, record_fixture=False)
    assert not (run['project_path'] / 'unverified-write.txt').exists()
    assert not (run['project_path'] / 'replay-marker.txt').exists()
    assert run['task']['finishReason'] == 'premature_close'
    assert run['task']['error']['kind'] == 'premature_close'
    assert len(run['state'].api_rounds) == 1
    assert not _tool_result_messages(run['messages'])
    _assert_invariants_clean(run['task'])


def test_derived_authoring_preserves_the_canonical_generation(tmp_path, monkeypatch):
    """A derived prefix may run, but must not append to its owner's fixture."""
    import lib.llm

    fixture_dir = tmp_path / 'scenario'
    fixture_dir.mkdir()
    (fixture_dir / 'script.jsonl').write_bytes(
        (FIXTURES_ROOT / 'text_reply' / 'script.jsonl').read_bytes())
    monkeypatch.setenv('TOFU_LLM_REPLAY_AUTHOR', '1')
    request = {'model': 'fixture', 'messages': [], 'tools': []}
    _install_transport(fixture_dir, monkeypatch, cwd=str(tmp_path))
    original = lib.llm.stream_chat(request)
    generated = (fixture_dir / RECORD_OUTPUT_NAME).read_bytes()

    _install_transport(fixture_dir, monkeypatch, cwd=str(tmp_path), record_fixture=False)
    derived = lib.llm.stream_chat(request)
    assert derived.message == original.message
    assert (fixture_dir / RECORD_OUTPUT_NAME).read_bytes() == generated


# ═════════════════════════════════════════════════════════════════════
#  3. rate_limit_fallback — pre-chunk 429, dispatch retry, then success
# ═════════════════════════════════════════════════════════════════════

def test_rate_limit_fallback(tmp_path, monkeypatch):
    run = _run_loop_scenario(
        'rate_limit_fallback', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Answer after the rate limit clears.')
    expected = _expected('rate_limit_fallback')
    task = run['task']
    for needle in expected['content_contains']:
        assert needle in task['content'], (
            f'final content missing {needle!r}: {task["content"]!r}')
    # A dispatch retry/fallback phase was observed on the durable stream:
    # the 429 slot-rotation callback emitted at least one retry phase frame.
    events = _durable_events(task)
    retry_events = [
        event for event in events
        if event.get('type') == 'phase'
        and ('retry' in str(event.get('phase') or '').lower()
             or 'retry' in str(event.get('detailKey') or '').lower())
    ]
    assert retry_events, (
        'no dispatch retry phase event found in durable events: '
        f'{[e.get("type") for e in events]}')
    _assert_invariants_clean(task)



# ═════════════════════════════════════════════════════════════════════
#  4. async_path — async_dispatch_stream with the async replayer
# ═════════════════════════════════════════════════════════════════════

def test_async_path(tmp_path, monkeypatch):
    """Dispatch-level depth (per spec): the async transport seam is what
    differs here; the root loop itself is transport-agnostic, and the sync
    scenarios above already prove loop depth.  The dispatcher is the REAL
    singleton built from the installed single-slot route — only
    ``async_stream_chat`` is substituted."""
    import asyncio
    fixture_dir = FIXTURES_ROOT / 'async_path'
    _install_replay_route()
    mode = _install_transport(fixture_dir, monkeypatch, cwd=str(tmp_path))
    from lib.llm_dispatch import api as dispatch_api
    chunks: list[str] = []

    async def _drive():
        return await dispatch_api.async_dispatch_stream(
            [{'role': 'user', 'content': 'Stream the async replay answer.'}],
            on_content=chunks.append,
            prefer_model=_REPLAY_MODEL,
            strict_model=True,
            log_prefix='[replay-async]')

    loop = asyncio.new_event_loop()
    try:
        msg, finish, usage = loop.run_until_complete(_drive())
    finally:
        loop.close()
    expected = _expected('async_path')
    aggregated = msg if isinstance(msg, str) else (msg or {}).get('content', '')
    for needle in expected['content_contains']:
        assert needle in aggregated, (
            f'async result missing {needle!r}: {aggregated!r}')
        assert needle in ''.join(chunks)
    assert finish == 'stop'
    assert mode in ('replay', 'author', 'record')


# ═════════════════════════════════════════════════════════════════════
#  5. parallel_tools_timeout — per-tool declared timeout in the pool
# ═════════════════════════════════════════════════════════════════════

def test_parallel_tools_timeout(tmp_path, monkeypatch):
    """Two read_files calls in one batch; the slow one exceeds its declared
    timeout and is replaced by the canonical sentinel, the fast one settles.

    The fixture deliberately carries the two calls ONLY in the terminal
    message (no ``tool_call`` stream events): a streamed read_files call is
    pre-executed by the StreamingToolAccumulator, whose fast path bypasses
    the pool's declared-timeout arming (and caches its result as
    authoritative).  The documented per-tool timeout seam under test lives
    in ``_execute_tool_one_in_pool``.
    """
    import time

    project_path = tmp_path / 'project'
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / 'slow.dat').write_text('SLOW-FILE-BODY', encoding='utf-8')
    (project_path / 'fast.dat').write_text('FAST-FILE-BODY', encoding='utf-8')
    (project_path / 'notes.dat').write_text('NOTES-FILE-BODY',
                                           encoding='utf-8')

    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat_mod
    real_execute_one = heartbeat_mod._execute_tool_one

    def slowed_execute_one(task, tc, fn_name, tc_id, fn_args, rn,
                           round_entry, cfg, pp, pe, all_tools=None):
        if fn_name == 'read_files' and 'slow.dat' in str(fn_args):
            time.sleep(1.0)
        return real_execute_one(task, tc, fn_name, tc_id, fn_args, rn,
                                round_entry, cfg, pp, pe,
                                all_tools=all_tools)

    monkeypatch.setattr(heartbeat_mod, '_execute_tool_one',
                        slowed_execute_one)
    monkeypatch.setattr(
        'lib.tools.registry.declared_tool_timeout_ms',
        lambda tool_name: 200 if tool_name == 'read_files' else None)

    run = _run_loop_scenario(
        'parallel_tools_timeout', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Read both replay data files, then summarize.',
        with_project=True)
    expected = _expected('parallel_tools_timeout')
    task = run['task']
    tool_messages = {
        message.get('tool_call_id'): str(message.get('content') or '')
        for message in _tool_result_messages(run['messages'])
    }
    slow_content = tool_messages.get(expected['slow_call_id'], '')
    assert expected['timeout_sentinel'] in slow_content, (
        f'slow call did not settle as the timeout sentinel: {slow_content!r}')
    # The model receives typed recovery instructions for a declared read-only
    # timeout; retaining only the old summary loses the replay decision.
    slow_failure = json.loads(slow_content)
    assert slow_failure['code'] == 'tool_execution_timeout'
    assert slow_failure['retryable'] is True
    assert 'normal tool gateway and authorization checks' in slow_failure['nextAction']
    fast_content = tool_messages.get(expected['fast_call_id'], '')
    assert 'FAST-FILE-BODY' in fast_content, (
        f'fast call lost its real read result: {fast_content!r}')
    # The durable projection: slow call completed with status=error, the
    # fast one clean.
    events = _durable_events(task)
    completes = {
        event.get('toolCallId'): event
        for event in events if event.get('type') == 'tool_complete'
    }
    slow_event = completes.get(expected['slow_call_id'])
    assert slow_event is not None, 'slow call left no tool_complete event'
    assert slow_event.get('status') == 'error'
    fast_event = completes.get(expected['fast_call_id'])
    assert fast_event is not None, 'fast call left no tool_complete event'
    assert fast_event.get('status') in (None, 'done')
    for needle in expected['content_contains']:
        assert needle in task['content'], (
            f'final content missing {needle!r}: {task["content"]!r}')
    _assert_invariants_clean(task)


# ═════════════════════════════════════════════════════════════════════
#  6. abort_mid_stream — owner cancel lands between recorded chunks
# ═════════════════════════════════════════════════════════════════════

def test_abort_mid_stream(tmp_path, monkeypatch):
    """The driver trips the owner-cancel seam (``task['aborted']``) from the
    replay session's ``after_event`` hook right after the first content
    chunk; the replayer mirrors the real stream's in-loop abort check and
    settles the attempt ``client_aborted`` — no fabricated success."""
    holder: dict = {}

    def trip_after_first_content(step_index, event):
        # ``prepare`` arms the holder before the loop starts, so this fires
        # DURING the streamed first step, between recorded chunks.
        task = holder.get('task')
        if task is not None and event.get('t') == 'content':
            task['aborted'] = True

    def prepare(task):
        holder['task'] = task

    run = _run_loop_scenario(
        'abort_mid_stream', tmp_path=tmp_path, monkeypatch=monkeypatch,
        user_prompt='Stream a long answer that will be stopped.',
        after_event=trip_after_first_content,
        prepare=prepare)
    expected = _expected('abort_mid_stream')
    task = run['task']
    assert task.get('aborted') is True, 'owner cancel never landed'
    outcome = run['result'].outcome
    assert outcome.aborted, (
        f'loop did not settle aborted: {outcome!r}')
    # No fabricated success: the final message never claims the full
    # recorded answer, and the loop never marked the turn completed.
    assert not outcome.completed
    for needle in expected.get('content_excludes', []):
        assert needle not in task['content'], (
            f'aborted stream fabricated content {needle!r}: '
            f'{task["content"]!r}')
    _assert_invariants_clean(task, allow_open_tail=True)
