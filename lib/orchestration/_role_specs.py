"""Backend-owned role FieldSpecs, bounds and field validation."""

from __future__ import annotations

from lib.orchestration.field_spec_contract import (
    VALID_PARAM_KINDS as VALID_PARAM_KINDS,
    field_spec,
)
from lib.orchestration.field_spec_validation import validate_field_specs


MAX_OBJECTIVE_LEN = 4000
MAX_LIST_ITEMS = 20
MAX_LIST_ITEM_LEN = 500


def _f(key, kind, label, *, heading=None, options=None, placeholder=None,
       **metadata):
    if kind in {'text', 'textarea'}:
        metadata.setdefault('maxLength', MAX_OBJECTIVE_LEN)
    elif kind == 'list':
        metadata.setdefault('maxItems', MAX_LIST_ITEMS)
        metadata.setdefault('maxItemLength', MAX_LIST_ITEM_LEN)
    return field_spec(
        key, kind, label,
        heading=heading,
        options=options,
        placeholder=placeholder,
        **metadata,
    )


def _objective_field(label, placeholder=None):
    return _f('objective', 'textarea', label, heading='Task',
              placeholder=placeholder)


_GENERIC_ROLE_SCHEMA = [
    _objective_field('orch.field.task', 'orch.ph.task'),
    _f('expected_outcome', 'textarea', 'orch.field.expectedOutcome',
       heading='Expected Outcome', placeholder='orch.ph.expectedOutcome'),
]

