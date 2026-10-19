"""Explicit human consult assignments over the existing conversation executor.

This service creates no worker/scheduler. Stable command identity delegates birth,
idle/human-lane priority, cancellation and replay to conversation_sync. Result and
review are distinct; acceptance never claims file publication/canonical delivery.
"""
from __future__ import annotations
import time
from copy import deepcopy

from lib.identity import PrincipalContext
from lib.turn_initiation import INITIATOR_BRAIN, stamp_initiator
from .admission import TrustedDispatch


class ProjectDispatchService:
    def __init__(self, repository, *, commands=None, turn_get=None, attempt_get=None,
                 workspace_get=None, publication_lock=None, publication_wake=None):
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
        return self._dispatch(self.repository.validate_own(binding))

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

    def view_for_coordinator(self, assignment_id, *, scope):
        """Passive evidence, including expired/stopped work, for the live role."""
        assignment = self.repository.read(assignment_id)
        if (scope['user_id'] != self.repository.owner or scope['project_path'] != self.repository.project
                or assignment['request']['coordinatorId'] != scope['conversation_id']):
            raise PermissionError('Assignment belongs to another Coordinator')
        self.repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
        self.repository.validate_grant_binding(assignment)
        return self._view(assignment)

    def review_for_coordinator(self, task, scope, arguments):
        """Coordinator judgment is distinct from human review and actual delivery."""
        from .delegation import review_worker
        with self._publication_lock():
            self.view_for_coordinator(arguments['assignment_id'], scope=scope)
            assignment = review_worker(self.repository, task, arguments)
            if arguments.get('checkpoint_sha'):
                attempt = self.repository.get_attempt(assignment['execution']['attemptId'])
                self.repository.validate_publication(self.repository.admission(assignment['id']), attempt['taskId'])
                wake = self.publication_wake
                if wake is None:
                    from lib.integration_control import _start_or_wake_worker
                    wake = _start_or_wake_worker
                wake()
        return self.view_for_coordinator(assignment['id'], scope=scope)

    def _view(self, assignment):
        association = assignment['execution']
        execution = None

        attempt = None
        if association is not None:
            request = assignment['request']
            turn = self.turn_get(request['recipientId'], association['turnId'],
                                 user_id=assignment['grant']['owner'])
            attempt = self.attempt_get(association['attemptId'], user_id=assignment['grant']['owner'])
            if turn is not None and attempt is not None:

                if (turn.get('turnId') != association['turnId']
                        or turn.get('conversationId') != request['recipientId']
                        or turn.get('actor') != 'assistant'
                        or attempt.get('attemptId') != association['attemptId']
                        or attempt.get('turnId') != association['turnId']
                        or attempt.get('conversationId') != request['recipientId']):
                    raise ValueError('Bound project execution association changed')
                execution = {'turnId': association['turnId'], 'attemptId': association['attemptId'],
                    'taskId': attempt.get('taskId') or '', 'status': attempt.get('status') or 'unknown',
                    'resultRevision': turn.get('projectionRevision'),
                    'result': deepcopy(turn.get('projection') or {})}
        delivery = None
        if assignment['request'].get('profile', {}).get('mode') in ('files', 'project'):
            from .publication import delivery_view
            delivery = delivery_view(assignment, attempt,
                                     self.workspace_get or self.repository.get_workspace)
        dependency_view = ({'dependencyStates': self.repository.dependency_states(assignment)}
                           if 'dependencies' in assignment['request'] else {})
        return {**dependency_view, 'assignmentId': assignment['id'], 'request': deepcopy(assignment['request']),
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

    def history_page(self, *, principal, command_id=None, assignment_id=None, cursor=None):
        """One bounded owned page; exact command/assignment lookup stays passive."""
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
        page = self.repository.history_page(cursor)
        views = []
        for row in page['rows']:
            if row['grant']['tenant'] != principal.tenant_id:
                continue
            view = self._view(row)
            if view['execution'] is not None:
                content = view['execution']['result'].get('content')
                view['execution']['result'] = {'content': content[:2000]} if isinstance(content, str) else {}
            views.append(view)
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
            if execution is not None:
                cancel = getattr(self.commands, 'abort_project_attempt', None) or self.commands.abort_attempt
                cancel(execution['attemptId'], assignment['grant']['owner'])
        return self.view_for_coordinator(assignment['id'], scope=scope)

    def revoke(self, assignment_id, *, principal, expected_revision):
        assignment = self.repository.read(assignment_id)
        self._actor(principal, assignment)
        # A consult prerequisite can authorize a file dependent's publication.
        # All revocations linearize with the existing final publication lock.
        with self._publication_lock():
            execution = self.repository.revoke(assignment_id, expected_revision=expected_revision)
            if execution is not None:
                # Grant revocation and prepublication guards linearize under the
                # same existing lock; ACK loss only retries this bound cancel.
                cancel = getattr(self.commands, 'abort_project_attempt', self.commands.abort_attempt)
                cancel(execution['attemptId'], assignment['grant']['owner'])
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
            self.repository.request_publication(assignment_id, principal=principal,
                expected_revision=expected_revision, result_revision=result_revision,
                expected_checkpoint_sha=expected_checkpoint_sha)
            binding = self.repository.admission(assignment_id)
            self.repository.validate_publication(binding, execution['taskId'])
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
