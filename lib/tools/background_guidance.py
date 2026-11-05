"""Project background-command guidance from the live executable catalog.

Provider schemas, search documents, and command receipts share this pure owner.
Projection changes prose only: it never grants controls, changes validation, or
mutates canonical schemas. Role hints and visible wire tools are not authority.

The prose follows the task's completion delivery
(``lib.tasks_pkg.conversation_delivery.completion_delivery``): ``conversation``
is the canonical wording; ``run`` is a root task without a saved conversation,
which reads a finished command at its next round boundary while the run
continues; ``poll`` is a child invocation, which collects it with ``poll``;
``none`` has no control that could collect a result, so its commands never
leave the foreground. ``lib/tools/project.py`` builds the canonical
``run_command`` description from the constants below, so every projection
matches its exact source text.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from lib.tools.poll import AUTO_BACKGROUND_AFTER_S, POLL_WAIT_CAP_S

RUN_COMMAND_STOP = (
    'runs until it finishes, the user stops it, or you stop a '
    'backgrounded one with `kill`.')
RUN_COMMAND_HANDOFF = (
    f'A command still running after {AUTO_BACKGROUND_AFTER_S} s, or when a new '
    'message arrives, moves to the background and returns a `bg_*` handle; '
    'results arrive automatically. ')
_STOP_WITHOUT_CONTROL = 'runs until it finishes or the user stops it.'
_RUN_HANDOFF = (
    f'A command still running after {AUTO_BACKGROUND_AFTER_S} s moves to the '
    'background and returns a `bg_*` handle; its result reaches your next step '
    'once it finishes, or `poll` waits for it. ')
_PULL_HANDOFF = (
    f'A command still running after {AUTO_BACKGROUND_AFTER_S} s moves to the '
    'background and returns a `bg_*` handle; collect its result with `poll` '
    'before you finish. ')
_BACKGROUND_INTRO = (
    'Start in background and return a command_id handle. '
    'Completion is delivered automatically — end the turn or do other work. ')
_RUN_INTRO = (
    'Start in background and return a command_id handle. '
    'Its result reaches your next step once it finishes; nothing delivers it '
    'after this run returns. ')
# The intro for a task whose completions are never pushed to it. _project_text
# recognizes it, so control projection never restores a delivery promise.
_PULL_ONLY_INTRO = ('Start in background and return a command_id handle. '
                    'Nothing delivers its completion to this task automatically. ')
_BACKGROUND_CONTROLS = '`poll` reads its status or waits for it; `kill` stops it.'
_POLL_DELIVERY_PROMISE = (' Background command completions are also delivered '
                          'automatically, so only wait when there is nothing '
                          'else to do.')
_RUN_POLL_DELIVERY = (' Background command completions also reach your next '
                      'step while this run continues, so only wait when there '
                      'is nothing else to do.')
_DELIVERY_TOOL_NAMES = frozenset({'run_command', 'poll'})
_RUN_REPLACEMENTS = (
    (RUN_COMMAND_HANDOFF, _RUN_HANDOFF),
    (_BACKGROUND_INTRO, _RUN_INTRO),
    (_POLL_DELIVERY_PROMISE, _RUN_POLL_DELIVERY),
)
# Idempotent over the canonical and the run projection: a child's catalog may
# be copied from either before its own assembly projects it.
_PULL_REPLACEMENTS = (
    (RUN_COMMAND_HANDOFF, _PULL_HANDOFF),
    (_RUN_HANDOFF, _PULL_HANDOFF),
    (_BACKGROUND_INTRO, _PULL_ONLY_INTRO),
    (_RUN_INTRO, _PULL_ONLY_INTRO),
    (_POLL_DELIVERY_PROMISE, ''),
    (_RUN_POLL_DELIVERY, ''),
)
_BACKGROUND_UNAVAILABLE = (
    'Unavailable: this request has no saved conversation, so nothing it starts '
    'may outlive the response. Omit it: a command still running after '
    f'{AUTO_BACKGROUND_AFTER_S} s moves to the background on its own and is '
    'stopped when the response ends.')


def _control_names(catalog: Any) -> frozenset[str]:
    if not isinstance(catalog, (list, tuple)):
        return frozenset()
    names = set()
    for tool in catalog:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function')
        fn = fn if isinstance(fn, dict) else tool
        name = fn.get('name')
        if name in ('poll', 'kill'):
            names.add(name)
    return frozenset(names)


def result_controls(catalog: Any) -> frozenset[str]:
    """The background controls (``poll``/``kill``) the live catalog authorizes."""
    return _control_names(catalog)


_POLL_ADVICE = {
    'conversation': ('Call `poll` with this command ID and `wait_s` to read '
                     'its status or wait mid-turn.'),
    'run': ('Call `poll` with this command ID and `wait_s` to wait for it; a '
            'blocking wait returns as soon as it finishes, so collect it '
            'before you finish.'),
}
_POLL_PULL_ADVICE = ('Call `poll` with this command ID and `wait_s` to read '
                     'its status or wait for it; collect its result before you '
                     'finish.')


def _control_guidance(controls: frozenset[str], *,
                      delivery: str = 'conversation') -> str:
    parts = []
    if 'poll' in controls:
        parts.append(_POLL_ADVICE.get(delivery, _POLL_PULL_ADVICE))
    if 'kill' in controls:
        parts.append('Use `kill` with this command ID to stop it.')
    else:
        parts.append('If stopping is needed, return the command ID to the '
                     'parent or operator; this task has no stop control.')
    if 'poll' not in controls:
        parts.append('No result control is authorized here; its completion is '
                     'delivered automatically, so wait for it or return the '
                     'command ID to the parent or operator for handling.'
                     if delivery == 'conversation' else
                     'No result control is authorized here; return the '
                     'command ID to the parent or operator for handling.')
    if controls != {'poll', 'kill'}:
        parts.append('Do not emulate unavailable controls.')
    return ' '.join(parts)


def background_command_guidance(*, authority_catalog: Any, delivery: str,
                                outlives_run: bool = False,
                                ends_with_response: bool = False) -> str:
    """Render a receipt using current task authority, not its launch snapshot.

    ``outlives_run``: a storage-free run, whose still-running commands keep
    running after it returns (a service started in a sandbox must outlive the
    agent). ``ends_with_response``: a request without a saved conversation,
    whose still-running commands stop when its response ends.
    """
    controls = _control_names(authority_catalog)
    if delivery == 'conversation':
        return ('Its completion will be shown in the conversation. After all '
                'pending commands finish, their results are reviewed together '
                'automatically. Do other work or end this turn for automatic '
                'delivery. ' + _control_guidance(controls, delivery=delivery))
    if delivery == 'run':
        receipt = ('Its result reaches your next step once it finishes; nothing '
                   'delivers it after this run returns. '
                   + _control_guidance(controls, delivery=delivery))
        if ends_with_response:
            return receipt + (' A command still running when this request\'s '
                              'response ends is stopped.')
        if outlives_run:
            return receipt + (' A command still running when this run returns '
                              'keeps running, so a service you started stays up.')
        return receipt
    return ('No conversation completion delivery is available. '
            + _control_guidance(controls, delivery=delivery))


def background_wait_cap_advice(command_id: str, *, delivery: str) -> str:
    """Advice after a capped wait on a command that is still running."""
    if delivery == 'conversation':
        return (f'Use poll(id={command_id!r}, wait_s={POLL_WAIT_CAP_S}) only when '
                'no useful work remains, or end the turn — completion is '
                'delivered automatically.')
    if delivery == 'run':
        return (f'Call poll(id={command_id!r}, wait_s={POLL_WAIT_CAP_S}) again to '
                'keep waiting, or do other work: its result reaches your next '
                'step once it finishes, but nothing delivers it after this run '
                'returns.')
    return (f'Call poll(id={command_id!r}, wait_s={POLL_WAIT_CAP_S}) again to keep '
            'waiting, or do other work and poll it before you finish; nothing '
            'delivers its completion to this task automatically.')


# A request without a saved conversation starts no sub-agent, so poll and kill
# must not advertise agent handles there (lib/tools/poll.py prose).
_NO_SUBAGENT_PROSE = {
    'poll': (
        ('description', 'swarm sub-agents from spawn_agents, or ', 'or '),
        ('id', 'a bg_* command id, a swarm agent id, or a task id',
         'a bg_* command id or a task id'),
        ('mode', 'With multiple command ids or multiple swarm-agent ids:',
         'With multiple command ids:'),
    ),
    'kill': (
        ('description', ' or a RUNNING swarm sub-agent', ''),
        ('id', 'a bg_* command id or a swarm agent id', 'a bg_* command id'),
    ),
}


# A leaf worker reaches only the bg_* commands it started
# (lib.swarm.routing.is_leaf_worker); its poll/kill prose must not advertise
# agent or task handles it would be refused.
_LEAF_WORKER_PROSE = {
    'poll': (
        ('background shell commands (bg_* handles from run_command), swarm '
         'sub-agents from spawn_agents, or tasks running in other '
         'conversations',
         'background shell commands you started (bg_* handles from your '
         'run_command calls); handles of other agents, tasks and commands are '
         'refused'),
        ('With no id, returns a snapshot of everything currently running.',
         'With no id, lists your background commands.'),
        ('a bg_* command id, a swarm agent id, or a task id',
         'a bg_* command id you started'),
        ('With multiple command ids or multiple swarm-agent ids:',
         'With multiple command ids:'),
    ),
    'kill': (
        ('a bg_* background command (its process tree receives SIGTERM then '
         'SIGKILL) or a RUNNING swarm sub-agent',
         'a bg_* background command you started (its process tree receives '
         'SIGTERM then SIGKILL); other handles are refused'),
        ("Tasks in other conversations cannot be killed from here; use that "
         "conversation's Stop control. ", ''),
        ('a bg_* command id or a swarm agent id', 'a bg_* command id you started'),
    ),
}


def _replace_all(value: Any, replacements) -> Any:
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {key: _replace_all(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_all(item, replacements) for item in value]
    return value


def _schema_name(tool: Any) -> Any:
    fn = tool.get('function') if isinstance(tool, dict) else None
    fn = fn if isinstance(fn, dict) else tool
    return fn.get('name') if isinstance(fn, dict) else None


def project_leaf_worker_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe poll/kill for a leaf worker: only the bg_* commands it started.

    Prose only: names, parameters and validation are unchanged, and the
    handler (lib/tasks_pkg/handlers/poll.py) enforces the scope.
    """
    return [_replace_all(tool, _LEAF_WORKER_PROSE[_schema_name(tool)])
            if _schema_name(tool) in _LEAF_WORKER_PROSE else tool
            for tool in tools or []]


