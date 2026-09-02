"""Orchestration and Swarm operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'orchestration.definition.list': ops.OperationSpec(
        'query', False, ops._orchestration_definition_list),
    'orchestration.definition.get': ops.OperationSpec(
        'query', False, ops._orchestration_definition_get),
    'orchestration.definition.create': ops.OperationSpec(
        'command', True, ops._orchestration_definition_create),
    'orchestration.definition.update': ops.OperationSpec(
        'command', True, ops._orchestration_definition_update),
    'orchestration.definition.delete': ops.OperationSpec(
        'command', True, ops._orchestration_definition_delete),
    'orchestration.run.create': ops.OperationSpec(
        'command', True, ops._orchestration_run_create),
    'orchestration.run.get': ops.OperationSpec(
        'query', False, ops._orchestration_run_get),
    'orchestration.run.list': ops.OperationSpec(
        'query', False, ops._orchestration_run_list),
    'orchestration.run.update_status': ops.OperationSpec(
        'command', True, ops._orchestration_run_update),
    'orchestration.run.retire_interrupted': ops.OperationSpec(
        'command', True, ops._orchestration_run_retire),
    'orchestration.run.retire_interrupted_all': ops.OperationSpec(
        'maintenance', False, ops._orchestration_run_retire_all),
    'orchestration.event.append': ops.OperationSpec(
        'command', False, ops._orchestration_event_append),
    'orchestration.event.project': ops.OperationSpec(
        'command', False, ops._orchestration_event_project),
    'orchestration.event.page': ops.OperationSpec(
        'query', False, ops._orchestration_event_page),
    'orchestration.run.delete': ops.OperationSpec(
        'command', True, ops._orchestration_run_delete),
    'swarm.session.save': ops.OperationSpec('command', True, ops._swarm_session_save),
    'swarm.session.terminate': ops.OperationSpec(
        'command', True, ops._swarm_session_terminate),
    'swarm.session.delete': ops.OperationSpec(
        'command', True, ops._swarm_session_delete),
    'swarm.agent.save': ops.OperationSpec('command', True, ops._swarm_agent_save),
    'swarm.agents.mark_delivered': ops.OperationSpec(
        'command', True, ops._swarm_agents_mark_delivered),
    'swarm.session.get': ops.OperationSpec('query', False, ops._swarm_session_get),
    'swarm.resumable.list': ops.OperationSpec(
        'query', False, ops._swarm_resumable_list),
}

__all__ = ['OPERATIONS']
