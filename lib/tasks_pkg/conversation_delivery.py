"""Deferred-completion delivery for tasks with and without a saved conversation.

Background commands, timer continuations and ``current``-conversation agent
schedules all finish after the tool call returns and publish their result into
the task's saved conversation. Headless request routes
(``POST /api/v1/chat/completions``, ``/api/v1/agent/run`` and the
OpenAI/Anthropic-compatible agent routes) instead run a task under a client job
label (``api-…``, ``agent-…``, ``compat-…`` or the caller's
``conversation_id``) that never becomes a saved conversation; their caller
receives only the request's own response. Such a task carries
:data:`NO_SAVED_CONVERSATION`, written only by
``create_task(..., saved_conversation=False)`` through
:func:`mark_no_saved_conversation`.

Readers use the predicates here, never the raw key: ``_inline_messages`` and
``_transientRuntime`` also mark swarm workers running inside a real parent
conversation, and an empty ``convId`` (Evolution stages, storage-free embeds)
already means "no conversation" with its own result owners.

A child invocation (swarm worker, orchestration step, approval delegate)
carries its parent's ``convId`` as execution identity but owns no turn in that
conversation. A deferred completion published there would inject the child's
result into, and wake, the parent's transcript, so a child has no conversation
delivery: its commands never hand off, and an explicit background command is
read with ``poll`` (docs/modules/task_engine.md, "Background command delivery
owner"). A timer continuation or a ``current`` agent schedule would start a
turn there that nobody in that conversation asked for, so a child, like a task
without a conversation, is refused both. A headless request cannot host a
sub-agent either: child lineage registers in, and reports into, a saved
conversation.

A storage-free runtime task (``tofu-agent run``/``serve`` and the embedded
``AgentRuntime``; config ``_storageFreeRuntime``) also runs under a job label
that no store holds, so nothing can publish a completion into it after its
tool call returns. Unlike a headless request it keeps explicit background
commands: they are pull-only, read with ``poll`` like a child's, and a command
still running when the run returns keeps running in its own session — a
service started in a sandbox container must outlive the agent that started it
(docs/DEVELOPER_RUNTIME.md, "Background commands").

Entry points: :func:`conversation_delivery_available` (handoff and receipt
policy), :func:`completion_conversation_id` (the conversation a background
command, timer or ``current`` schedule delivers into),
:func:`deferred_completion_refusal` (handler admission).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NO_SAVED_CONVERSATION = '_noSavedConversation'


def mark_no_saved_conversation(task: dict[str, Any]) -> None:
    """Stamp a newly created headless-request task; only task creation calls this."""
    task[NO_SAVED_CONVERSATION] = True


def has_no_saved_conversation(task: Mapping[str, Any]) -> bool:
    """Whether the task's creator declared that its ``convId`` is only a job label."""
    return task.get(NO_SAVED_CONVERSATION) is True


def storage_free_runtime(task: Mapping[str, Any]) -> bool:
    """Whether the task runs in the storage-free composition (no application store)."""
    return (task.get('config') or {}).get('_storageFreeRuntime') is True


def conversation_delivery_available(task: Mapping[str, Any]) -> bool:
    """Whether a deferred completion can publish into this task's own conversation."""
    from lib.tasks_pkg.invocation import is_child_invocation
    return (bool(task.get('convId') and task.get('_userId'))
            and not has_no_saved_conversation(task)
            and not storage_free_runtime(task)
            and not is_child_invocation(task))


def completion_conversation_id(task: Mapping[str, Any]) -> str:
    """The conversation that receives this task's deferred completions, or ''."""
    return str(task.get('convId') or '') if conversation_delivery_available(task) else ''


def completion_reaches(task: Mapping[str, Any], command: Mapping[str, Any]) -> bool:
    """Whether a polled command's completion is published into this task's conversation.

    ``command`` is a background-command snapshot: its ``conv_id`` names the
    conversation the command ran in, and only a command launched by that
    conversation's own task has a delivery status other than ``not_applicable``.
    """
    own = completion_conversation_id(task)
    return (bool(own) and command.get('conv_id') == own
            and command.get('delivery_status') != 'not_applicable')


# (typed error code, reason) for each kind of task that owns no conversation turn.
_HEADLESS = ('no_saved_conversation', 'This request has no saved conversation')
_CHILD = ('no_conversation_turn', "This agent runs inside its parent's "
          'conversation without owning a turn there')
_CONVERSATIONLESS = ('no_conversation_turn', 'This task has no conversation of its own')