def project_leaf_worker_documents(documents_by_name: Any) -> Any:
    """Apply the same projection to contract documents (search and help text)."""
    if not isinstance(documents_by_name, dict):
        return documents_by_name
    return {name: _replace_all(document, _LEAF_WORKER_PROSE[name])
            if name in _LEAF_WORKER_PROSE else document
            for name, document in documents_by_name.items()}


def _project_without_subagents(fn: dict[str, Any]) -> None:
    for field, old, new in _NO_SUBAGENT_PROSE[fn['name']]:
        owner = (fn if field == 'description' else
                 ((fn.get('parameters') or {}).get('properties') or {}).get(field))
        if isinstance(owner, dict) and isinstance(owner.get('description'), str):
            owner['description'] = owner['description'].replace(old, new)


def project_no_saved_conversation_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe command tools for a request without a saved conversation.

    Its run reads a handed-off command's result at the next round boundary or
    with ``poll``, ``background`` is refused because nothing it starts may
    outlive the response, and no sub-agent can start, so neither a
    conversation promise nor a sub-agent handle may be advertised. Prose only:
    parameters stay declared and validation is unchanged.
    """
    projected = []
    for tool in tools or []:
        name = _schema_name(tool)
        if name not in ('run_command', 'poll', 'kill'):
            projected.append(tool)
            continue
        tool = _replace_all(tool, _RUN_REPLACEMENTS)
        fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
        if name == 'run_command':
            background = ((fn.get('parameters') or {}).get('properties') or {}).get('background')
            if isinstance(background, dict):
                background['description'] = _BACKGROUND_UNAVAILABLE
        else:
            _project_without_subagents(fn)
        projected.append(tool)
    return projected


def project_run_completion_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe command tools for a root task without a saved conversation.

    Storage-free runs and conversation-less roots read a finished command at
    their next round boundary while the run continues, and with ``poll``;
    nothing publishes it after the run returns. Prose only: names, parameters
    and validation are unchanged.
    """
    return [_replace_all(tool, _RUN_REPLACEMENTS)
            if _schema_name(tool) in _DELIVERY_TOOL_NAMES else tool
            for tool in tools or []]


