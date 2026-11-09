"""Headless request routes never hand work to a lane that outlives the response.

Native ``/api/v1/chat/completions``, ``/api/v1/agent/run`` and the
OpenAI/Anthropic-compatible agent routes run a task under a client job label
(``api-…``, ``agent-…``, ``compat-…``) that never becomes a saved conversation.
An explicit background command, timer continuation or ``current``-conversation
agent schedule started there could only publish into that missing
conversation, so the caller would lose the result. A long foreground command
still moves to the background after ``AUTO_BACKGROUND_AFTER_S``, inside the
run: its result reaches the next model round or ``poll``, and it is stopped
when the response ends. The real routes, task runner, tool pipeline and
handlers run here; only the provider transport is scripted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import threading

import pytest
from quart import Quart

from lib.llm.stream_result import ProviderStreamResult
from tests.support.model_routing import (
    allow_native_test_endpoint,
    clear_test_model_routing,
    native_test_model,
    reset_native_test_model_route,
)

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api

OWNER = 1
NO_CONVERSATION = 'no saved conversation'


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            # The HTTP response can precede physical task retirement. Keep
            # storage fixtures and the loop live until executor work returns.
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


class ScriptedProvider:
    """Answer each model request with the next scripted turn.

    A turn is either final text or tool calls, or a callable that computes one
    from the requests recorded so far. Each request's messages and offered
    tools are recorded as JSON so assertions see exactly what the model was
    told, including earlier tool results.
    """

    def __init__(self, *turns):
        self._turns = list(turns)
        self._lock = threading.Lock()
        self.requests: list[dict] = []

    def stream_chat(self, body, *, on_content=None, **_kwargs):
        with self._lock:
            self.requests.append(json.loads(json.dumps(
                {'messages': body.get('messages') or [],
                 'tools': body.get('tools') or [],
                 # Tool Search and exact-name calls admit from this catalog.
                 'catalog': [tool['function']['name']
                             for tool in body.get('_executable_tool_catalog') or []
                             if tool.get('function')]}, default=str)))
            turn = self._turns.pop(0) if self._turns else {'content': 'done'}
        if callable(turn):
            turn = turn(self)
        if 'tool_calls' in turn:
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': None, 'tool_calls': turn['tool_calls']},
                'tool_calls', {'prompt_tokens': 10, 'completion_tokens': 5})
        if on_content is not None:
            on_content(turn['content'])
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': turn['content']},
            'stop', {'prompt_tokens': 12, 'completion_tokens': 3})

    async def async_stream_chat(self, body, **kwargs):
        return self.stream_chat(body, **kwargs)

    def chat(self, *_args, **_kwargs):
        return '', {}

    def install(self, monkeypatch):
        import importlib
        monkeypatch.setattr('lib.llm.stream_chat', self.stream_chat)
        monkeypatch.setattr(importlib.import_module('lib.llm.astream'),
                            'async_stream_chat', self.async_stream_chat)
        monkeypatch.setattr(importlib.import_module('lib.llm.chat'), 'chat', self.chat)
        monkeypatch.setattr('lib.llm.chat', self.chat)
        return self

    def offered(self, index=0) -> dict[str, dict]:
        return {tool['function']['name']: tool['function']
                for tool in self.requests[index]['tools'] if tool.get('function')}

    def catalog(self, index=0) -> set[str]:
        return set(self.requests[index]['catalog'])

    def tool_results(self, index=1) -> list[str]:
        return [str(message.get('content') or '')
                for message in self.requests[index]['messages']
                if message.get('role') == 'tool']


def _call(name, arguments, call_id='call-headless-1'):
    return {'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}]}


def _handle(receipt: str) -> str:
    match = re.search(r'\[Command moved to background: (bg_[0-9a-f]+)\.', receipt)
    assert match, receipt
    return match.group(1)


def _wait_until_finished(command_id):
    """Wait on actual completion; the enclosing test hang guard owns liveness."""
    from lib.tasks_pkg import background_commands
    info = background_commands.check_background_command(
        command_id, user_id=OWNER, wait_s=background_commands.WAIT_BACKGROUND_COMMAND_MAX_S)
    assert info and info['status'] == 'completed', info
    return info


@pytest.fixture(scope='module')
def surface():
    from lib.api_keys import create_key
    from routes.api_v1.agent_run import api_v1_agent_run_bp
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.api_v1.chat import api_v1_chat_bp
    from routes.api_v1.chat_direct import api_v1_chat_direct_bp
    from routes.compat_anthropic import compat_anthropic_bp
    from routes.compat_openai import compat_openai_bp

    app = Quart(__name__, static_folder=None)
    app.config.update(TESTING=True, PROVIDE_AUTOMATIC_OPTIONS=True)
    app.before_request(bearer_auth_before_request)
    for blueprint in (api_v1_chat_bp, api_v1_agent_run_bp, api_v1_chat_direct_bp,
                      compat_openai_bp, compat_anthropic_bp):
        app.register_blueprint(blueprint)
    _, token = create_key(owner_user_id=OWNER, name='headless-background',
                          scopes=['chat', 'agents:run'])
    with allow_native_test_endpoint():
        yield app, {'Authorization': f'Bearer {token}'}
    clear_test_model_routing(owner_user_id=OWNER)


def _timer_ids():
    from lib.scheduler.timer import list_active_timers
    return {timer['id'] for timer in list_active_timers(user_id=OWNER)}


def _schedule_ids():
    from lib.scheduler.manager import get_scheduler
    return {task['id'] for task in get_scheduler().list_tasks(
        user_id=OWNER, include_disabled=True)}


class Runtime:
    def __init__(self, client, headers):
        self.client, self.headers = client, headers
        self.timer_loops: list[str] = []
        self.timers_before = _timer_ids()
        self.schedules_before = _schedule_ids()

    def new_timers(self):
        return _timer_ids() - self.timers_before

    def new_schedules(self):
        return _schedule_ids() - self.schedules_before


@pytest.fixture
def runtime(surface, monkeypatch, tmp_path):
    from lib.idempotency import _cache
    from lib.scheduler.manager import get_scheduler
    from lib.scheduler.timer import cancel_timer
    from lib.tasks_pkg import background_commands

    _cache.clear()
    reset_native_test_model_route(owner_user_id=OWNER)
    monkeypatch.setattr(background_commands, '_registry', {})
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path / 'records'))
    app, headers = surface
    state = Runtime(app.test_client(), headers)
    monkeypatch.setattr('lib.scheduler.timer.start_timer_loop',
                        lambda timer_id, **_kwargs: state.timer_loops.append(timer_id))
    yield state
    # A regression may leave durable watchers or jobs; never leak them into
    # the next test of this module-scoped authority.
    for timer_id in state.new_timers():
        cancel_timer(timer_id, user_id=OWNER)
    for schedule_id in state.new_schedules():
        get_scheduler().delete_task(schedule_id, user_id=OWNER)


def _commands():
    from lib.tasks_pkg import background_commands
    return background_commands.list_background_commands(user_id=OWNER)


TOOL_CONFIG = {'codeExecEnabled': True, 'searchMode': 'off', 'fetchEnabled': False,
               'mcpEnabled': False, 'browserEnabled': False, 'plugins': []}


def _command_config(mode, tmp_path):
    """Standalone ``run_command`` (code_exec handler) or the project handler."""
    if mode == 'code_exec':
        return dict(TOOL_CONFIG)
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    return dict(TOOL_CONFIG, codeExecEnabled=False, projectPath=str(project),
                projectPaths=[str(project)])


def _task_request(route, *, config=None, tools=None):
    """Return (path, body) for one blocking request on a task-backed route."""
    messages = [{'role': 'user', 'content': 'Run the requested command.'}]
    if route in ('chat', 'agent'):
        body = {'model': native_test_model(), 'messages': messages, 'timeout_s': 60,
                'config': dict(TOOL_CONFIG if config is None else config)}
        if tools is not None:
            body['tools'] = tools
        return ('/api/v1/chat/completions' if route == 'chat' else '/api/v1/agent/run'), body
    body = {'model': 'stub-model', 'tofu_creator_id': 'tofu-test',
            'max_tokens': 128, 'timeout_s': 60, 'messages': messages}
    return ('/v1/chat/completions' if route == 'openai' else '/v1/messages'), body


async def _post(runtime, path, body):
    response = await runtime.client.post(path, json=body, headers=runtime.headers)
    text = await response.get_data(as_text=True)
    assert response.status_code == 200, text
    return text


@pytest.fixture
def gated_handoff_command(tmp_path):
    """The real shell stays alive until this case releases its owned FIFO."""
    fifo = tmp_path / 'command-release'
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    released = False
    lock = threading.Lock()

    def release():
        nonlocal released
        with lock:
            if not released:
                os.write(descriptor, b'\n')
                released = True

    try:
        yield f'read -r _ < {shlex.quote(str(fifo))}; echo headless-handoff-done', release
    finally:
        release()
        os.close(descriptor)


@pytest.mark.parametrize('finish_order', ['poll', 'inbox'])
@pytest.mark.parametrize('mode', ['code_exec', 'project'])
@pytest.mark.parametrize('route', ['chat', 'agent'])
def test_long_command_hands_off_and_poll_collects_its_result(
        route, mode, finish_order, runtime, monkeypatch, tmp_path,
        gated_handoff_command):
    from lib.tasks_pkg.handlers import _background_command as handoff

    # Every subprocess is old enough for handoff, but its completion is owned
    # by the case, not by interpreter startup or round-persistence latency.
    monkeypatch.setattr(handoff, 'AUTO_BACKGROUND_AFTER_S', 0.0)
    command, release = gated_handoff_command
    if finish_order == 'inbox':
        from lib.tasks_pkg import background_commands
        take = background_commands.take_run_completions

        def complete_before_inbox(**kwargs):
            for row in background_commands.list_background_commands(**kwargs):
                release()
                _wait_until_finished(row['command_id'])
            return take(**kwargs)

        monkeypatch.setattr(background_commands, 'take_run_completions', complete_before_inbox)

    def poll_after_request_capture(seen):
        # ScriptedProvider recorded the pre-poll request before this callback.
        release()
        return _call('poll', {'id': _handle(seen.tool_results(1)[0]),
                              'wait_s': 60}, 'call-headless-2')

    provider = ScriptedProvider(
        _call('run_command', {'command': command}),
        poll_after_request_capture,
        {'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(route, config=_command_config(mode, tmp_path))))

    receipt, polled = provider.tool_results(2)
    assert 'reaches your next step' in receipt
    assert 'stopped' in receipt  # nothing started here outlives the response
    assert 'shown in the conversation' not in receipt
    assert 'headless-handoff-done' in polled
    # Poll suppresses future inbox delivery, not an already delivered original
    # in chronological history. Both schedules must have no post-poll addition.
    command_id = _handle(receipt)
    prefix = f'<background-command id="{command_id}" '

    def carriers(index):
        return [message['content'] for message in provider.requests[index]['messages']
                if isinstance(message.get('content'), str)
                and message['content'].startswith(prefix)]

    assert len(carriers(1)) == (1 if finish_order == 'inbox' else 0)
    assert carriers(2) == carriers(1)


@pytest.mark.parametrize('route', ['chat', 'agent'])
def test_finished_handoff_reaches_the_next_model_round(
        route, runtime, monkeypatch, tmp_path):
    from lib.tasks_pkg.handlers import _background_command as handoff

    monkeypatch.setattr(handoff, 'AUTO_BACKGROUND_AFTER_S', 0.0)

    def once_it_finished(seen):
        _wait_until_finished(_handle(seen.tool_results(1)[0]))
        return _call('poll', {}, 'call-headless-2')  # a snapshot collects nothing

    provider = ScriptedProvider(
        _call('run_command', {'command': 'sleep 1; echo headless-lane-done'}),
        once_it_finished,
        {'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(
        route, config=_command_config('code_exec', tmp_path))))

    told = json.dumps(provider.requests[2]['messages'])
    assert told.count('<background-command id=') == 1
    assert 'headless-lane-done' in told


def test_handed_off_command_is_stopped_when_the_response_ends(
        runtime, monkeypatch, tmp_path):
    from lib.tasks_pkg.handlers import _background_command as handoff

    monkeypatch.setattr(handoff, 'AUTO_BACKGROUND_AFTER_S', 0.0)
    marker = tmp_path / 'outlived-the-response'
    provider = ScriptedProvider(
        _call('run_command', {'command': f'sleep 60; touch {marker}'}),
        {'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request('chat', config=_command_config('code_exec', tmp_path))))

    # Settling long before the sleep could end proves the stop.
    _wait_until_finished(_handle(provider.tool_results(1)[0]))
    assert not marker.exists()


@pytest.mark.parametrize('route,surface_kind', [
    ('chat', 'code_exec'), ('agent', 'code_exec'),
    ('chat', 'project'), ('agent', 'project'),
    # Native caller-supplied schemas bypass registry assembly but still reach
    # the server's run_command handler.
    ('chat', 'caller_tools'),
])
def test_explicit_background_is_refused_before_anything_runs(
        route, surface_kind, runtime, monkeypatch, tmp_path):
    from lib.tools.code_exec import CODE_EXEC_TOOL

    marker = tmp_path / 'ran-in-background'
    provider = ScriptedProvider(
        _call('run_command', {'command': f'touch {marker}', 'background': True}),
        {'content': 'finished'}).install(monkeypatch)
    tools = [CODE_EXEC_TOOL] if surface_kind == 'caller_tools' else None
    config = _command_config('project' if surface_kind == 'project' else 'code_exec', tmp_path)
    _run(_post(runtime, *_task_request(route, config=config, tools=tools)))

    result, = provider.tool_results()
    assert NO_CONVERSATION in result
    assert 'without `background`' in result  # the next action
    assert '[Command moved to background:' not in result
    assert not marker.exists()
    assert _commands() == []


@pytest.mark.parametrize('mode', ['code_exec', 'project'])
@pytest.mark.parametrize('route', ['chat', 'agent'])
def test_command_tool_text_describes_delivery_within_the_request(
        route, mode, runtime, monkeypatch, tmp_path):
    provider = ScriptedProvider({'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(route, config=_command_config(mode, tmp_path))))

    offered = provider.offered()
    command = offered['run_command']
    background = command['parameters']['properties']['background']['description']
    text = command['description'] + background
    assert 'arrive automatically' not in text
    assert 'delivered automatically' not in text
    assert 'reaches your next step' in command['description']
    assert NO_CONVERSATION in background
    # A handed-off command's handle exists here; a sub-agent's cannot.
    control = json.dumps(offered['poll'])
    assert 'bg_*' in control and 'other conversations' in control
    assert 'delivered automatically' not in control
    assert 'swarm' not in control and 'spawn_agents' not in control
    stop = json.dumps(offered['kill'])
    assert 'bg_*' in stop and 'swarm' not in stop


@pytest.mark.parametrize('route', ['chat', 'agent', 'openai', 'anthropic'])
def test_timer_continuation_is_not_offered(route, runtime, monkeypatch):
    provider = ScriptedProvider({'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(route)))

    offered = provider.offered()
    assert offered, 'the route must still offer its ordinary tools'
    assert 'timer_create' not in offered
    target = offered.get('schedule_create', {}).get('parameters', {}).get(
        'properties', {}).get('target_conv_id', {}).get('description', '')
    assert 'for this conversation' not in target
    assert runtime.timer_loops == [] and not runtime.new_timers()


@pytest.mark.parametrize('route', ['chat', 'agent', 'openai', 'anthropic'])
def test_sub_agents_are_not_offered(route, runtime, monkeypatch):
    """A sub-agent registers in, and reports into, a saved conversation."""
    provider = ScriptedProvider({'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(route)))

    offered = provider.offered()
    assert offered, 'the route must still offer its ordinary tools'
    assert not {'spawn_agents', 'get_agent_result', 'manage_agents'} & set(offered)
    assert 'use one spawn_agents call' not in json.dumps(provider.requests[0]['messages'])


# Everything a request without a saved conversation is never offered: the
# deferred timer continuation, and every tool whose call registers in, reads
# from or reports into the request's own conversation (sub-agents, retained
# agents, exploration plans, working facts and the project mailbox).
NOT_OFFERED = frozenset({
    'timer_create',
    'spawn_agents', 'get_agent_result', 'send_agent_message',
    'get_agent_messages', 'set_approval_delegate', 'manage_agents', 'list_agents',
    'get_exploration', 'manage_exploration', 'read_working_facts',
    'record_working_fact', 'send_project_message', 'get_project_messages',
    'consider_project_message',
})


@pytest.mark.parametrize('route,mode', [
    ('chat', 'code_exec'), ('agent', 'code_exec'), ('chat', 'project'),
    ('agent', 'project'), ('openai', None), ('anthropic', None),
])
def test_conversation_scoped_tools_are_not_offered(route, mode, runtime, monkeypatch, tmp_path):
    """Neither the wire nor the Tool Search catalog offers them, nor does any prompt name them."""
    provider = ScriptedProvider({'content': 'finished'}).install(monkeypatch)
    config = _command_config(mode, tmp_path) if mode else None
    _run(_post(runtime, *_task_request(route, config=config)))

    offered, catalog = set(provider.offered()), provider.catalog()
    assert offered and catalog, 'the route must still offer its ordinary tools'
    assert not NOT_OFFERED & (offered | catalog)
    prompt = json.dumps(provider.requests[0]['messages'])
    assert not [name for name in NOT_OFFERED if name in prompt]
    if mode:
        # A handed-off command is collected and stopped within the request.
        assert {'run_command', 'poll', 'kill'} <= offered
    # Owner-scoped scheduling (command/python/prompt jobs, or agent turns in an
    # existing conversation) stays, as do the task's own isolated-workspace
    # controls and reads of other owned conversations.
    assert {'schedule_create', 'schedule_list', 'schedule_manage',
            'timer_manage'} <= catalog
    if mode == 'project':
        assert {'integration_checkpoint', 'integration_submit',
                'list_conversations', 'get_conversation'} <= catalog


SPAWN_ARGUMENTS = {'agents': [{'id': 'probe-agent', 'role': 'general',
                               'objective': 'Summarize the repository layout.'}]}


def test_spawn_agents_is_refused_before_any_agent_exists(runtime, monkeypatch):
    """Native caller-supplied schemas still reach the swarm handler, which refuses."""
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    launches = []
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool',
                        lambda *args, **kwargs: launches.append(args) or '{}')
    provider = ScriptedProvider(
        _call('spawn_agents', SPAWN_ARGUMENTS, 'call-spawn'),
        {'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request('chat', tools=[SPAWN_AGENTS_TOOL])))

    result, = provider.tool_results()
    assert NO_CONVERSATION in result, result
    assert 'No agent was created' in result
    assert 'directly' in result  # the next action: do the work in this request
    assert 'arrive automatically' not in result and 'async_launched' not in result
    assert launches == []
    # Every model request is the parent's own; no worker ever ran. The prompt
    # may span several system messages, so locate the opening user turn.
    opening_user_turns = [
        next(m['content'] for m in request['messages'] if m.get('role') == 'user')
        for request in provider.requests]
    assert all('Run the requested command.' in json.dumps(turn)
               for turn in opening_user_turns), opening_user_turns


TIMER_ARGUMENTS = {'continuation_message': 'Report the build result.',
                   'condition_command': 'test -f /nonexistent/ready'}


def test_timer_and_current_conversation_schedule_are_refused_when_called(runtime, monkeypatch):
    """Caller-supplied schemas reach the scheduler handler, which refuses."""
    from lib.scheduler.tool_defs import SCHEDULE_TOOL_CREATE, TIMER_TOOL_CREATE

    provider = ScriptedProvider(
        _call('timer_create', TIMER_ARGUMENTS, 'call-timer'),
        _call('schedule_create', {'name': 'watch', 'schedule': '*/5 * * * *',
                                  'command': 'check', 'task_type': 'agent',
                                  'target_conv_id': 'current'}, 'call-schedule'),
        {'content': 'finished'}).install(monkeypatch)
    _run(_post(runtime, *_task_request(
        'chat', tools=[TIMER_TOOL_CREATE, SCHEDULE_TOOL_CREATE])))

    timer_result, = provider.tool_results(1)
    schedule_result, = provider.tool_results(2)[1:]
    for result in (timer_result, schedule_result):
        assert NO_CONVERSATION in result, result
    assert 'run_command' in timer_result  # the next action: wait in this request
    assert 'target_conv_id' in schedule_result
    assert runtime.timer_loops == [] and not runtime.new_timers()
    assert not runtime.new_schedules()


@pytest.mark.parametrize('compatibility', [False, True])
def test_direct_relay_never_executes_server_tools_or_defers_work(
        compatibility, runtime, monkeypatch, tmp_path):
    """chat_direct has no task, so it never reaches a background lane."""
    from copy import deepcopy
    from lib.llm import astream
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.scheduler.tool_defs import TIMER_TOOL_CREATE

    marker = tmp_path / 'direct-ran'
    calls = [{'id': 'call-direct-1', 'type': 'function', 'function': {
        'name': 'run_command',
        'arguments': json.dumps({'command': f'touch {marker}', 'background': True})}},
        {'id': 'call-direct-2', 'type': 'function', 'function': {
            'name': 'timer_create', 'arguments': json.dumps(TIMER_ARGUMENTS)}}]
    observed = []

    async def transport(body, **_kwargs):
        observed.append(deepcopy(body.get('tools')))
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': None, 'tool_calls': calls},
            'tool_calls', {'prompt_tokens': 11, 'completion_tokens': 7})

    monkeypatch.setattr(astream, 'async_stream_chat', transport)
    messages = [{'role': 'user', 'content': 'Run it in the background.'}]
    if compatibility:
        path, body = '/v1/chat/completions', {
            'model': 'stub-model', 'tofu_creator_id': 'tofu-test', 'messages': messages,
            'tools': [CODE_EXEC_TOOL, TIMER_TOOL_CREATE], 'max_tokens': 128,
            'timeout_s': 10}
    else:
        path, body = '/api/v1/chat/stream-direct', {
            'model': native_test_model(), 'messages': messages, 'timeout_s': 10}
    _run(_post(runtime, path, body))

    assert len(observed) == 1
    offered = {tool['function']['name'] for tool in observed[0] or []}
    assert offered == ({'run_command', 'timer_create'} if compatibility else set())
    assert not marker.exists()
    assert _commands() == [] and runtime.timer_loops == [] and not runtime.new_timers()
