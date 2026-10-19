"""Read-only owner requirement identity carried by the shared child factory.

Entry points: requirement_source, snapshot_requirement_source,
restore_requirement_source. A frozen source is context, never lifecycle or
permission authority. Final admission revalidates its current owned Attempt
through acceptance.turns. Persist only through the existing assignment snapshot.
Dependencies: identity, task principal validation, standard dataclasses.
"""
from dataclasses import asdict, dataclass

from lib.identity import PrincipalContext


@dataclass(frozen=True, slots=True)
class TurnRequirementSource:
    owner: int
    tenant: str
    conversation: str
    turn: str
    attempt: str
    task: str

    @property
    def identity(self):
        return self.conversation, self.turn, self.attempt, self.task

    def validate(self, consumer):
        from lib.tasks_pkg.manager import task_user_id
        owner = task_user_id(consumer)
        principal = PrincipalContext.from_payload(consumer['_principalContext'])
        if (type(self.owner) is not int or self.owner != owner
                or self.tenant != str(principal.tenant_id or '')
                or self.tenant != str(consumer.get('_tenant_id') or '')
                or self.conversation != consumer.get('convId')
                or any(not isinstance(value, str) or not value for value in self.identity)):
            raise PermissionError('Task requirement source scope mismatch')
        return self


def requirement_source(task):
    """Resolve trusted invocation metadata; never inspect request configuration."""
    if '_turnRequirementSource' in task:
        source = task['_turnRequirementSource']
        if not isinstance(source, TurnRequirementSource):
            raise ValueError('Task requirements require an issued source identity')
        return source.validate(task)
    if task.get('_transientRuntime') or not task.get('_turnId'):
        return None
    from lib.tasks_pkg.manager import task_user_id
    owner = task_user_id(task)
    principal = PrincipalContext.from_payload(task['_principalContext'])
    return TurnRequirementSource(owner, str(principal.tenant_id or ''),
        task.get('convId'), task.get('_turnId'), task.get('_attemptId'), task.get('id')).validate(task)


def snapshot_requirement_source(task):
    source = requirement_source(task)
    return asdict(source) if source is not None else None


def restore_requirement_source(task, snapshot):
    """Only the owned frozen-assignment loader calls this host composition port."""
    if not isinstance(snapshot, dict) or set(snapshot) != set(TurnRequirementSource.__dataclass_fields__):
        raise ValueError('Invalid frozen task requirement source')
    task['_turnRequirementSource'] = TurnRequirementSource(**snapshot).validate(task)
