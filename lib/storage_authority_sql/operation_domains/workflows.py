"""Orchestration and Swarm operation registrations."""

from lib.storage_authority_sql import operations as ops


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
    'goal.run.start': ops.OperationSpec(
        'command', True, ops._goal_run_start),
    'goal.run.transition': ops.OperationSpec(
        'command', True, ops._goal_run_transition),
    'goal.run.get': ops.OperationSpec(
        'query', False, ops._goal_run_get),
    'goal.run.latest': ops.OperationSpec(
        'query', False, ops._goal_run_latest),
    'swarm.session.save': ops.OperationSpec('command', True, ops._swarm_session_save),
    'swarm.session.terminate': ops.OperationSpec(
        'command', True, ops._swarm_session_terminate),
    'swarm.session.quarantine_ownerless': ops.OperationSpec(
        'command', True, ops._swarm_session_quarantine_ownerless),
    'swarm.session.delete': ops.OperationSpec(
        'command', True, ops._swarm_session_delete),
    'swarm.agent.save': ops.OperationSpec('command', True, ops._swarm_agent_save),
    'swarm.agent.append': ops.OperationSpec('command', True, ops._swarm_agent_append),
    'swarm.agent.checkpoint.mark': ops.OperationSpec(
        'command', True, ops._swarm_agent_checkpoint_mark),
    'swarm.agents.mark_delivered': ops.OperationSpec(
        'command', True, ops._swarm_agents_mark_delivered),
    'swarm.agents.acknowledge_results': ops.OperationSpec(
        'command', True, ops._swarm_agents_acknowledge_results),
    'swarm.session.get': ops.OperationSpec('query', False, ops._swarm_session_get),
    'swarm.agents.page': ops.OperationSpec('query', False, ops._swarm_agents_page),
    'swarm.agent.state.get': ops.OperationSpec(
        'query', False, ops._swarm_agent_state_get),
    'swarm.resumable.list': ops.OperationSpec(
        'query', False, ops._swarm_resumable_list),
}

__all__ = ['OPERATIONS']
