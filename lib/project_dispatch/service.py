"""Explicit human consult assignments over the existing conversation executor.

This service creates no worker/scheduler. Stable command identity delegates birth,
idle/human-lane priority, cancellation and replay to conversation_sync. Result and
review are distinct; acceptance never claims file publication/canonical delivery.
HistoryPreviews keeps one owner/project's previous history-page result previews,
reused only while the Turn's projection revision and public status are unchanged.
A history page lists historyRow projections; exact command/assignment lookups
return complete views. A page build whose requester is gone stops before its next
row with HistoryBuildCancelled.
"""
from __future__ import annotations
from collections.abc import Mapping
import threading
import time
from copy import deepcopy

from lib.identity import PrincipalContext
from lib.log import get_logger
from lib.turn_initiation import INITIATOR_BRAIN, stamp_initiator
from .admission import TrustedDispatch
from .repository import CONTRACT, ProjectDispatchError

logger = get_logger(__name__)

# The list projection of a request: exact lookups keep every request field.
HISTORY_REQUEST_FIELDS = tuple(CONTRACT['$defs']['historyRequest']['properties'])


class HistoryBuildCancelled(RuntimeError):
    """The page's requester is gone; the build stopped before reading another row."""


class FollowUpRefused(ProjectDispatchError):
    """The Worker session cannot take this follow-up now; nothing was written or started."""
    code = 'follow_up_refused'
    execution_started = False


