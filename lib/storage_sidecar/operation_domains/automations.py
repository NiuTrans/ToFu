"""Automation occurrence admission, fenced progress and atomic owner cancellation."""
from lib.storage_sidecar.operations import OperationSpec
from lib.storage_sidecar.operations_pkg._automations import (
    _automation_run_admit, _automation_run_advance, _automation_run_cancel,
)
from lib.storage_sidecar.operations_pkg._automation_definitions import _automation_definition_update

from lib.storage_sidecar.operations_pkg._automation_events import _automation_event_settle
from lib.storage_sidecar.operations_pkg._automation_approval import _automation_run_approve

from lib.storage_sidecar.operations_pkg._automation_projects import _automation_project_update
from lib.storage_sidecar.operations_pkg._automation_skill_sources import _automation_skill_source_update

OPERATIONS = {
    'automation.skills.source.update': OperationSpec('command',False,_automation_skill_source_update),
    'automation.project.update': OperationSpec('command',False,_automation_project_update),
    'automation.run.approve': OperationSpec('command', False, _automation_run_approve),
    'automation.event.settle': OperationSpec('command', False, _automation_event_settle),
    'automation.definition.update': OperationSpec('command', False, _automation_definition_update),
    'automation.run.admit': OperationSpec('command', False, _automation_run_admit),
    'automation.run.advance': OperationSpec('command', False, _automation_run_advance),
    'automation.run.cancel': OperationSpec('command', False, _automation_run_cancel),
}
