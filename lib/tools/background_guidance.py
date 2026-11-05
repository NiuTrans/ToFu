"""Project background-command guidance from the live executable catalog.

Provider schemas, search documents, and command receipts share this pure owner.
Projection changes prose only: it never grants controls, changes validation, or
mutates canonical schemas. Role hints and visible wire tools are not authority.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from lib.tools.poll import POLL_WAIT_CAP_S


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


def _control_guidance(controls: frozenset[str], *,
                      automatic_delivery: bool = True) -> str:
    parts = []
    if 'poll' in controls:
        parts.append('Call `poll` with this command ID and `wait_s` to read '
                     'its status or wait mid-turn.' if automatic_delivery else
                     'Call `poll` with this command ID and `wait_s` to read '
                     'its status or wait for it; collect its result before you '
                     'finish.')
    if 'kill' in controls:
        parts.append('Use `kill` with this command ID to stop it.')
    else:
        parts.append('If stopping is needed, return the command ID to the '
                     'parent or operator; this task has no stop control.')
    if 'poll' not in controls:
        parts.append('No result control is authorized here; its completion is '
                     'delivered automatically, so wait for it or return the '
                     'command ID to the parent or operator for handling.'
                     if automatic_delivery else
                     'No result control is authorized here; return the '
                     'command ID to the parent or operator for handling.')
    if controls != {'poll', 'kill'}:
        parts.append('Do not emulate unavailable controls.')
    return ' '.join(parts)


def background_command_guidance(*, authority_catalog: Any,
                                has_conversation: bool,
                                outlives_run: bool = False) -> str:
    """Render a receipt using current task authority, not its launch snapshot."""
    if outlives_run and not has_conversation:
        # A storage-free run has no conversation to publish into, and a service
        # started here must outlive the agent: say both, or the model ends its
        # run to "wait for delivery" or stops a service it was asked to leave up.
        controls = _control_names(authority_catalog)
        advice = ('Call `poll` with this command ID and `wait_s` when a later '
                  'step needs its result; a blocking wait returns as soon as it '
                  'finishes.' if 'poll' in controls else
                  'No result control is authorized here; return the command ID '
                  'to the parent or operator for handling.')
        lifetime = ('A command still running when this run returns keeps '
                    'running, so a service you started stays up. '
                    + ('Use `kill` only when it must stop.'
                       if 'kill' in controls else ''))
        return ('Nothing delivers its completion to this run. ' + advice + ' '
                + lifetime)
    delivery = (
        'Its completion will be shown in the conversation. After all pending '
        'commands finish, their results are reviewed together automatically. '
        'Do other work or end this turn for automatic delivery. '
        if has_conversation else
        'No conversation completion delivery is available. '
    )
    return delivery + _control_guidance(_control_names(authority_catalog),
                                        automatic_delivery=has_conversation)


def background_wait_cap_advice(command_id: str, *, automatic_delivery: bool) -> str:
    """Advice after a capped wait on a command that is still running."""
    if automatic_delivery:
        return (f'Use poll(id={command_id!r}, wait_s={POLL_WAIT_CAP_S}) only when '
                'no useful work remains, or end the turn — completion is '
                'delivered automatically.')
    return (f'Call poll(id={command_id!r}, wait_s={POLL_WAIT_CAP_S}) again to keep '
            'waiting, or do other work and poll it before you finish; nothing '
            'delivers its completion to this task automatically.')


_STOP_WITH_CONTROL = (
    'runs until it finishes, the user stops it, or you stop a '
    'backgrounded one with `kill`.')
_STOP_WITHOUT_CONTROL = 'runs until it finishes or the user stops it.'
_BACKGROUND_INTRO = (
    'Start in background and return a command_id handle. '
    'Completion is delivered automatically — end the turn or do other work. ')
_BACKGROUND_CONTROLS = '`poll` reads its status or waits for it; `kill` stops it.'
_HANDOFF_PROMISE = ('Execution may move to the background on new messages; '
                    'results arrive automatically. ')
_POLL_DELIVERY_PROMISE = (' Background command completions are also delivered '
                          'automatically, so only wait when there is nothing '
                          'else to do.')
# The intro for a task whose completions are never pushed to it. _project_text
# recognizes it, so control projection never restores a delivery promise.
_PULL_ONLY_INTRO = ('Start in background and return a command_id handle. '
                    'Nothing delivers its completion to this task automatically. ')
_PULL_ONLY_TOOL_NAMES = frozenset({'run_command', 'poll'})
# Deliberately matches none of the stop/intro/control phrases above, so the
# control projection below never rewrites it back into a background promise.
_FOREGROUND_ONLY = ('runs in the foreground until it finishes or the request '
                    'is stopped; this request has no saved conversation, so a '
                    'command never moves to the background.')
_BACKGROUND_UNAVAILABLE = (
    'Unavailable: this request has no saved conversation, so a background '
    'completion could never be delivered. Omit it; the command runs in the '
    'foreground until it finishes.')


# Handles a request without a saved conversation can never obtain, in the
# canonical poll prose (lib/tools/poll.py): its commands stay in the
# foreground and it starts no sub-agent. ``kill`` could stop nothing there and
# is not offered.
_UNOBTAINABLE_HANDLE_PROSE = {
    'poll': (
        ('description', 'background shell commands (bg_* handles from '
                        'run_command background=true), swarm sub-agents from '
                        'spawn_agents, or ', ''),
        ('description', _POLL_DELIVERY_PROMISE, ''),
        ('id', 'a bg_* command id, a swarm agent id, or a task id', 'a task id'),
        ('mode', "With multiple swarm-agent ids: 'any' (default) returns as "
                 "soon as one finishes; 'all' waits for every match.",
         'Unused here: this request starts no sub-agents.'),
    ),
}


# A leaf worker reaches only the bg_* commands it started
# (lib.swarm.routing.is_leaf_worker); its poll/kill prose must not advertise
# agent or task handles it would be refused.
_LEAF_WORKER_PROSE = {
    'poll': (
        ('background shell commands (bg_* handles from run_command '
         'background=true), swarm sub-agents from spawn_agents, or tasks '
         'running in other conversations',
         'background shell commands you started (bg_* handles from your '
         'run_command background=true); handles of other agents, tasks and '
         'commands are refused'),
        ('With no id, returns a snapshot of everything currently running.',
         'With no id, lists your background commands.'),
        ('a bg_* command id, a swarm agent id, or a task id',
         'a bg_* command id you started'),
        ("With multiple swarm-agent ids: 'any' (default) returns as soon as "
         "one finishes; 'all' waits for every match.",
         'Unused here: you poll only your own commands.'),
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


def _leaf_worker_value(name: str, value: Any) -> Any:
    if isinstance(value, str):
        for old, new in _LEAF_WORKER_PROSE[name]:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {key: _leaf_worker_value(name, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_leaf_worker_value(name, item) for item in value]
    return value


def project_leaf_worker_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe poll/kill for a leaf worker: only the bg_* commands it started.

    Prose only: names, parameters and validation are unchanged, and the
    handler (lib/tasks_pkg/handlers/poll.py) enforces the scope.
    """
    return [_leaf_worker_value(_schema_name(tool), tool)
            if _schema_name(tool) in _LEAF_WORKER_PROSE else tool
            for tool in tools or []]