def project_pull_only_completion_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe command tools for a task whose completions are never pushed to it.

    A child invocation runs in its parent's conversation without owning a turn
    there (lib.tasks_pkg.conversation_delivery) and drains no inbox, so a
    handed-off or explicit background command is read only with ``poll``.
    Neither ``run_command`` nor ``poll`` may promise automatic delivery. Prose
    only: names, parameters and validation are unchanged.
    """
    return [_replace_all(tool, _PULL_REPLACEMENTS)
            if _schema_name(tool) in _DELIVERY_TOOL_NAMES else tool
            for tool in tools or []]


def project_pull_only_completion_documents(documents_by_name: Any) -> Any:
    """Apply the same projection to contract documents (search and help text)."""
    if not isinstance(documents_by_name, dict):
        return documents_by_name
    return {name: _replace_all(document, _PULL_REPLACEMENTS)
            if name in _DELIVERY_TOOL_NAMES else document
            for name, document in documents_by_name.items()}


def _project_text(text: str, controls: frozenset[str]) -> str:
    stop = RUN_COMMAND_STOP if 'kill' in controls else _STOP_WITHOUT_CONTROL
    text = text.replace(RUN_COMMAND_STOP, stop).replace(
        _STOP_WITHOUT_CONTROL, stop)
    if 'poll' not in controls:
        # Without poll only a conversation can collect a handed-off result, so
        # a run or child task keeps every command in the foreground.
        text = text.replace(_RUN_HANDOFF, '').replace(_PULL_HANDOFF, '')
    # Rebuild our owned suffix even if a previous request projected it. A
    # revoked control must not survive in a latched or retried wire schema.
    for intro, delivery in ((_BACKGROUND_INTRO, 'conversation'),
                            (_RUN_INTRO, 'run'),
                            (_PULL_ONLY_INTRO, 'poll')):
        if text.startswith(intro):
            return intro + (
                _BACKGROUND_CONTROLS if controls == {'poll', 'kill'} else
                _control_guidance(controls, delivery=delivery))
    advice = (_BACKGROUND_CONTROLS if controls == {'poll', 'kill'}
              else _control_guidance(controls))
    return text.replace(_BACKGROUND_CONTROLS, advice)


def _project_parameters(parameters: Any, controls: frozenset[str]) -> None:
    if not isinstance(parameters, dict):
        return
    properties = parameters.get('properties')
    if not isinstance(properties, dict):
        return
    background = properties.get('background')
    if isinstance(background, dict) and isinstance(
            background.get('description'), str):
        background['description'] = _project_text(
            background['description'], controls)


def project_background_tool_guidance(
    tools: list[dict[str, Any]] | None, *, authority_catalog: Any,
) -> list[dict[str, Any]]:
    """Copy only run_command prose when the task lacks canonical controls."""
    controls = _control_names(authority_catalog)
    projected = []
    for tool in tools or []:
        fn = tool.get('function') if isinstance(tool, dict) else None
        fn = fn if isinstance(fn, dict) else tool
        if not isinstance(fn, dict) or fn.get('name') != 'run_command':
            projected.append(tool)
            continue
        tool = deepcopy(tool)
        fn = tool.get('function')
        fn = fn if isinstance(fn, dict) else tool
        if isinstance(fn.get('description'), str):
            fn['description'] = _project_text(fn['description'], controls)
        _project_parameters(fn.get('parameters'), controls)
        projected.append(tool)
    return projected


def project_background_search_document(
    document: dict[str, Any], *, authority_catalog: Any,
) -> dict[str, Any]:
    """Project discovery help without changing the execution contract owner."""
    controls = _control_names(authority_catalog)
    if document.get('name') != 'run_command' or controls == {'poll', 'kill'}:
        return document
    projected = deepcopy(document)
    for key in ('summary', 'help'):
        if isinstance(projected.get(key), str):
            projected[key] = _project_text(projected[key], controls)
    _project_parameters(projected.get('arguments_schema'), controls)
    return projected
