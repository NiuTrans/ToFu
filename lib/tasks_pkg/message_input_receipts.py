"""Exact mailbox carrier witnesses for the final physical provider attempt.

Injection is not delivery. Each retry independently filters immutable carrier
text against its actual request body. Only a verified response publishes the
matched IDs; a dropped or summarized carrier cannot be acknowledged.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.llm_errors import AbortedError, RequestScopedError


class MessageInputScopeError(RequestScopedError, PermissionError):
    """Local mailbox scope denial: no route retry, fallback, or health penalty."""

    failure_stage = 'before_dispatch'


class _ReceiptLease:
    """Decorate the existing physical-attempt lease, never authorize a route.

    Without a budget delegate this is only an input observer. Dispatch, slot
    reservation, cancellation, and settlement remain with their existing owners.
    """

    def __init__(self, admission, delegate, max_output_tokens):
        self.admission = admission
        self.delegate = delegate
        self.max_output_tokens = (delegate.max_output_tokens if delegate is not None
                                  else max_output_tokens)
        self._dispatched = False

    @property
    def dispatched(self):
        return self.delegate.dispatched if self.delegate is not None else self._dispatched

    def mark_dispatched(self):
        if self.delegate is not None:
            self.delegate.mark_dispatched()
        self._dispatched = True

    def prepare_input(self, body):
        # Recompute from the sealed, translated input after every dispatcher
        # wait. Canonical-body membership cannot prove physical consumption.
        self.admission.matches = None
        matches = matched_ids(self.admission.task, body)
        try:
            validate_matches(self.admission.task, matches)
        except AbortedError:
            raise
        except Exception as error:
            raise MessageInputScopeError('Mailbox input scope is no longer available') from error
        if self.delegate is not None:
            prepare = getattr(self.delegate, 'prepare_input', None)
            if callable(prepare):
                prepare(body)
        self.admission.matches = matches

    def confirm_input(self, *, successful):
        if self.delegate is not None:
            confirm = getattr(self.delegate, 'confirm_input', None)
            if callable(confirm):
                confirm(successful=successful)

    def settle(self, usage=None, *, unsent=False):
        if self.delegate is not None:
            self.delegate.settle(usage, unsent=unsent)


class ReceiptAdmission:
    """Request-local observer composed at the canonical prepared-input seam."""

    def __init__(self, task, budget_admit):
        self.task = task
        self.budget_admit = budget_admit
        self.matches = None

    def __call__(self, **counts):
        delegate = self.budget_admit(**counts) if self.budget_admit is not None else None
        return _ReceiptLease(self, delegate, counts['max_output_tokens'])


def receipt_admission(task: dict, budget_admit):
    if not task.get('_messageInputWitnesses'):
        return None
    return ReceiptAdmission(task, budget_admit)


def remember(task: dict, messages: list[dict]) -> None:
    witnesses = []
    for message in messages:
        content = message.get('content')
        if not isinstance(content, str) or not content:
            continue
        for family, id_key in (('swarm', '_swarmMessageIds'), ('project', '_projectMessageIds')):
            ids = message.get(id_key)
            if ids:
                mailbox_key = '_swarmMessageMailbox' if family == 'swarm' else '_projectMailbox'
                witnesses.append({'family': family, 'ids': list(ids), 'text': content,
                                  'mailbox': message.get(mailbox_key)})
    task['_messageInputWitnesses'] = witnesses


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key in ('content', 'text', 'parts', 'messages', 'input', 'instructions', 'system', 'prompt'):
            if key in value:
                yield from _strings(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def matched_ids(task: dict, body: dict) -> dict:
    matched = {'swarm': [], 'project': []}
    witnesses = task.get('_messageInputWitnesses') or []
    if not witnesses:
        return matched
    remaining = list(witnesses)
    # Provider input fields only: tools, metadata, routing and diagnostics are
    # not proof that the model received an injected mailbox carrier.
    for text in _strings(body):
        next_remaining = []
        for witness in remaining:
            if witness['text'] in text:
                matched[witness['family']].extend(witness['ids'])
            else:
                next_remaining.append(witness)
        remaining = next_remaining
        if not remaining:
            break
    return {key: list(dict.fromkeys(ids)) for key, ids in matched.items()}


def validate_matches(task: dict, matches: dict) -> None:
    witnesses = task.get('_messageInputWitnesses') or []
    if matches['swarm']:
        from lib.swarm.run_messages import validate_target, RunMessageRepository
        binding = task.get('_swarmMessageBinding')
        if not binding:
            raise PermissionError('Restored child message has no current run authority')
        validate_target(binding)
        key = RunMessageRepository(binding).key
        if any(w['family'] == 'swarm' and set(w['ids']) & set(matches['swarm'])
               and w.get('mailbox') != key for w in witnesses):
            raise PermissionError('Restored child message belongs to another run')
    if matches['project']:
        from lib.conversations.project_messages import identity, scope, ProjectMailbox
        owner, recipient, project = identity(task)
        repository = ProjectMailbox(owner, project, recipient)
        if any(w['family'] == 'project' and set(w['ids']) & set(matches['project'])
               and w.get('mailbox') != repository.key for w in witnesses):
            raise PermissionError('Restored project message belongs to another mailbox')
        rows = repository.load()[0]['messages']
        found = {row['id'] for row in rows if row['id'] in matches['project']}
        if found != set(matches['project']):
            raise PermissionError('Restored project message has no durable original')
        for row in rows:
            if row['id'] in found:
                scope(owner, row['sender'], project)


def stream_with_receipts(stream, task: dict, body: dict, **kwargs):
    from lib.llm.stream_result import ensure_provider_stream_result
    task.pop('_verifiedMessageInputIds', None)
    task.pop('_messageInputPhysicalIds', None)
    matches = matched_ids(task, body)
    validate_matches(task, matches)
    try:
        result = stream(task, body, **kwargs)
        if ensure_provider_stream_result(result).is_verified_complete:
            # Named non-transport/test seams retain canonical matching. The
            # production manager always supplies the selected physical input.
            task['_verifiedMessageInputIds'] = task.get('_messageInputPhysicalIds', matches)
        return result
    finally:
        task.pop('_messageInputPhysicalIds', None)