def project_leaf_worker_documents(documents_by_name: Any) -> Any:
    """Apply the same projection to contract documents (search and help text)."""
    if not isinstance(documents_by_name, dict):
        return documents_by_name
    return {name: _leaf_worker_value(name, document)
            if name in _LEAF_WORKER_PROSE else document
            for name, document in documents_by_name.items()}


def _project_foreground_run_command(fn: dict[str, Any]) -> None:
    if isinstance(fn.get('description'), str):
        fn['description'] = fn['description'].replace(
            _HANDOFF_PROMISE, '').replace(
            _STOP_WITH_CONTROL, _FOREGROUND_ONLY).replace(
            _STOP_WITHOUT_CONTROL, _FOREGROUND_ONLY)
    background = ((fn.get('parameters') or {}).get('properties') or {}).get('background')
    if isinstance(background, dict):
        background['description'] = _BACKGROUND_UNAVAILABLE


def _project_without_unobtainable_handles(fn: dict[str, Any]) -> None:
    for field, old, new in _UNOBTAINABLE_HANDLE_PROSE[fn['name']]:
        owner = (fn if field == 'description' else
                 ((fn.get('parameters') or {}).get('properties') or {}).get(field))
        if isinstance(owner, dict) and isinstance(owner.get('description'), str):
            owner['description'] = owner['description'].replace(old, new)


