"""Existing automation action adapter for finite human standing responsibility.

No PrincipalContext.user is constructed here: the automation system principal
proves the frozen publication binding; the application restores the original
human from its private captured payload and checks live source/owner/Stop gates.
"""
from lib.automations.execution import ActionAdapter, ExecutionRegistry
from lib.automations.registry import builtin_registry
from .responsibilities import ProjectResponsibilities


def responsibility_adapter(*, client_factory, service_factory=None, now_ms=None):
    def invoke(invocation, *, reconcile=False):
        context, arguments = invocation.context, invocation.arguments
        principal = context.principal
        identity = arguments['responsibilityId']
        if principal.kind != 'system' or principal.subject_id != f'automation:{principal.require_owner()}:{identity}':
            raise PermissionError('Responsibility action requires its exact existing automation publication')
        context.checkpoint(allow_cancelled=True)
        if service_factory is None:
            service = ProjectResponsibilities(principal.require_owner(), arguments['projectPath'],
                client=client_factory, clock=(lambda: now_ms()/1000) if now_ms else None)
        else:
            service = service_factory(principal.require_owner(), arguments['projectPath'])
        row = service._row(identity)
        if row['view']['tenant'] != principal.tenant_id:
            raise PermissionError('Responsibility automation tenant changed')
        status = service.run_occurrence(identity, invocation.effect_key, reconcile=reconcile,
                                       cancelling=context.cancellation_requested)
        if status == 'unknown':
            return None
        return {'responsibilityId': identity, 'occurrenceId': invocation.effect_key,
                'status': {'active': 'running', 'completed': 'succeeded'}.get(status, status)}
    return ActionAdapter(builtin_registry().get('project.responsibility.run'), invoke,
                         lambda invocation: invoke(invocation, reconcile=True))


def responsibility_registry(*, client_factory, now_ms=None):
    from lib.automations.presentation import show_task_start_time
    return ExecutionRegistry([
        ActionAdapter(builtin_registry().get('task.show_time'),
            lambda invocation: show_task_start_time(invocation.arguments, invocation.event)),
        responsibility_adapter(client_factory=client_factory, now_ms=now_ms),
    ])