def _refusal(owner: tuple[str, str], *, subject: str, outcome: str,
             next_action: str) -> str:
    from lib.tools.result_envelope import typed_tool_error
    code, reason = owner
    return typed_tool_error(
        code, retryable=False, message=f'{reason}, so {subject}. {outcome}',
        next_action=next_action,
    ).to_model_text()


def _targets_own_conversation(task: Mapping[str, Any], fn_name: str,
                              fn_args: Mapping[str, Any]) -> bool:
    target = str(fn_args.get('target_conv_id') or '').strip()
    return (fn_name == 'schedule_create' and fn_args.get('task_type') == 'agent'
            and bool(target) and target in {'current', str(task.get('convId') or '')})


def _headless_refusal(task: Mapping[str, Any], fn_name: str,
                      fn_args: Mapping[str, Any]) -> str | None:
    """Nothing a headless request starts may outlive its response."""
    if fn_name == 'spawn_agents' or (
            fn_name == 'manage_agents' and fn_args.get('action') == 'assign'):
        return _refusal(
            _HEADLESS, subject=('sub-agents cannot run: their runs register in, '
                                'and report into, a saved conversation'),
            outcome=('No agent was created.' if fn_name == 'spawn_agents'
                     else 'Nothing was assigned; the agent stays idle.'),
            next_action=('Do the work directly in this request with your own '
                         'tools; nothing started here may outlive the response.'))
    if fn_name == 'run_command' and fn_args.get('background'):
        return _refusal(
            _HEADLESS,
            subject="a background command's completion could never be delivered",
            outcome='No command ran.',
            next_action=('Run it again without `background`; it then runs in '
                         'the foreground until it finishes and returns its '
                         'result in this request.'))
    if fn_name == 'timer_create':
        return _refusal(
            _HEADLESS,
            subject='a timer would have no conversation to continue when it fires',
            outcome='No timer was created.',
            next_action=('Check readiness within this request instead, for '
                         'example with `run_command`, and continue once the '
                         'condition holds.'))
    if fn_name == 'await_device':
        return _refusal(
            _HEADLESS,
            subject='a device-online wake would have no conversation to continue',
            outcome='No wait was registered.',
            next_action=('Run the device work in a request while the device is '
                         'online, or from a saved conversation that can wait for it.'))
    if _targets_own_conversation(task, fn_name, fn_args):
        return _refusal(
            _HEADLESS,
            subject='a scheduled agent turn cannot target this request',
            outcome='No job was created.',
            next_action=('Pass an existing conversation ID as `target_conv_id`, '
                         'or create a command, python or prompt job.'))
    return None


def _unowned_turn_refusal(task: Mapping[str, Any], fn_name: str,
                          fn_args: Mapping[str, Any]) -> str | None:
    """A child or conversation-less task: no continuation may start a turn.

    Its commands stay allowed: an explicit background command is pull-only.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    child = is_child_invocation(task)
    owner = _CHILD if child else _CONVERSATIONLESS
    if fn_name == 'timer_create':
        return _refusal(
            owner, subject='a timer would have nothing to continue when it fires',
            outcome='No timer was created.',
            next_action=(
                f'Check readiness within this {"run" if child else "task"} instead, '
                'for example with `run_command` (or `poll` with `wait_s` for a '
                'background command), and continue once the condition holds.'
                + (' If the wait must outlive this run, report the condition in '
                   'your result so the parent can create the timer.' if child else '')))
    if fn_name == 'await_device':
        return _refusal(
            owner, subject='a device-online wake would have nothing to continue',
            outcome='No wait was registered.',
            next_action=('Report the device dependency in your result so the parent '
                         'can wait for the device.' if child else
                         'Run the device work while the device is online.'))
    if _targets_own_conversation(task, fn_name, fn_args):
        return _refusal(
            owner, subject='a scheduled agent turn cannot target it',
            outcome='No job was created.',
            next_action=(
                ('Report the job in your result so the parent can create it, '
                 'pass another' if child else 'Pass an')
                + ' existing conversation ID as `target_conv_id`, or create a '
                'command, python or prompt job.'))
    return None


def deferred_completion_refusal(task: Mapping[str, Any], fn_name: str,
                                fn_args: Mapping[str, Any]) -> str | None:
    """Return the model-visible refusal for a deferred lane, or None to proceed."""
    if has_no_saved_conversation(task):
        return _headless_refusal(task, fn_name, fn_args)
    if completion_conversation_id(task):
        return None
    return _unowned_turn_refusal(task, fn_name, fn_args)


__all__ = [
    'NO_SAVED_CONVERSATION',
    'completion_conversation_id',
    'completion_reaches',
    'conversation_delivery_available',
    'deferred_completion_refusal',
    'has_no_saved_conversation',
    'mark_no_saved_conversation',
    'storage_free_runtime',
]