class HistoryPreviews:
    """Result previews of the previous history page, one page per owner/project.

    A Turn's public projection is fenced by its projectionRevision; the public
    status selects live or settled read normalization of that stored revision.
    A page reuses a preview only for that exact Turn, revision and status, after
    a live metadata read. Attempt, association, grant, tenant and delivery
    evidence are still read on every build. Each build replaces its slot with
    the previews it displayed, so retention is one contract page per
    owner/project rather than a tuned size or age.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pages = {}

    def previous(self, owner, project):
        with self._lock:
            return self._pages.get((owner, project), {})

    def replace(self, owner, project, previews):
        with self._lock:
            if previews:
                self._pages[(owner, project)] = previews
            else:
                self._pages.pop((owner, project), None)


# Shared across requests only where a composition root passes it explicitly.
PROCESS_HISTORY_PREVIEWS = HistoryPreviews()


def _preview_version(turn):
    return turn.get('projectionRevision'), turn.get('status')


class ProjectDispatchService:
    def __init__(self, repository, *, commands=None, turn_get=None, attempt_get=None,
                 workspace_get=None, publication_lock=None, publication_wake=None,
                 history_previews=None, publication_release=None):
        self.repository = repository
        if commands is None:
            from lib.conversation_sync.runtime import conversation_turn_commands
            commands = conversation_turn_commands
        if turn_get is None or attempt_get is None:
            from lib.turn_lifecycle import get_turn, get_attempt
            turn_get = turn_get or get_turn
            attempt_get = attempt_get or get_attempt
        self.commands, self.turn_get, self.attempt_get = commands, turn_get, attempt_get

        self.workspace_get = workspace_get

        if workspace_get is not None:
            self.repository.workspace_get = workspace_get
        self.publication_lock = publication_lock
        self.publication_wake = publication_wake
        self.publication_release = publication_release
        self.history_previews = HistoryPreviews() if history_previews is None else history_previews

    def _publication_lock(self):
        if self.publication_lock is not None:
            return self.publication_lock(self.repository.project)
        from lib.integration_control import _repo_lock
        return _repo_lock(self.repository.project)
    def _actor(self, principal, assignment):
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.require_owner() != assignment['grant']['owner']
                or principal.tenant_id != assignment['grant']['tenant']):
            raise PermissionError('Project assignment requires its authorized human owner and tenant')
        principal.require_scope('chat')

    def _continuation(self, assignment):
        trusted = TrustedDispatch(self.repository.admission(assignment['id']))
        execution = assignment['execution']
        outcome = self.commands.resume_turn(
            assignment['request']['recipientId'], execution['turnId'], assignment['grant']['owner'], {
                'commandId': 'project-dispatch-resume:' + assignment['id'] + ':' + execution['attemptId'],
                'operation': 'continue',
                'expectedProjectionRevision': self.turn_get(
                    assignment['request']['recipientId'], execution['turnId'],
                    user_id=assignment['grant']['owner'])['projectionRevision'],
                'config': {},
            }, request_started_at=time.time(), principal=trusted.principal,
            trusted_dispatch=trusted)
        return {**self._view(self.repository.read(assignment['id'])),
                'dispatch': 'aborted' if outcome.value.get('aborted') else 'accepted'}

    def continues_in_place(self, assignment):
        """Whether a follow-up continues ``assignment`` itself rather than a new one.

        A review is immutable, and a delivered receipt is final for its work id
        (its checkout may already be reclaimed): either ends the assignment's Turns.
        """
        execution = assignment['execution']
        if assignment['review'] is not None:
            return False
        if execution is None:
            return True
        from .publication import delivery_final
        attempt = self.attempt_get(execution['attemptId'], user_id=assignment['grant']['owner'])
        return not delivery_final(assignment, attempt, self.workspace_get or self.repository.get_workspace)

    def follow_up(self, assignment_id, *, turn_id, attempt_id, expected_revision, instruction, command_id):
        """Continue an ended Worker session with a Coordinator instruction as its next Turn.

        ``turn_id``, ``attempt_id`` and ``expected_revision`` pin the settled result
        the Coordinator read. The new Turn follows exactly that Turn and continues
        the same assignment, grant, checkout and account from its association. The
        instruction is Coordinator context, never a human message. Replaying
        ``command_id`` reports its accepted Turn and starts nothing new.
        """
        binding = self.repository.admission(assignment_id)
        assignment = self.repository.validate(binding)
        owner, request = assignment['grant']['owner'], assignment['request']
        execution = assignment['execution'] or {}
        follows = None
        if execution.get('turnId') != turn_id or execution.get('attemptId') != attempt_id:
            current = self.attempt_get(execution['attemptId'], user_id=owner) if execution else None
            if not isinstance(current, Mapping) or current.get('commandId') != command_id:
                raise FollowUpRefused(
                    'This Worker session moved on since you read it: its assignment now continues '
                    f"from Turn {execution.get('turnId')}. Read get_conversation_status and pin its "
                    'latest settled Turn.')
            if current.get('status') != 'pending' or current.get('taskId'):
                # This follow-up was accepted and launched; report it, start nothing.
                return {'turn': self.turn_get(request['recipientId'], execution['turnId'],
                                              user_id=owner, include_projection=False),
                        'attempt': current, 'idempotentReplay': True}
        else:
            previous = self.attempt_get(attempt_id, user_id=owner)
            if not isinstance(previous, Mapping) or previous.get('status') in ('pending', 'running'):
                raise FollowUpRefused(
                    'The Worker is still running this assignment. follow_up continues a settled '
                    'session; send_project_message reaches a running Worker at its next step.')
            turn = self.turn_get(request['recipientId'], turn_id, user_id=owner, include_projection=False)
            if not isinstance(turn, Mapping) or turn.get('projectionRevision') != expected_revision:
                raise FollowUpRefused('The Worker result changed since you read it; read '
                                      'get_conversation_status and pin its current projectionRevision.')
            follows = {'execution': deepcopy(execution), 'source': {
                'attemptId': attempt_id, 'turnId': turn_id, 'taskId': str(previous.get('taskId') or ''),
                'status': previous['status']}}
        trusted = TrustedDispatch(binding, follows=follows)
        from .delegation import follow_up_input
        projection = stamp_initiator({
            'role': 'user', 'content': follow_up_input(instruction),
            '_projectAssignmentId': assignment_id,
            '_projectAuthorizationSource': deepcopy(request['authorizationSource']),
        }, INITIATOR_BRAIN)
        outcome = self.commands.create_turn(request['recipientId'], owner, {
            'commandId': command_id, 'inputActor': 'virtual_user', 'inputKind': 'input',
            'inputTurn': projection, 'config': {},
            'parentTurnId': turn_id, 'requireParentIsLaneTail': True,
        }, request_started_at=time.time(), principal=trusted.principal, trusted_dispatch=trusted)
        return outcome.value

    def _check_rework(self, request, principal):
        if request.get('reworkOf') is not None:
            reference = request['reworkOf']
            old = self.view(reference['assignmentId'], principal=principal)
            if (old['grantRevision'] != reference['grantRevision'] or old['review'] is None
                    or old['review']['decision'] != 'rework' or old['execution'] is None
                    or old['execution']['status'] != 'completed'
                    or old['execution']['resultRevision'] != reference['resultRevision']):
                raise ValueError('Linked rework requires the exact current reviewed result')

    def authorize_and_dispatch(self, request, *, principal, command_id):
        self._check_rework(request, principal)
        assignment = self.repository.authorize(request, principal=principal, command_id=command_id)
        return self.dispatch(assignment['id'], principal=principal)

    def authorize_batch_and_dispatch(self, request, *, principal, command_id):
        """Commit the entire human-approved fan-out before any executor birth.

        Each child remains an ordinary immutable grant. Starting conversations
        is not atomic; unknown replies retain exact command identity, never a
        second grant. Ordinary lane-idle/startup recovery owns deferred births.
        """
        from .repository import batch_requests
        requests = batch_requests(request)
        if self.repository.find_batch(command_id) is None:
            for child_request in requests:
                self._check_rework(child_request, principal)
        batch = self.repository.authorize_batch(request, principal=principal, command_id=command_id)
        return self.dispatch_batch(batch['id'], principal=principal)

    def _batch_actor(self, batch, principal):
        for assignment_id in batch['assignmentIds']:
            self._actor(principal, self.repository.read(assignment_id))

    def view_batch(self, batch_id, *, principal):
        """Demand-only evidence; no wake, reconciliation write or executor birth."""
        batch = self.repository.read_batch(batch_id)
        self._batch_actor(batch, principal)
        return {'batchId': batch['id'], 'request': deepcopy(batch['request']),
                'assignments': [self.view(identity, principal=principal)
                                for identity in batch['assignmentIds']]}

    def batch_receipt(self, *, principal, command_id):
        """Read the original scoped command, including after a lost create ACK."""
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.require_owner() != self.repository.owner):
            raise PermissionError('Batch evidence requires its owning human principal')
        principal.require_scope('chat')
        batch = self.repository.find_batch(command_id)
        if batch is None:
            return None  # Absence is not proof no write is in flight.
        if any(self.repository.read(identity)['grant']['tenant'] != principal.tenant_id
               for identity in batch['assignmentIds']):
            return None
        return self.view_batch(batch['id'], principal=principal)

    def dispatch_batch(self, batch_id, *, principal):
        """Admit independent asynchronous turns through the existing service.

        No threadpool or wait-for-result is added. An uncertain launch must not
        starve other approved lanes or be described as a definite rejection.
        Per-child Stop, human priority, budget and binding checks stay live.
        """
        batch = self.repository.read_batch(batch_id)
        self._batch_actor(batch, principal)
        dispatches = []
        for assignment_id in batch['assignmentIds']:
            try:
                outcome = self.dispatch(assignment_id, principal=principal)
                status = outcome['dispatch']
                reason = ''
            except Exception:
                from lib.log import get_logger
                get_logger(__name__).warning('Batch dispatch unconfirmed for assignment %s', assignment_id)
                status, reason = 'unknown', 'dispatch_unconfirmed'
            dispatches.append({'assignmentId': assignment_id, 'status': status, 'reason': reason})
        return {**self.view_batch(batch_id, principal=principal), 'dispatches': dispatches}

    def dispatch(self, assignment_id, *, principal):
        assignment = self.repository.read(assignment_id)
        self._actor(principal, assignment)

        return self._dispatch(assignment)

    def dispatch_authorized(self, binding):
        assignment = self.repository.validate_own(binding)
        if assignment['execution'] is None:
            return self._dispatch(assignment)
        execution = assignment['execution']
        attempt = self.repository.get_attempt(execution['attemptId'])
        if (not isinstance(attempt, Mapping) or attempt.get('status') in ('pending', 'running')
                or not self.repository.restart_paused(assignment)):
            return self._dispatch(assignment)
        return self._continuation(assignment)

    def view_authorized(self, binding):
        """Passive evidence for an exact existing grant; never creates authority."""
        return self._view(self.repository.validate_own(binding))

    def _dispatch(self, assignment):
        assignment_id = assignment['id']
        binding = self.repository.admission(assignment_id)

        assignment = self.repository.validate_own(binding)
        if any(state['status'] != 'ready' for state in self.repository.dependency_states(assignment)):
            assignment = self.repository.validate_own(binding)
            return {**self._view(assignment), 'dispatch': 'deferred'}
        trusted = TrustedDispatch(binding)
        request = assignment['request']
        content = request['brief']
        if request.get('delegation'):
            from .delegation import worker_input
            content = worker_input(self.repository, request)
        projection = stamp_initiator({
            'role': 'user', 'content': content,
            '_projectAssignmentId': assignment_id,
            '_projectAuthorizationSource': deepcopy(request['authorizationSource']),
        }, INITIATOR_BRAIN)
        try:
            outcome = self.commands.create_turn(request['recipientId'], binding['owner'], {
                'commandId': 'project-dispatch:' + assignment_id,
                'inputActor': 'virtual_user', 'inputKind': 'input',
                'inputTurn': projection, 'config': {},
            }, request_started_at=time.time(), principal=trusted.principal, trusted_dispatch=trusted)
        except Exception as error:
            from lib.turn_lifecycle import LifecycleConflict
            if isinstance(error, LifecycleConflict) and error.code in ('lane_busy', 'superseded_by_human'):
                return {**self._view(self.repository.validate_own(binding)), 'dispatch': 'deferred'}
            raise
        # The typed accepted binding is written before executor birth. A lost
        # response can retry this exact command; it cannot buy a second task.
        return {**self._view(self.repository.read(assignment_id)),
                'dispatch': 'aborted' if outcome.value.get('aborted') else 'accepted'}

    def view(self, assignment_id, *, principal):
        assignment = self.repository.read(assignment_id)
        self._actor(principal, assignment)

        return self._view(assignment)

    def view_for_coordinator(self, assignment_id, *, scope, settlement_only=False):
        """Passive evidence, including expired/stopped work, for the live role.

        settlement_only keeps every authority check but reads Turn metadata
        instead of the projection and skips dependency evidence: execution
        status and the delivery receipt for waits, never a presentable result.
        """
        assignment = self.repository.read(assignment_id)
        if (scope['user_id'] != self.repository.owner or scope['project_path'] != self.repository.project
                or assignment['request']['coordinatorId'] != scope['conversation_id']):
            raise PermissionError('Assignment belongs to another Coordinator')
        self.repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
        self.repository.validate_grant_binding(assignment)
        return self._view(assignment, settlement_only=settlement_only)

    def review_for_coordinator(self, task, scope, arguments):
        """Coordinator judgment is distinct from human review and actual delivery."""
        from .delegation import review_worker
        with self._publication_lock():
            self.view_for_coordinator(arguments['assignment_id'], scope=scope, settlement_only=True)
            assignment = review_worker(self.repository, task, arguments)
            if arguments.get('checkpoint_sha'):
                work_id = self.repository.workspace_task_id(assignment)
                self.repository.validate_publication(self.repository.admission(assignment['id']), work_id)
                self._release_publication_hold(work_id)
                wake = self.publication_wake
                if wake is None:
                    from lib.integration_control import _start_or_wake_worker
                    wake = _start_or_wake_worker
                wake()
        # A receipt needs the decision, revisions and delivery, never the
        # Worker projection (MBs for a tool-heavy Turn).
        return self.view_for_coordinator(assignment['id'], scope=scope, settlement_only=True)

    def _release_publication_hold(self, task_id):
        """Retire the receipt's pre-pin hold under this publication lock.

        Views and delivery waits then report the delivery in progress instead
        of the earlier refusal. The receiver still delivers and rechecks every
        fence; a failed release only leaves that to its next pass.
        """
        release = self.publication_release
        if release is None:
            from lib.integration_control import release_publication_hold as release
        try:
            release(self.repository.project, task_id, user_id=self.repository.owner)
        except Exception:
            logger.warning('[ProjectDispatch] publication hold release left to the receiver '
                           'task=%s', task_id, exc_info=True)

    def _bound_turn(self, identity, owner, *, settlement_only, previews):
        """Return the bound Turn plus the exact remembered preview it still has.

        A Worker projection grows with every round (MBs); metadata carries the
        identity and revision that settlement and a remembered preview need.
        """
        conversation_id, turn_id = identity
        if settlement_only:
            return self.turn_get(conversation_id, turn_id, user_id=owner, include_projection=False), None
        if previews is not None and identity in previews[0]:
            turn = self.turn_get(conversation_id, turn_id, user_id=owner, include_projection=False)
            remembered = previews[0][identity]
            if isinstance(turn, Mapping) and _preview_version(turn) == remembered[0]:
                return turn, remembered
        return self.turn_get(conversation_id, turn_id, user_id=owner), None

    def _view(self, assignment, *, settlement_only=False, previews=None, snapshot=None):
        """previews: a history page's (previous, current) maps of exact previews.

        Only a history row passes them; that row also carries the contract's
        historyRequest projection of the request instead of the complete one.
        snapshot: the page's loaded dispatch value; dependency states read the
        row's prerequisites from it instead of reloading them.
        """
        request = assignment['request']
        association = assignment['execution']
        execution = None

        attempt = None
        if association is not None:
            identity = (request['recipientId'], association['turnId'])
            turn, remembered = self._bound_turn(identity, assignment['grant']['owner'],
                                                settlement_only=settlement_only, previews=previews)
            attempt = self.attempt_get(association['attemptId'], user_id=assignment['grant']['owner'])
            if turn is not None and attempt is not None:

                if (turn.get('turnId') != association['turnId']
                        or turn.get('conversationId') != request['recipientId']
                        or turn.get('actor') != 'assistant'
                        or attempt.get('attemptId') != association['attemptId']
                        or attempt.get('turnId') != association['turnId']
                        or attempt.get('conversationId') != request['recipientId']):
                    raise ValueError('Bound project execution association changed')
                result = {}
                if previews is not None:
                    if remembered is None:
                        content = (turn.get('projection') or {}).get('content')
                        remembered = (_preview_version(turn),
                                      content[:2000] if isinstance(content, str) else None)
                    if type(remembered[0][0]) is int:
                        previews[1][identity] = remembered
                    result = {} if remembered[1] is None else {'content': remembered[1]}
                elif not settlement_only:
                    result = deepcopy(turn.get('projection') or {})
                execution = {'turnId': association['turnId'], 'attemptId': association['attemptId'],
                    'taskId': attempt.get('taskId') or '', 'status': attempt.get('status') or 'unknown',
                    'resultRevision': turn.get('projectionRevision'),
                    'result': result}
        delivery = None
        if assignment['request'].get('profile', {}).get('mode') in ('files', 'project'):
            from .publication import delivery_view
            delivery = delivery_view(assignment, attempt,
                                     self.workspace_get or self.repository.get_workspace)
        dependency_view = ({'dependencyStates': self.repository.dependency_states(assignment, value=snapshot)}
                           if 'dependencies' in assignment['request'] and not settlement_only else {})
        if previews is not None:
            request = {name: request[name] for name in HISTORY_REQUEST_FIELDS if name in request}
        return {**dependency_view, 'assignmentId': assignment['id'], 'request': deepcopy(request),
            'grantRevision': assignment['grant']['revision'], 'revoked': assignment['grant']['revoked'],
            'execution': execution, 'review': deepcopy(assignment['review']),
            'publication': deepcopy(assignment.get('publication')), 'delivery': delivery,
            'canonicalDelivery': bool(delivery is not None and delivery['status'] == 'delivered')}

    def list(self, *, principal, command_id=None):

        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.require_owner() != self.repository.owner):
            raise PermissionError('Assignment evidence requires its owning human principal')
        principal.require_scope('chat')
        if command_id is not None:
            row = self.repository.find_command(command_id)
            return ([self.view(row['id'], principal=principal)]
                    if row is not None and row['grant']['tenant'] == principal.tenant_id else [])
        return [self.view(row['id'], principal=principal) for row in self.repository.list()
                if row['grant']['tenant'] == principal.tenant_id]

    def history_page(self, *, principal, command_id=None, assignment_id=None, cursor=None,
                     cancelled=lambda: False):
        """One bounded owned page of historyRow projections.

        Exact command/assignment lookups stay passive and return complete views.
        ``cancelled`` reports that the requester is gone; it is checked before the
        page load and between rows, and a cancelled build keeps the previous
        previews slot.
        """
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.require_owner() != self.repository.owner):
            raise PermissionError('Assignment evidence requires its owning human principal')
        principal.require_scope('chat')
        if command_id is not None:
            return {'assignments': self.list(principal=principal, command_id=command_id),
                    'olderCursor': None, 'newerCursor': None}
        if assignment_id is not None:
            return {'assignments': [self.view(assignment_id, principal=principal)],
                    'olderCursor': None, 'newerCursor': None}
        if cancelled():
            raise HistoryBuildCancelled('Assignment history requester disconnected before the page load')
        page = self.repository.history_page(cursor)
        owner, project = self.repository.owner, self.repository.project
        previews = (self.history_previews.previous(owner, project), {})
        views = []
        for row in page['rows']:
            if cancelled():
                raise HistoryBuildCancelled('Assignment history requester disconnected during the build')
            if row['grant']['tenant'] != principal.tenant_id:
                continue
            views.append(self._view(row, previews=previews, snapshot=page['snapshot']))
        self.history_previews.replace(owner, project, previews[1])
        return {'assignments': views, 'olderCursor': page['olderCursor'], 'newerCursor': page['newerCursor']}

    def cancel_for_coordinator(self, task, scope, arguments):
        """Withdraw an owned worker under human steering or its original continuation."""
        from .repository import _validate_schema
        from .delegation import worker_control
        _validate_schema(arguments, 'cancelProjectWork')
        with self._publication_lock():
            assignment = self.repository.read(arguments['assignment_id'])
            worker_control(self.repository, task, assignment)
            revision = assignment['grant']['revision'] - int(assignment['grant']['revoked'])
            execution = self.repository.revoke(assignment['id'], expected_revision=revision)
            self._cancel_worker(assignment, execution, (
                f"Revoked by Coordinator conversation {task.get('convId') or '?'} "
                f"(task {task.get('id') or '?'}) calling cancel_project_work "
                f"on assignment {assignment['id']}."))
        return self.view_for_coordinator(assignment['id'], scope=scope, settlement_only=True)

    def _cancel_worker(self, assignment, execution, provenance):
        """Stop the bound attempt and every other live attempt in its Worker conversation.

        A turn that ran without the binding (an older unbound wake, human input)
        is unreachable through the association, so it gets the same grant
        revocation cause. A Coordinator continuation grant's recipient is the
        Coordinator conversation itself; its live turns are never swept.
        """
        owner, request = assignment['grant']['owner'], assignment['request']
        attempts = [execution['attemptId']] if execution is not None else []
        if request['recipientId'] != request['coordinatorId']:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            for task in chat_task_runtime.snapshot_owned(user_id=owner):
                attempt_id = task.get('_attemptId') or task.get('attemptId')
                if (task.get('convId') == request['recipientId'] and attempt_id
                        and task.get('status') in ('pending', 'running')
                        and not task.get('aborted') and attempt_id not in attempts):
                    attempts.append(attempt_id)
        for attempt_id in attempts:
            self._cancel_execution(attempt_id, owner, provenance)

    def _cancel_execution(self, attempt_id, owner, provenance):
        cancel = getattr(self.commands, 'abort_project_attempt', None)
        if cancel is None:
            return self.commands.abort_attempt(attempt_id, owner)
        return cancel(attempt_id, owner, provenance=provenance)

    def revoke(self, assignment_id, *, principal, expected_revision):
        assignment = self.repository.read(assignment_id)
        self._actor(principal, assignment)
        # A consult prerequisite can authorize a file dependent's publication.
        # All revocations linearize with the existing final publication lock.
        with self._publication_lock():
            execution = self.repository.revoke(assignment_id, expected_revision=expected_revision)
            # Grant revocation and prepublication guards linearize under the
            # same existing lock; ACK loss only retries this bound cancel.
            self._cancel_worker(
                assignment, execution,
                f"Revoked through the project assignment revoke API by "
                f"{getattr(principal, 'subject_id', None) or '?'} on assignment {assignment_id}.")
        return self.view(assignment_id, principal=principal)

    def publish(self, assignment_id, *, principal, expected_revision, result_revision, expected_checkpoint_sha):
        with self._publication_lock():
            view = self.view(assignment_id, principal=principal)
            execution = view['execution']
            if (execution is None or execution['status'] != 'completed'
                    or execution['resultRevision'] != result_revision or not execution['taskId']):
                raise ValueError('Publication requires the exact current completed executor result')
            if (view['delivery'] is None or not expected_checkpoint_sha
                    or view['delivery']['checkpointSha'] != expected_checkpoint_sha):
                raise ValueError('Publication checkpoint differs from the exact reviewed workspace')
            from pathlib import Path
            from .publication import require_publishable
            workspace = self.repository.get_workspace(
                str(Path(self.repository.project).expanduser().resolve()),
                view['delivery']['taskId'], user_id=self.repository.owner)
            require_publishable(workspace or {}, execution['taskId'])
            self.repository.request_publication(assignment_id, principal=principal,
                expected_revision=expected_revision, result_revision=result_revision,
                expected_checkpoint_sha=expected_checkpoint_sha)
            binding = self.repository.admission(assignment_id)
            # The delivery names the checkout's work id; the executor ran as its own task.
            self.repository.validate_publication(binding, view['delivery']['taskId'])
            self._release_publication_hold(view['delivery']['taskId'])
            wake = self.publication_wake
            if wake is None:
                from lib.integration_control import _start_or_wake_worker
                wake = _start_or_wake_worker
            wake()
        return self.view(assignment_id, principal=principal)

    def review(self, assignment_id, *, principal, decision, note, result_revision, expected_revision):
        view = self.view(assignment_id, principal=principal)
        result = view['execution']
        if (result is None or result['status'] != 'completed'
                or result['resultRevision'] != result_revision):
            raise ValueError('Review requires the current completed execution result')
        self.repository.record_review(assignment_id, decision, note, result_revision, expected_revision)

        if decision == 'accept':
            # Only a successful review CAS (including durable replay) emits a
            # hint. Existing grants/birth guards own the cross-lane retry.
            from .recovery import wake_project_dependents
            wake_project_dependents(self.repository.owner, self.repository.project,
                                    assignment_id, repository=self.repository, service=self)
        return self.view(assignment_id, principal=principal)