ROLE_PARAM_SCHEMA = {
    'critic': [
        _objective_field('orch.field.reviewCriteria', 'orch.ph.reviewCriteria'),
        _f('must_check', 'list', 'orch.field.mustCheck', heading='Must Check',
           placeholder='orch.ph.mustCheck'),
        _f('verdict_format', 'select', 'orch.field.verdictFormat',
           heading='Verdict Format', options=[
               {'value': 'stop_continue', 'label': 'orch.opt.stopContinue'},
               {'value': 'pass_fail', 'label': 'orch.opt.passFail'},
           ]),
        _f('adversarial', 'bool', 'orch.field.adversarial',
           heading='Adversarial Verification'),
    ],
    'reviewer': [
        _objective_field('orch.field.reviewCriteria', 'orch.ph.reviewCriteria'),
        _f('must_check', 'list', 'orch.field.mustCheck', heading='Must Check',
           placeholder='orch.ph.mustCheck'),
        _f('verdict_format', 'select', 'orch.field.verdictFormat',
           heading='Verdict Format', options=[
               {'value': 'stop_continue', 'label': 'orch.opt.stopContinue'},
               {'value': 'pass_fail', 'label': 'orch.opt.passFail'},
           ]),
        _f('adversarial', 'bool', 'orch.field.adversarial',
           heading='Adversarial Verification'),
    ],
    'researcher': [
        _objective_field('orch.field.researchQuestions', 'orch.ph.researchQuestions'),
        _f('sources', 'list', 'orch.field.sources', heading='Sources',
           placeholder='orch.ph.sources'),
        _f('expected_outcome', 'textarea', 'orch.field.expectedOutcome',
           heading='Expected Outcome', placeholder='orch.ph.expectedOutcome'),
    ],
    'worker': [
        _objective_field('orch.field.taskWorker', 'orch.ph.taskWorker'),
        _f('must_do', 'list', 'orch.field.mustDo', heading='Must Do',
           placeholder='orch.ph.mustDo'),
        _f('must_not_do', 'list', 'orch.field.mustNotDo', heading='Must Not Do',
           placeholder='orch.ph.mustNotDo'),
        _f('expected_outcome', 'textarea', 'orch.field.expectedOutcome',
           heading='Expected Outcome', placeholder='orch.ph.expectedOutcome'),
    ],
    'planner': [
        _objective_field('orch.field.planningBrief', 'orch.ph.planningBrief'),
        _f('deliverables', 'list', 'orch.field.deliverables',
           heading='Deliverables', placeholder='orch.ph.deliverables'),
        _f('acceptance_criteria', 'list', 'orch.field.acceptance',
           heading='Acceptance Criteria', placeholder='orch.ph.acceptance'),
    ],
    'coder': [
        _objective_field('orch.field.taskCoder', 'orch.ph.taskCoder'),
        _f('scope_paths', 'list', 'orch.field.scopePaths', heading='Files / Paths',
           placeholder='orch.ph.scopePaths'),
        _f('constraints', 'list', 'orch.field.constraints', heading='Constraints',
           placeholder='orch.ph.constraints'),
        _f('verify_cmd', 'text', 'orch.field.verifyCmd', heading='Verify Command',
           placeholder='orch.ph.verifyCmd'),
    ],
    'analyst': [
        _objective_field('orch.field.analysisQuestion', 'orch.ph.analysisQuestion'),
        _f('data_sources', 'list', 'orch.field.dataSources', heading='Data Sources',
           placeholder='orch.ph.dataSources'),
        _f('metrics', 'list', 'orch.field.metrics', heading='Metrics',
           placeholder='orch.ph.metrics'),
        _f('expected_outcome', 'textarea', 'orch.field.expectedOutcome',
           heading='Expected Outcome', placeholder='orch.ph.expectedOutcome'),
    ],
    'writer': [
        _objective_field('orch.field.writeTask', 'orch.ph.writeTask'),
        _f('audience', 'text', 'orch.field.audience', heading='Audience',
           placeholder='orch.ph.audience'),
        _f('tone', 'select', 'orch.field.tone', heading='Tone', options=[
            {'value': 'neutral', 'label': 'orch.opt.toneNeutral'},
            {'value': 'formal', 'label': 'orch.opt.toneFormal'},
            {'value': 'casual', 'label': 'orch.opt.toneCasual'},
            {'value': 'technical', 'label': 'orch.opt.toneTechnical'},
            {'value': 'persuasive', 'label': 'orch.opt.tonePersuasive'},
        ]),
        _f('must_cover', 'list', 'orch.field.mustCover', heading='Must Cover',
           placeholder='orch.ph.mustCover'),
    ],
    'browser': [
        _objective_field('orch.field.browseTask', 'orch.ph.browseTask'),
        _f('start_url', 'text', 'orch.field.startUrl', heading='Start URL',
           placeholder='orch.ph.startUrl'),
        _f('steps', 'list', 'orch.field.steps', heading='Steps',
           placeholder='orch.ph.steps'),
        _f('extract', 'textarea', 'orch.field.extract', heading='Extract',
           placeholder='orch.ph.extract'),
    ],
    'synthesizer': [
        _objective_field('orch.field.synthTask', 'orch.ph.synthTask'),
        _f('inputs_desc', 'textarea', 'orch.field.inputsDesc', heading='Inputs',
           placeholder='orch.ph.inputsDesc'),
        _f('conflict_policy', 'select', 'orch.field.conflictPolicy',
           heading='Conflict Policy', options=[
            {'value': 'reconcile', 'label': 'orch.opt.reconcile'},
            {'value': 'majority', 'label': 'orch.opt.majority'},
            {'value': 'flag', 'label': 'orch.opt.flag'},
        ]),
        _f('output_shape', 'textarea', 'orch.field.outputShape',
           heading='Output Shape', placeholder='orch.ph.outputShape'),
    ],
    'router': [
        _objective_field('orch.field.routeBasis', 'orch.ph.routeBasis'),
        _f('categories', 'list', 'orch.field.categories', heading='Categories',
           placeholder='orch.ph.categories'),
        _f('default_route', 'text', 'orch.field.defaultRoute',
           heading='Default Route', placeholder='orch.ph.defaultRoute'),
    ],
    'virtual_user': [
        _objective_field('orch.field.persona', 'orch.ph.persona'),
        _f('done_signal', 'text', 'orch.field.doneSignal',
           heading='Done Signal', placeholder='orch.ph.doneSignal'),
    ],
}


_ROLE_INFRA_KEYS = frozenset({'tier', 'isolation', 'emits', 'name', 'io'})


def role_param_schema(role: str) -> list[dict]:
    """Return the shared FieldSpec list for a role or the generic fallback."""
    return ROLE_PARAM_SCHEMA.get(role, _GENERIC_ROLE_SCHEMA)


def _validate_role_params(role: str, where: str, params: dict,
                          errors: list, warnings: list,
                          path: str = '') -> None:
    """Validate structured role params against their backend-owned FieldSpecs."""
    validate_field_specs(
        role_param_schema(role),
        where=where,
        params=params,
        owner_kind='role',
        owner_name=role,
        infra_keys=_ROLE_INFRA_KEYS,
        errors=errors,
        warnings=warnings,
        max_text_length=MAX_OBJECTIVE_LEN,
        max_list_items=MAX_LIST_ITEMS,
        max_list_item_length=MAX_LIST_ITEM_LEN,
        path_prefix=f'{path}/params' if path else '',
    )
