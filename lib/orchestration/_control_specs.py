"""Canonical control-node FieldSpecs shared by authoring and validation.

This module owns the control-kind catalogue, its backend-authored Inspector
fields and validation derived from those same fields.  Whole-graph topology
validation remains in :mod:`lib.orchestration._validate`.
"""

from __future__ import annotations

import copy

from lib.orchestration.field_spec_contract import field_spec
from lib.orchestration.field_spec_validation import validate_field_specs
from lib.orchestration.loop_policy import (
    DEFAULT_EXECUTOR_MAX_ITERATIONS,
    DEFAULT_MAX_ITERATIONS,
)
from lib.orchestration._role_specs import MAX_OBJECTIVE_LEN


#: Control-node kinds and whether at most one may exist per definition.
CONTROL_KINDS = {
    'start':    {'single': True},
    'stop':     {'single': True},
    'loop':     {'single': False},
    'parallel': {'single': False},
    'barrier':  {'single': False},
    'branch':   {'single': False},
    'artifact': {'single': False},
    'human':    {'single': False},
}

DEFAULT_HUMAN_APPROVAL_TIMEOUT = 300

#: Canonical params for a newly-authored node of each control kind. Keeping
#: these beside the kind catalogue and FieldSpecs makes the entire control
#: authoring contract one backend-owned unit.
CONTROL_NODE_DEFAULTS = {
    'start': {'seed': ''},
    'stop': {},
    'loop': {
        'max_iterations': DEFAULT_MAX_ITERATIONS,
        'stop_condition': 'verdict:STOP',
        'verifier': 'critic',
    },
    'parallel': {},
    'barrier': {},
    'branch': {'classifier': 'router'},
    'artifact': {'path': '', 'description': '', 'format': 'file'},
    'human': {'mode': 'approve', 'prompt': '',
              'timeout_sec': DEFAULT_HUMAN_APPROVAL_TIMEOUT},
}

if set(CONTROL_NODE_DEFAULTS) != set(CONTROL_KINDS):
    raise RuntimeError('control defaults must cover exactly CONTROL_KINDS')

MAX_ARTIFACT_PATH_LEN = 512


def _control_field(key: str, kind: str, label: str, **kwargs) -> dict:
    """Build one control-node FieldSpec consumed by validator and Studio."""
    if kind in {'text', 'textarea'}:
        kwargs.setdefault('maxLength', MAX_OBJECTIVE_LEN)
    return field_spec(key, kind, label, **kwargs)


#: Executable control-node params. Topology-derived facts such as parallel
#: width and branch count are deliberately absent: outgoing edges are their
#: single source of truth and the engine never consumed the old shadow params.
CONTROL_PARAM_SCHEMA = {
    'start': [
        _control_field('seed', 'textarea', 'orch.fld.startInput',
                       placeholder='orch.fld.startInputPh'),
    ],
    'stop': [],
    'loop': [
        _control_field(
            'max_iterations', 'int', 'orch.fld.maxIter', min=1,
            runtimeMax=DEFAULT_EXECUTOR_MAX_ITERATIONS,
        ),
        _control_field('stop_condition', 'select', 'orch.fld.stopWhen',
                       options=[
                           {'value': 'verdict:STOP', 'label': 'orch.stop.verdict'},
                           {'value': 'no_new_findings',
                            'label': 'orch.stop.noNew', 'disabled': True},
                           {'value': 'max_only',
                            'label': 'orch.stop.maxOnly', 'disabled': True},
                       ]),
        _control_field('verifier', 'select', 'orch.fld.verifier', options=[
            {'value': 'critic', 'label': 'orch.verifier.critic'},
            {'value': 'reviewer', 'label': 'orch.verifier.reviewer'},
            {'value': 'virtual_user', 'label': 'orch.verifier.virtualUser'},
            {'value': 'none', 'label': 'orch.verifier.none'},
        ]),
    ],
    'parallel': [],
    'barrier': [],
    'branch': [
        _control_field('classifier', 'select', 'orch.fld.classifier',
                       allowUnknown=True, options=[
                           {'value': 'router', 'label': 'orch.classifier.router'},
                           {'value': 'analyst', 'label': 'orch.classifier.analyst'},
                           {'value': 'general', 'label': 'orch.classifier.general'},
                       ]),
    ],
    'artifact': [
        _control_field('path', 'text', 'orch.fld.filePath',
                       placeholder='orch.fld.filePathPh',
                       maxLength=MAX_ARTIFACT_PATH_LEN),
        _control_field('format', 'select', 'orch.fld.artifactKind',
                       severity='warning', options=[
                           {'value': 'file', 'label': 'orch.afmt.file'},
                           {'value': 'report', 'label': 'orch.afmt.report'},
                           {'value': 'dataset', 'label': 'orch.afmt.dataset'},
                           {'value': 'code', 'label': 'orch.afmt.code'},
                           {'value': 'image', 'label': 'orch.afmt.image'},
                       ]),
        _control_field('description', 'textarea', 'orch.fld.description',
                       placeholder='orch.fld.artifactDescPh'),
    ],
    'human': [
        _control_field('mode', 'select', 'orch.fld.humanMode',
                       errorName='human mode', options=[
            {'value': 'approve', 'label': 'orch.hmode.approve'},
            {'value': 'input', 'label': 'orch.hmode.input'},
            {'value': 'notify', 'label': 'orch.hmode.notify'},
        ]),
        _control_field('prompt', 'textarea', 'orch.fld.prompt',
                       placeholder='orch.fld.promptPh'),
        _control_field('timeout_sec', 'int', 'orch.fld.approveTimeout', min=1,
                       visibleWhen={'key': 'mode', 'equals': 'approve'}),
    ],
}


def _control_select_values(kind: str, key: str) -> frozenset[str]:
    field = next(spec for spec in CONTROL_PARAM_SCHEMA[kind]
                 if spec['key'] == key)
    return frozenset(option['value'] for option in field['options'])


# Public compatibility constants are derived from the canonical FieldSpecs,
# not separately maintained mirrors.
VALID_ARTIFACT_FORMATS = _control_select_values('artifact', 'format')

# ``approve`` blocks for a decision, ``input`` collects free text, and
# ``notify`` is non-blocking.
VALID_HUMAN_MODES = _control_select_values('human', 'mode')

_CONTROL_INFRA_KEYS = frozenset({'io'})


def control_param_schema(kind: str) -> list[dict]:
    """Return the shared FieldSpec list for one control kind."""
    return CONTROL_PARAM_SCHEMA.get(kind, [])


def resolve_control_param(node: dict, key: str, *, kind: str = ''):
    """Resolve one non-empty control value against its authored default.

    Runtime policies with intentional legacy behavior, such as a branch with
    no classifier, remain focused resolvers and do not call this helper.
    """
    params = node.get('params') or {}
    if isinstance(params, dict):
        value = params.get(key)
        if value not in (None, ''):
            return value
    control_kind = str(kind or node.get('kind') or '')
    defaults = CONTROL_NODE_DEFAULTS.get(control_kind) or {}
    return copy.deepcopy(defaults.get(key))


def _validate_control_params(kind: str, where: str, params: dict,
                             errors: list, warnings: list,
                             path: str = '') -> None:
    """Validate control params from the same FieldSpecs the Studio renders."""
    validate_field_specs(
        control_param_schema(kind),
        where=where,
        params=params,
        owner_kind='control',
        owner_name=kind,
        infra_keys=_CONTROL_INFRA_KEYS,
        errors=errors,
        warnings=warnings,
        max_text_length=MAX_OBJECTIVE_LEN,
        path_prefix=f'{path}/params' if path else '',
    )