def project_no_saved_conversation_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe command tools for a task without a saved conversation.

    Its commands never leave the foreground, ``background`` is refused and no
    sub-agent can start, so neither the handoff promise nor a command or
    sub-agent handle may be advertised; ``poll`` keeps only tasks in other
    conversations. Prose only: parameters stay declared and validation is
    unchanged.
    """
    projected = []
    for tool in tools or []:
        fn = tool.get('function') if isinstance(tool, dict) else None
        name = fn.get('name') if isinstance(fn, dict) else None
        if name != 'run_command' and name not in _UNOBTAINABLE_HANDLE_PROSE:
            projected.append(tool)
            continue
        tool = deepcopy(tool)
        if name == 'run_command':
            _project_foreground_run_command(tool['function'])
        else:
            _project_without_unobtainable_handles(tool['function'])
        projected.append(tool)
    return projected


def _pull_only_value(value: Any) -> Any:
    if isinstance(value, str):
        return (value.replace(_HANDOFF_PROMISE, '')
                .replace(_BACKGROUND_INTRO, _PULL_ONLY_INTRO)
                .replace(_POLL_DELIVERY_PROMISE, ''))
    if isinstance(value, dict):
        return {key: _pull_only_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pull_only_value(item) for item in value]
    return value


def _schema_name(tool: Any) -> Any:
    fn = tool.get('function') if isinstance(tool, dict) else None
    fn = fn if isinstance(fn, dict) else tool
    return fn.get('name') if isinstance(fn, dict) else None


def project_pull_only_completion_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Describe command tools for a task whose completions are never pushed to it.

    A child invocation runs in its parent's conversation without owning a turn
    there (lib.tasks_pkg.conversation_delivery): its commands never hand off,
    and an explicit background command is read only with ``poll``. Neither
    ``run_command`` nor ``poll`` may promise automatic delivery. Prose only:
    names, parameters and validation are unchanged.
    """
    return [_pull_only_value(tool) if _schema_name(tool) in _PULL_ONLY_TOOL_NAMES
            else tool for tool in tools or []]


def project_pull_only_completion_documents(documents_by_name: Any) -> Any:
    """Apply the same projection to contract documents (search and help text)."""
    if not isinstance(documents_by_name, dict):
        return documents_by_name
    return {name: _pull_only_value(document) if name in _PULL_ONLY_TOOL_NAMES
            else document for name, document in documents_by_name.items()}


def _project_text(text: str, controls: frozenset[str]) -> str:
    stop = _STOP_WITH_CONTROL if 'kill' in controls else _STOP_WITHOUT_CONTROL
    text = text.replace(_STOP_WITH_CONTROL, stop).replace(
        _STOP_WITHOUT_CONTROL, stop)
    # Rebuild our owned suffix even if a previous request projected it. A
    # revoked control must not survive in a latched or retried wire schema.
    for intro, automatic_delivery in ((_BACKGROUND_INTRO, True),
                                      (_PULL_ONLY_INTRO, False)):
        if text.startswith(intro):
            return intro + (
                _BACKGROUND_CONTROLS if controls == {'poll', 'kill'} else
                _control_guidance(controls, automatic_delivery=automatic_delivery))
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
