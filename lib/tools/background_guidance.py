"""Project background-command guidance from the live executable catalog.

Provider schemas, search documents, and command receipts share this pure owner.
Projection changes prose only: it never grants controls, changes validation, or
mutates canonical schemas. Role hints and visible wire tools are not authority.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


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


def _control_guidance(controls: frozenset[str]) -> str:
    parts = []
    if 'poll' in controls:
        parts.append('Call `poll` with this command ID and `wait_s` to read '
                     'its status or wait mid-turn.')
    if 'kill' in controls:
        parts.append('Use `kill` with this command ID to stop it.')
    else:
        parts.append('If stopping is needed, return the command ID to the '
                     'parent or operator; this task has no stop control.')
    if 'poll' not in controls:
        parts.append('No result control is authorized here; rely on automatic '
                     'delivery when available, otherwise return the command '
                     'ID to the parent or operator for handling.')
    if controls != {'poll', 'kill'}:
        parts.append('Do not emulate unavailable controls.')
    return ' '.join(parts)


def background_command_guidance(*, authority_catalog: Any,
                                has_conversation: bool) -> str:
    """Render a receipt using current task authority, not its launch snapshot."""
    delivery = (
        'Its completion will be shown in the conversation. After all pending '
        'commands finish, their results are reviewed together automatically. '
        'Do other work or end this turn for automatic delivery. '
        if has_conversation else
        'No conversation completion delivery is available. '
    )
    return delivery + _control_guidance(_control_names(authority_catalog))


_STOP_WITH_CONTROL = (
    'runs until it finishes, the user stops it, or you stop a '
    'backgrounded one with `kill`.')
_STOP_WITHOUT_CONTROL = 'runs until it finishes or the user stops it.'
_BACKGROUND_INTRO = (
    'Start in background and return a command_id handle. '
    'Completion is delivered automatically — end the turn or do other work. ')
_BACKGROUND_CONTROLS = '`poll` reads its status or waits for it; `kill` stops it.'


def _project_text(text: str, controls: frozenset[str]) -> str:
    stop = _STOP_WITH_CONTROL if 'kill' in controls else _STOP_WITHOUT_CONTROL
    text = text.replace(_STOP_WITH_CONTROL, stop).replace(
        _STOP_WITHOUT_CONTROL, stop)
    advice = (_BACKGROUND_CONTROLS if controls == {'poll', 'kill'}
              else _control_guidance(controls))
    # Rebuild our owned suffix even if a previous request projected it. A
    # revoked control must not survive in a latched or retried wire schema.
    if text.startswith(_BACKGROUND_INTRO):
        return _BACKGROUND_INTRO + advice
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
