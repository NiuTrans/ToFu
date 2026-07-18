"""lib/orchestration/_roles.py — Role axis: schema, emits/scope, per-role params.

Owns the ROLE dimension of an orchestration definition: the known role
set, the message axis (``emits``) + subflow scope resolvers, the value
caps + param-kind constants, and the per-role structured-param FieldSpec
schema the studio inspector renders and the engine folds into a delegation
brief. Also owns the read-only :func:`role_persona` accessor.

See :mod:`lib.orchestration` for the package overview.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.orchestration._io import _coerce_list

logger = get_logger(__name__)

#: Role names the executor will eventually map to swarm ``AGENT_ROLES``
#: (lib/swarm/registry.py) plus the endpoint-style conceptual roles
#: (planner / worker / critic) and composition roles (synthesizer /
#: router). Unknown roles are a *warning*, not an error — the studio is
#: an authoring surface and roles may be user-defined before the engine
#: learns them.
KNOWN_ROLES = frozenset({
    # swarm AGENT_ROLES
    'researcher', 'coder', 'analyst', 'browser', 'reviewer', 'writer', 'general',
    # endpoint-style + composition roles
    'planner', 'worker', 'critic', 'synthesizer', 'router',
    # autopilot: a synthetic user that auto-replies to keep the loop going
    'virtual_user',
})

#: The MESSAGE axis (orthogonal to ``role``). Every role/subflow node
#: produces a conversation turn; ``emits`` decides whether that turn is
#: recorded as a ``user`` or ``assistant`` message (and which side of the
#: chat it renders on). This makes the user/assistant distinction — until
#: now hardcoded in the endpoint adapter (critic→user, planner/worker→
#: assistant) — a first-class, per-node authoring choice. Omitting it falls
#: back to :func:`resolve_emits` derivation, so existing definitions and
#: ``build_endpoint_definition`` are unchanged.
VALID_EMITS = frozenset({'user', 'assistant'})

#: Roles whose turn is, by default, a ``user`` message (the conversation's
#: "other side"). A verifier critiques the producer; a virtual user stands
#: in for the human. Everything else defaults to ``assistant``.
_USER_EMIT_ROLES = frozenset({'critic', 'reviewer', 'virtual_user'})

VALID_TIERS = frozenset({'light', 'standard', 'heavy'})
VALID_ISOLATION = frozenset({'fresh-context', 'shared-context'})

#: Subflow execution scope (a ``subflow`` node only). ``inline`` is the
#: phase-1 macro-expansion: the child's nodes are spliced into the parent
#: graph (shared parent context — no boundary). ``isolated`` is the true
#: black box: the child runs in its OWN nested executor with its own
#: context, sees only the upstream context as a seed, and returns a single
#: converged result — to the parent it is indistinguishable from a role.
#: An absent scope defaults to ``inline`` so pre-scope definitions (and
#: every existing test) behave exactly as before; the Studio stamps new
#: groups with ``isolated`` explicitly.
VALID_SCOPES = frozenset({'inline', 'isolated'})

MAX_OBJECTIVE_LEN = 4000

#: Max items in a list-kind structured param, and max chars per item.
MAX_LIST_ITEMS = 20
MAX_LIST_ITEM_LEN = 500

#: Valid structured-param field kinds. The frontend inspector renders each
#: kind with a matching control; the validator type-checks by kind.
VALID_PARAM_KINDS = frozenset({'text', 'textarea', 'select', 'list', 'int', 'bool'})


# ── Per-role structured params (the "what to do" schema) ──────────────
#
# Each role exposes a list of FieldSpec dicts describing the structured
# inputs the studio inspector should render and the engine renders into the
# delegation brief (see :func:`render_role_brief`). A FieldSpec is::
#
#     {
#       'key':   'must_do',            # params.<key> — stable, NOT i18n'd
#       'kind':  'list',               # one of VALID_PARAM_KINDS
#       'label': 'orch.field.mustDo',  # i18n KEY (frontend resolves via t())
#       'options': [{'value': 'stop_continue', 'label': 'orch.opt.stopContinue'}],
#                                       # select-kind only; value stable, label i18n key
#       'placeholder': 'orch.ph.mustDo',  # optional i18n key
#       'heading': 'Must Do',           # section heading in the rendered brief
#     }
#
# EVERY role keeps the core ``objective`` field (stable key) — only its
# LABEL changes per role, which is what fixes the "obscure single field".
# Roles without an entry use :data:`_GENERIC_ROLE_SCHEMA`. Labels are i18n
# KEYS, never user-facing strings — the backend owns structure, the frontend
# owns wording (consumed via the /role-schema accessor).

def _f(key, kind, label, *, heading=None, options=None, placeholder=None):
    spec = {'key': key, 'kind': kind, 'label': label}
    if heading is not None:
        spec['heading'] = heading
    if options is not None:
        spec['options'] = options
    if placeholder is not None:
        spec['placeholder'] = placeholder
    return spec


#: The core task field every role carries (label overridden per role below).
def _objective_field(label, placeholder=None):
    return _f('objective', 'textarea', label, heading='Task',
              placeholder=placeholder)


#: Generic schema for roles without a bespoke entry (writer / synthesizer /
#: analyst / general / browser / router, and any unknown/user-defined role).
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


def resolve_emits(node: dict) -> str:
    """Resolve a node's effective message axis (``'user'`` | ``'assistant'``).

    An explicit ``params.emits`` wins (validated against :data:`VALID_EMITS`).
    Otherwise it is derived from the node's ``role`` so existing definitions
    behave exactly as before:

      * ``critic`` / ``reviewer`` / ``virtual_user`` → ``'user'``
      * everything else (planner, worker, specialists, subflows) → ``'assistant'``

    Pure; never raises — an invalid explicit value falls through to derivation
    (the validator is what flags it as an error).
    """
    params = node.get('params') or {}
    explicit = params.get('emits')
    if explicit in VALID_EMITS:
        return explicit
    role = node.get('role') or ''
    return 'user' if role in _USER_EMIT_ROLES else 'assistant'


def resolve_scope(node: dict) -> str:
    """Resolve a ``subflow`` node's execution scope (``'inline'`` | ``'isolated'``).

    An explicit, valid ``params.scope`` wins; anything else (absent or
    invalid) falls back to ``'inline'`` so existing definitions keep their
    flatten-into-parent behavior. Pure; never raises — an invalid explicit
    value falls through to the default (the validator flags it as an error).
    """
    params = node.get('params') or {}
    scope = params.get('scope')
    return scope if scope in VALID_SCOPES else 'inline'


#: Non-task params a role node legitimately carries (validated elsewhere or
#: structural). Keys outside the role's field schema AND this set get an
#: unknown-key WARNING (forward-compat, mirrors the unknown-role stance).
_ROLE_INFRA_KEYS = frozenset({'tier', 'isolation', 'emits', 'name', 'io'})


def _validate_role_params(role: str, where: str, params: dict,
                          errors: list, warnings: list) -> None:
    """Type-check a role node's structured params against its schema.

    Enforced as ERRORS: wrong kind (e.g. a list field given a non-list/str),
    a select value outside its options, and length / item-count caps. Reported
    as WARNINGS: param keys the role's schema doesn't know about (so an old
    or future field never hard-blocks a save). ``objective`` length is checked
    by the caller; here it is only kind-checked.
    """
    schema = role_param_schema(role)
    known = {spec['key'] for spec in schema} | _ROLE_INFRA_KEYS
    by_key = {spec['key']: spec for spec in schema}

    for key, val in params.items():
        if key not in by_key:
            if key not in known:
                warnings.append(f'{where} unknown param {key!r} for role '
                                f'{role!r} (ignored by the engine)')
            continue
        spec = by_key[key]
        kind = spec['kind']
        if val is None:
            continue
        if kind == 'list':
            if not isinstance(val, (list, tuple, str)):
                errors.append(f'{where} param {key!r} must be a list')
                continue
            items = _coerce_list(val)
            if len(items) > MAX_LIST_ITEMS:
                errors.append(f'{where} param {key!r} exceeds {MAX_LIST_ITEMS} items')
            for it in items:
                if len(it) > MAX_LIST_ITEM_LEN:
                    errors.append(f'{where} param {key!r} item exceeds '
                                  f'{MAX_LIST_ITEM_LEN} chars')
                    break
        elif kind == 'bool':
            if not isinstance(val, bool):
                errors.append(f'{where} param {key!r} must be a boolean')
        elif kind == 'int':
            if not isinstance(val, int) or isinstance(val, bool):
                errors.append(f'{where} param {key!r} must be an integer')
        elif kind == 'select':
            opts = {o['value'] for o in (spec.get('options') or [])}
            if not isinstance(val, str) or val not in opts:
                errors.append(f'{where} param {key!r} must be one of '
                              f'{sorted(opts)}')
        else:  # text / textarea
            if not isinstance(val, str):
                errors.append(f'{where} param {key!r} must be a string')
            elif len(val) > MAX_OBJECTIVE_LEN:
                errors.append(f'{where} param {key!r} exceeds {MAX_OBJECTIVE_LEN} chars')


def role_param_schema(role: str) -> list[dict]:
    """Return the structured-param FieldSpec list for a role.

    Known roles get their bespoke schema; everything else (unknown /
    user-defined / generic specialists) gets :data:`_GENERIC_ROLE_SCHEMA`.
    Pure; returns the shared list object (callers must not mutate it).
    """
    return ROLE_PARAM_SCHEMA.get(role, _GENERIC_ROLE_SCHEMA)


def role_persona(role: str | None = None):
    """Return the READ-ONLY persona design for a role (or every role).

    A role's behavior is fixed by the backend in
    :data:`lib.swarm.registry.AGENT_ROLES`: a ``system_prompt_suffix`` (the
    character's prompt), a ``when_to_use`` guidance blurb, and a model-tier
    hint. The Orchestration Studio SHOWS this so an author understands what a
    character does and how it behaves — but it is deliberately **not** an
    editable field. The prompt design is owned here, not in the authoring
    layer, so a flow author can never silently rewrite a role's character.

    ``role_persona('coder')`` → that role's persona dict (a ``general``
    fallback for unknown roles). ``role_persona()`` → a ``{role: persona}``
    map for every known role. Each persona is::

        {'prompt': <system_prompt_suffix>, 'whenToUse': <guidance>,
         'tier': <'light'|'standard'|'heavy'>}

    The swarm import is lazy (function-local) to avoid a module-load cycle —
    ``lib.orchestration`` is imported by the lightweight route layer, while
    ``lib.swarm`` pulls in the heavier agent stack. Pure; never raises.
    """
    from lib.swarm.registry import AGENT_ROLES

    def _one(r: str) -> dict:
        cfg = AGENT_ROLES.get(r) or AGENT_ROLES.get('general') or {}
        return {
            'prompt': (cfg.get('system_prompt_suffix') or '').strip(),
            'whenToUse': (cfg.get('when_to_use') or '').strip(),
            'tier': cfg.get('model_hint', 'standard'),
        }

    if role is not None:
        return _one(role)
    return {r: _one(r) for r in AGENT_ROLES}
