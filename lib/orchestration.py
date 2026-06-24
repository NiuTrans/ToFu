"""lib/orchestration.py — Orchestration definition schema + validator.

An *orchestration definition* is the declarative graph a user authors in
the frontend Orchestration Studio (``static/js/orchestration.js``). It
describes a topology of ROLE agents and CONTROL nodes wired by directed
edges — an endpoint-style loop, a fan-out/synthesize flow, etc.

This module is the **contract seam**: it owns the schema constants and a
pure ``validate_definition()`` that both the REST store
(``routes/api_v1/orchestrations.py``) and the future execution engine
import. Keeping validation here (not in the route) means the engine
validates with the exact same rules the authoring API enforced.

The definition is intentionally NOT executed here. Per CLAUDE.md the
frontend authors JSON; the backend stores + validates it now, and a
swarm-backed interpreter will consume it later.

Schema (``tofu.orchestration/v1``)::

    {
      "schema": "tofu.orchestration/v1",
      "name":   "Endpoint Loop",
      "nodes": [
        {"id": "planner1", "type": "role", "role": "planner",
         "name": "Planner", "pos": {"x": 1, "y": 2}, "params": {...}},
        {"id": "loop1", "type": "control", "kind": "loop",
         "pos": {...}, "params": {"max_iterations": 10, ...}}
      ],
      "edges": [{"from": "planner1", "to": "loop1"}]
    }
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

SCHEMA_ID = 'tofu.orchestration/v1'

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

#: Nesting cap for subflow expansion — defense against pathological or
#: (via a ref resolver) self-referential nesting.
MAX_SUBFLOW_DEPTH = 5

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

#: Valid artifact 'format' hints (mirror the studio inspector dropdown).
VALID_ARTIFACT_FORMATS = frozenset({'file', 'report', 'dataset', 'code', 'image'})

#: Human-in-the-loop gate modes (mirror the studio inspector dropdown):
#: ``approve`` blocks for an approve/reject decision, ``input`` blocks to
#: collect a free-text answer that is appended to the flow context, and
#: ``notify`` is non-blocking — it just surfaces a message to the user.
VALID_HUMAN_MODES = frozenset({'approve', 'input', 'notify'})

MAX_ARTIFACT_PATH_LEN = 512

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

MAX_NAME_LEN = 120
MAX_NODES = 200
MAX_OBJECTIVE_LEN = 4000

#: Max items in a list-kind structured param, and max chars per item.
MAX_LIST_ITEMS = 20
MAX_LIST_ITEM_LEN = 500

#: Valid structured-param field kinds. The frontend inspector renders each
#: kind with a matching control; the validator type-checks by kind.
VALID_PARAM_KINDS = frozenset({'text', 'textarea', 'select', 'list', 'int', 'bool'})

# ── Typed node I/O contract (the Dify-style dataflow axis) ────────────
#
# Orthogonal to ``role`` (who) and ``emits`` (which side of the chat), a
# node may declare a STRICT input/output contract under ``params.io``::
#
#     params.io = {
#       'inputs':  [{'name': 'brief', 'type': 'text', 'from': 'planner.text'}],
#       'outputs': [{'name': 'summary', 'type': 'text'},
#                   {'name': 'changes',  'type': 'artifact'}],
#     }
#
# A port's ``type`` is a hint from :data:`VALID_IO_TYPES`. An input's
# ``from`` references an upstream producer as ``'<nodeId>'`` (its primary
# output), ``'<nodeId>.<outputName>'`` (a named output), or the literal
# ``'start'`` (the flow's initial context). The contract is OPTIONAL and
# fully back-compatible: a node with no ``io`` block keeps the legacy
# accumulating-scratchpad behavior and emits a single implicit ``text``
# output (see :func:`node_output_names`). Declaring ``io.inputs`` switches
# that node to typed-input composition in the engine — it then sees ONLY
# the referenced outputs instead of the whole transcript blob, which is
# what makes a flow read like Dify.
VALID_IO_TYPES = frozenset({'text', 'json', 'artifact', 'file', 'number', 'bool', 'any'})

#: Max declared input or output ports on a single node.
MAX_IO_PORTS = 12

#: The implicit output every node exposes when it declares none. A
#: pure-natural-language node has exactly this one ``text`` output; a
#: tool-heavy worker opts into a second ``artifact`` output (e.g.
#: ``changes``) to expose its state-changing actions as a typed manifest.
DEFAULT_OUTPUT_NAME = 'text'

#: Literal ``from`` token referencing the flow's initial context (the Start
#: node's seed / the Run-panel input).
IO_START_REF = 'start'

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


def _coerce_list(value) -> list[str]:
    """Normalize a list-kind param value to a list of non-empty strings.

    Tolerates a single string (split on newlines) so a textarea-backed list
    field round-trips, and drops blank entries.
    """
    if isinstance(value, str):
        items = value.split('\n')
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out = []
    for it in items:
        s = str(it).strip()
        if s:
            out.append(s)
    return out


def node_output_names(node: dict) -> list[str]:
    """Return the names of the outputs a node exposes.

    A node that declares ``params.io.outputs`` exposes exactly those named
    ports; any other node exposes the single implicit
    :data:`DEFAULT_OUTPUT_NAME` (``'text'``) port — so legacy definitions
    behave as if every node has one ``text`` output. Pure; never raises.
    """
    io = (node.get('params') or {}).get('io')
    if isinstance(io, dict):
        outs = io.get('outputs')
        if isinstance(outs, list):
            names = [o.get('name') for o in outs
                     if isinstance(o, dict) and isinstance(o.get('name'), str)
                     and o.get('name').strip()]
            if names:
                return names
    return [DEFAULT_OUTPUT_NAME]


def parse_io_ref(ref: str) -> tuple[str, str | None]:
    """Split an input ``from`` ref into ``(node_id, output_name|None)``.

    ``'planner'`` → ``('planner', None)`` (the node's primary output);
    ``'worker.changes'`` → ``('worker', 'changes')``; the literal
    ``'start'`` → ``('start', None)``. Pure; never raises.
    """
    if not isinstance(ref, str):
        return '', None
    ref = ref.strip()
    if '.' in ref:
        nid, _, out = ref.partition('.')
        return nid, (out or None)
    return ref, None


def _validate_node_io(node: dict, where: str, params: dict, ids: set,
                      id_to_node: dict, errors: list, warnings: list) -> None:
    """Validate a node's optional ``params.io`` typed-contract block.

    Checks, per :data:`VALID_IO_TYPES` and :data:`MAX_IO_PORTS`:
      * ``io`` (if present) is an object with optional list ``inputs`` /
        ``outputs``; each port is ``{name, type}`` with a unique, non-empty
        name and a known type (ERROR otherwise).
      * Each input ``from`` (when supplied) references a real upstream node
        (or the literal ``start``); a named output ref must match one the
        target actually declares (ERROR for an unknown node, WARNING for an
        unknown output name so a forward-declared port never hard-blocks).

    Pure relative to its inputs (mutates only the passed error/warning
    lists). Cross-node ref resolution needs ``id_to_node``, which the caller
    builds once for the whole definition.
    """
    io = params.get('io')
    if io is None:
        return
    if not isinstance(io, dict):
        errors.append(f'{where} io must be an object')
        return

    for side in ('inputs', 'outputs'):
        ports = io.get(side)
        if ports is None:
            continue
        if not isinstance(ports, list):
            errors.append(f'{where} io.{side} must be an array')
            continue
        if len(ports) > MAX_IO_PORTS:
            errors.append(f'{where} io.{side} exceeds {MAX_IO_PORTS} ports')
        seen_names: set[str] = set()
        for j, port in enumerate(ports):
            pwhere = f'{where} io.{side}[{j}]'
            if not isinstance(port, dict):
                errors.append(f'{pwhere} must be an object')
                continue
            pname = port.get('name')
            if not isinstance(pname, str) or not pname.strip():
                errors.append(f'{pwhere} missing string name')
            elif pname in seen_names:
                errors.append(f'{pwhere} duplicate port name {pname!r}')
            else:
                seen_names.add(pname)
            ptype = port.get('type')
            if ptype is not None and ptype not in VALID_IO_TYPES:
                errors.append(f'{pwhere} invalid type {ptype!r} '
                              f'(expected one of {sorted(VALID_IO_TYPES)})')
            if side == 'inputs':
                frm = port.get('from')
                if frm is None or frm == '':
                    continue
                if not isinstance(frm, str):
                    errors.append(f'{pwhere} from must be a string')
                    continue
                src_id, src_out = parse_io_ref(frm)
                if src_id == IO_START_REF:
                    continue
                if src_id not in ids:
                    errors.append(f'{pwhere} from {frm!r} references '
                                  'unknown node')
                    continue
                if src_out is not None:
                    avail = node_output_names(id_to_node.get(src_id) or {})
                    if src_out not in avail:
                        warnings.append(
                            f'{pwhere} from {frm!r}: node {src_id!r} does not '
                            f'declare an output named {src_out!r} '
                            f'(has {avail})')


def render_role_brief(node: dict) -> str:
    """Compose a role node's structured params into a delegation brief.

    This is the bridge from the authoring layer to the execution layer: the
    engine fills ``SubTaskSpec.objective`` with this rendered text (the swarm
    stays dumb — it still just wraps the result in ``## Your Task``).

    Back-compat invariant: a node whose only meaningful param is ``objective``
    (no other structured fields set) returns **exactly** ``objective`` —
    byte-identical to the pre-structured-params behavior — so every existing
    definition, ``build_endpoint_definition`` and ``build_autopilot_definition``
    render unchanged.

    Composition rule: the ``objective`` field renders as a bare lead paragraph
    (no heading); every other set field renders as a ``### <heading>`` section.
    List fields become ``- item`` bullets; bool fields render only when true;
    select fields render their stored value. Empty/unset fields are omitted.
    Section order follows the role's schema order. Pure; never raises.
    """
    params = node.get('params') or {}
    role = node.get('role') or ''
    schema = role_param_schema(role)

    lead = ''
    sections: list[str] = []
    for spec in schema:
        key = spec.get('key')
        kind = spec.get('kind')
        val = params.get(key)
        if key == 'objective':
            lead = (val or '').strip() if isinstance(val, str) else ''
            continue
        heading = spec.get('heading') or key
        if kind == 'list':
            items = _coerce_list(val)
            if items:
                body = '\n'.join(f'- {it}' for it in items)
                sections.append(f'### {heading}\n{body}')
        elif kind == 'bool':
            if val is True:
                sections.append(f'### {heading}\nYes.')
        elif kind in ('text', 'textarea', 'select'):
            s = (val or '').strip() if isinstance(val, str) else ''
            if s:
                sections.append(f'### {heading}\n{s}')
        elif kind == 'int':
            if isinstance(val, int):
                sections.append(f'### {heading}\n{val}')

    if not sections:
        return lead
    parts = ([lead] if lead else []) + sections
    return '\n\n'.join(parts)


#: Engine role → the endpoint UI phase (and streaming-bubble role) it maps to.
#: A planner node opens the loop with a Planner bubble; a verifier (critic /
#: reviewer / virtual_user) lands on the user side ("reviewing"); every other
#: producer role streams as a Worker. Mirrors ``EndpointEventAdapter``'s
#: role/emits classification so the bubble the FRONTEND creates up front
#: matches the first message the adapter will actually emit.
_PLANNER_ROLES = frozenset({'planner'})


def first_executed_role(defn: dict) -> dict | None:
    """Return the first ROLE node the engine would run, or ``None``.

    Walks the graph from the start node following single ``from→to`` edges,
    skipping control nodes (start / loop / parallel / barrier / branch /
    artifact), and returns the first ``type == 'role'`` (or ``subflow``) node
    encountered. This is a static, side-effect-free preview of "what bubble
    comes first" — used to pick the initial chat phase so a plannerless flow
    (e.g. autopilot: worker→vu) never shows a hanging Planner placeholder.

    Pure; never raises. Returns ``None`` for a graph with no reachable role.
    """
    if not isinstance(defn, dict):
        return None
    nodes = {n.get('id'): n for n in defn.get('nodes') or []
             if isinstance(n, dict) and n.get('id')}
    fwd: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in defn.get('edges') or []:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in nodes and d in fwd:
            fwd[s].append(d)
    # Locate start (explicit start kind, else a source node).
    start = None
    for nid, n in nodes.items():
        if n.get('kind') == 'start':
            start = nid
            break
    if start is None:
        rev_targets = {d for outs in fwd.values() for d in outs}
        for nid in nodes:
            if nid not in rev_targets:
                start = nid
                break
    if start is None:
        return None
    seen: set[str] = set()
    cur = start
    while cur and cur not in seen:
        seen.add(cur)
        n = nodes.get(cur) or {}
        if n.get('type') in ('role', 'subflow'):
            return n
        nxt = fwd.get(cur) or []
        cur = nxt[0] if nxt else None
    return None


def initial_phase_for_flow(defn: dict) -> str:
    """Classify a flow's opening chat phase from its first role node.

    Returns one of ``'planning'`` | ``'reviewing'`` | ``'working'`` — the
    same vocabulary ``routes/chat.py`` ships as ``endpointPhase`` and the
    frontend maps to the planner / critic / worker streaming bubble. A flow
    that opens on a ``planner`` role → ``'planning'``; one that opens on a
    verifier (its first turn lands user-side) → ``'reviewing'``; everything
    else (the common worker-first / autopilot case) → ``'working'``.

    Pure; never raises. Defaults to ``'working'`` when no role is found.
    """
    node = first_executed_role(defn)
    if not node:
        return 'working'
    role = node.get('role') or ''
    if role in _PLANNER_ROLES:
        return 'planning'
    if resolve_emits(node) == 'user':
        return 'reviewing'
    return 'working'


def _validate_subflow_node(node: dict, where: str, params: dict,
                           errors: list, warnings: list,
                           depth: int, seen_refs: frozenset[str]) -> None:
    """Validate a ``subflow`` node (a "big role" composed of small roles).

    A subflow node embeds (``params.definition``) or references
    (``params.ref`` — a stored orchestration id) a complete child
    ``tofu.orchestration/v1`` definition. To the parent it is one node with
    its own ``role`` label + ``emits``; internally it is a self-contained
    flow with its own start/stop and context organisation. Validation
    recurses into an embedded definition (bounded by
    :data:`MAX_SUBFLOW_DEPTH`) and detects ref cycles. A bare ``ref`` is NOT
    resolved here (the validator is pure / I/O-free) — the engine resolves +
    re-validates it at expansion time; we only guard against a subflow
    referencing an ancestor (direct self-include).
    """
    emits = params.get('emits')
    if emits is not None and emits not in VALID_EMITS:
        errors.append(f'{where} invalid emits {emits!r} '
                      f'(expected one of {sorted(VALID_EMITS)})')

    scope = params.get('scope')
    if scope is not None and scope not in VALID_SCOPES:
        errors.append(f'{where} invalid scope {scope!r} '
                      f'(expected one of {sorted(VALID_SCOPES)})')

    child = params.get('definition')
    ref = params.get('ref')
    if child is None and ref is None:
        errors.append(f'{where} subflow needs params.definition (embedded) '
                      'or params.ref (stored id)')
        return

    if ref is not None:
        if not isinstance(ref, str) or not ref:
            errors.append(f'{where} subflow ref must be a non-empty string')
        elif ref in seen_refs:
            errors.append(f'{where} subflow ref {ref!r} is recursive '
                          '(references an ancestor flow)')
        return  # embedded definition (if any) is validated below; ref is opaque here

    if depth + 1 > MAX_SUBFLOW_DEPTH:
        errors.append(f'{where} subflow nesting exceeds MAX_SUBFLOW_DEPTH '
                      f'({MAX_SUBFLOW_DEPTH})')
        return

    sub = validate_definition(child, _depth=depth + 1, _seen_refs=seen_refs)
    for e in sub['errors']:
        errors.append(f'{where} subflow: {e}')
    for w in sub['warnings']:
        warnings.append(f'{where} subflow: {w}')


def validate_definition(defn: Any, *, _depth: int = 0,
                        _seen_refs: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Validate an orchestration definition.

    Pure function — no I/O, no mutation of the input. Returns a verdict
    dict so both the REST layer and the engine can decide what to do.

    Args:
        defn: The candidate definition (already JSON-parsed).

    Returns:
        ``{'ok': bool, 'errors': [str], 'warnings': [str]}``. ``ok`` is
        True iff ``errors`` is empty; ``warnings`` never block.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(defn, dict):
        return {'ok': False, 'errors': ['definition must be a JSON object'],
                'warnings': []}

    schema = defn.get('schema')
    if schema != SCHEMA_ID:
        warnings.append(f'unexpected schema {schema!r} (expected {SCHEMA_ID!r})')

    name = defn.get('name', '')
    if not isinstance(name, str) or not name.strip():
        errors.append('name is required and must be a non-empty string')
    elif len(name) > MAX_NAME_LEN:
        errors.append(f'name exceeds {MAX_NAME_LEN} chars')

    nodes = defn.get('nodes')
    edges = defn.get('edges')
    if not isinstance(nodes, list):
        errors.append('nodes must be an array')
        nodes = []
    if not isinstance(edges, list):
        errors.append('edges must be an array')
        edges = []

    if len(nodes) > MAX_NODES:
        errors.append(f'too many nodes ({len(nodes)} > {MAX_NODES})')

    ids: set[str] = set()
    kind_counts: dict[str, int] = {}
    role_count = 0

    for i, node in enumerate(nodes):
        where = f'node[{i}]'
        if not isinstance(node, dict):
            errors.append(f'{where} must be an object')
            continue
        nid = node.get('id')
        if not isinstance(nid, str) or not nid:
            errors.append(f'{where} missing string id')
            continue
        where = f'node {nid!r}'
        if nid in ids:
            errors.append(f'duplicate node id {nid!r}')
        ids.add(nid)

        ntype = node.get('type')
        params = node.get('params') or {}
        if not isinstance(params, dict):
            errors.append(f'{where} params must be an object')
            params = {}

        if ntype == 'role':
            role_count += 1
            role = node.get('role')
            if not isinstance(role, str) or not role:
                errors.append(f'{where} role node missing role')
            elif role not in KNOWN_ROLES:
                warnings.append(f'{where} unknown role {role!r} (engine may '
                                'not map it until registered)')
            tier = params.get('tier')
            if tier is not None and tier not in VALID_TIERS:
                errors.append(f'{where} invalid tier {tier!r}')
            iso = params.get('isolation')
            if iso is not None and iso not in VALID_ISOLATION:
                errors.append(f'{where} invalid isolation {iso!r}')
            obj = params.get('objective')
            if isinstance(obj, str) and len(obj) > MAX_OBJECTIVE_LEN:
                errors.append(f'{where} objective exceeds {MAX_OBJECTIVE_LEN} chars')
            emits = params.get('emits')
            if emits is not None and emits not in VALID_EMITS:
                errors.append(f'{where} invalid emits {emits!r} '
                              f'(expected one of {sorted(VALID_EMITS)})')
            _validate_role_params(role if isinstance(role, str) else '',
                                  where, params, errors, warnings)
        elif ntype == 'subflow':
            role_count += 1
            _validate_subflow_node(node, where, params, errors, warnings,
                                   _depth, _seen_refs)
        elif ntype == 'control':
            kind = node.get('kind')
            if kind not in CONTROL_KINDS:
                errors.append(f'{where} invalid control kind {kind!r}')
            else:
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                if kind == 'artifact':
                    path = params.get('path')
                    if path is not None and not isinstance(path, str):
                        errors.append(f'{where} artifact path must be a string')
                    elif isinstance(path, str) and len(path) > MAX_ARTIFACT_PATH_LEN:
                        errors.append(f'{where} artifact path exceeds '
                                      f'{MAX_ARTIFACT_PATH_LEN} chars')
                    fmt = params.get('format')
                    if fmt is not None and fmt not in VALID_ARTIFACT_FORMATS:
                        warnings.append(f'{where} unknown artifact format {fmt!r}')
                    if not (isinstance(path, str) and path.strip()):
                        warnings.append(f'{where} artifact has no path — it will be '
                                        'recorded but unnamed')
                elif kind == 'human':
                    mode = params.get('mode')
                    if mode is not None and mode not in VALID_HUMAN_MODES:
                        errors.append(f'{where} invalid human mode {mode!r}')
                    prompt = params.get('prompt')
                    if isinstance(prompt, str) and len(prompt) > MAX_OBJECTIVE_LEN:
                        errors.append(f'{where} prompt exceeds {MAX_OBJECTIVE_LEN} chars')
        else:
            errors.append(f'{where} invalid type {ntype!r} (expected '
                          "'role', 'subflow' or 'control')")

    # Single-instance control nodes.
    for kind, cfg in CONTROL_KINDS.items():
        if cfg['single'] and kind_counts.get(kind, 0) > 1:
            errors.append(f'at most one {kind!r} node allowed '
                          f'(found {kind_counts[kind]})')

    id_to_node = {n.get('id'): n for n in nodes if isinstance(n, dict)}

    # Typed I/O contract — validated in a second pass so an input ``from``
    # ref may point at a node declared later in the array.
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get('id')
        if not isinstance(nid, str) or not nid:
            continue
        nparams = node.get('params') or {}
        if isinstance(nparams, dict):
            _validate_node_io(node, f'node {nid!r}', nparams, ids,
                              id_to_node, errors, warnings)

    # Edge validation.
    seen_edges: set[tuple[str, str]] = set()
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f'edge[{i}] must be an object')
            continue
        src = edge.get('from')
        dst = edge.get('to')
        if src not in ids:
            errors.append(f'edge[{i}] from {src!r} references unknown node')
        if dst not in ids:
            errors.append(f'edge[{i}] to {dst!r} references unknown node')
        if src == dst:
            errors.append(f'edge[{i}] self-loop on {src!r}')
        if (src, dst) in seen_edges:
            warnings.append(f'duplicate edge {src!r}→{dst!r}')
        seen_edges.add((src, dst))
        # A Start node has no input; a Stop node has no output.
        sn = id_to_node.get(src)
        dn = id_to_node.get(dst)
        if dn and dn.get('kind') == 'start':
            errors.append(f'edge[{i}] targets a start node (start has no input)')
        if sn and sn.get('kind') == 'stop':
            errors.append(f'edge[{i}] leaves a stop node (stop has no output)')

    # Structural soft-guidance (warnings only — a draft may be incomplete).
    if nodes:
        if kind_counts.get('start', 0) == 0:
            warnings.append('no start node — the engine will not know where to begin')
        if kind_counts.get('stop', 0) == 0:
            warnings.append('no stop node — the flow has no defined terminal')
        if role_count == 0:
            warnings.append('no agent nodes — the flow does no work')

    return {'ok': not errors, 'errors': errors, 'warnings': warnings}


def build_endpoint_definition(*, name: str = 'Endpoint Loop',
                              max_iterations: int = 10,
                              verifier: str = 'critic') -> dict:
    """Build the canonical endpoint-mode flow as a definition.

    Expresses Tofu's endpoint mode — Planner → loop[Worker → Critic] → Stop —
    as a ``tofu.orchestration/v1`` graph the :class:`FlowExecutor` can run.
    The Worker is ``shared-context`` so it accumulates its prior attempt +
    the critic's feedback across iterations (the engine reproduces endpoint's
    progress-carryover); the verifier loops back to the loop node.

    This is the single source of truth bridging endpoint mode and the
    declarative engine: a future cutover runs THIS definition instead of the
    bespoke loop in ``lib/tasks_pkg/endpoint.py``. Kept here (not in the
    engine) so it is validated + laid out by the same pure helpers.
    """
    defn = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'planner', 'type': 'role', 'role': 'planner',
             'params': {'objective': 'Rewrite the request into a structured '
                        'brief + checklist for the worker.'}},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': {'max_iterations': int(max_iterations),
                        'stop_condition': 'verdict:STOP', 'verifier': verifier}},
            {'id': 'worker', 'type': 'role', 'role': 'worker',
             'params': {'isolation': 'shared-context', 'tier': 'heavy',
                        'objective': 'Execute the plan. Your first tool call '
                        'MUST be state-changing — act, do not just analyze.'}},
            {'id': 'critic', 'type': 'role', 'role': verifier,
             'params': {'objective': 'Review the worker output against the '
                        'checklist. End with [VERDICT: STOP] or '
                        '[VERDICT: CONTINUE_WORKER].'}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'planner'},
            {'from': 'planner', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'critic'},
            {'from': 'critic', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(defn)
    return defn


def build_autopilot_definition(*, name: str = 'Autopilot',
                               max_iterations: int = 12,
                               worker: str = 'worker') -> dict:
    """Build the canonical autopilot (virtual-user) flow as a definition.

    Expresses autopilot mode — a ``worker`` that keeps going because a
    ``virtual_user`` auto-replies at every natural stop — as a
    ``tofu.orchestration/v1`` graph:

        start → loop[ worker(assistant) → virtual_user(user) ] → stop

    The virtual user emits a ``user`` turn (the message axis this change
    introduces), and signals completion with ``[VU: TASK_DONE]`` (mapped to
    the loop's STOP verdict). The worker is ``shared-context`` so it
    accumulates the running conversation across turns. This is the single
    source of truth bridging autopilot mode and the declarative engine —
    the sibling of :func:`build_endpoint_definition`.
    """
    defn = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': {'max_iterations': int(max_iterations),
                        'stop_condition': 'verdict:STOP', 'verifier': 'virtual_user'}},
            {'id': 'worker', 'type': 'role', 'role': worker,
             'params': {'isolation': 'shared-context', 'tier': 'heavy',
                        'emits': 'assistant',
                        'objective': 'Continue the task. Make concrete '
                        'progress every turn; act, do not just analyze.'}},
            {'id': 'vu', 'type': 'role', 'role': 'virtual_user',
             'params': {'emits': 'user', 'tier': 'standard',
                        'objective': 'Stand in for the human. Reply in 1-3 '
                        'sentences to keep the task moving. Emit '
                        '[VERDICT: STOP] (or [VU: TASK_DONE]) only when the '
                        'assistant has clearly finished.'}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'vu'},
            {'from': 'vu', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(defn)
    return defn


def expand_subflows(defn: dict, *, resolver: Any = None, _depth: int = 0) -> dict:
    """Flatten every ``subflow`` node into the parent graph (macro expansion).

    A subflow node is inlined: its embedded child definition's inner nodes
    are spliced into the parent with namespaced ids (``<subflowId>/<childId>``),
    the child's ``start`` / ``stop`` control nodes are dropped, and the
    parent's edges into / out of the subflow node are rewired to the child's
    real entry / exit nodes. Subroutine-inlining semantics.

    This is the phase-1 nesting strategy: the result is a single flat graph
    the existing :class:`FlowExecutor` runs unchanged. Inlining deliberately
    does NOT create a context boundary (inner nodes share the parent
    context).

    Only ``inline``-scoped subflows (the default — see
    :func:`resolve_scope`) are flattened here. An ``isolated`` subflow is the
    true black box: it is left **intact** as a subflow node so the engine can
    run it in its own nested :class:`FlowExecutor` with a fresh context. Its
    embedded child is therefore NOT expanded by the parent — the nested
    executor expands its own inline subflows when it is constructed.

    Args:
        defn: A validated definition (possibly containing subflow nodes).
        resolver: Optional ``callable(ref:str) -> definition|None`` used to
            resolve ``params.ref`` subflows from a store. Embedded
            ``params.definition`` subflows need no resolver.
        _depth: Recursion guard (bounded by :data:`MAX_SUBFLOW_DEPTH`).

    Returns:
        A NEW definition dict (input is not mutated). Positions are recomputed
        via :func:`layout_definition` so the flattened graph lays out cleanly.

    Raises:
        ValueError: on a ref that cannot be resolved, or nesting past the cap.
    """
    import copy

    if _depth > MAX_SUBFLOW_DEPTH:
        raise ValueError(f'subflow nesting exceeds MAX_SUBFLOW_DEPTH ({MAX_SUBFLOW_DEPTH})')

    nodes = defn.get('nodes') or []
    edges = defn.get('edges') or []
    if not any(isinstance(n, dict) and n.get('type') == 'subflow'
               and resolve_scope(n) == 'inline' for n in nodes):
        return copy.deepcopy(defn)

    out_nodes: list[dict] = []
    out_edges: list[dict] = [dict(e) for e in edges if isinstance(e, dict)]

    for node in nodes:
        if not isinstance(node, dict) or node.get('type') != 'subflow':
            out_nodes.append(copy.deepcopy(node))
            continue
        # Isolated subflows are a context boundary, not a macro — leave the
        # node (and its embedded child) intact for the nested executor.
        if resolve_scope(node) == 'isolated':
            out_nodes.append(copy.deepcopy(node))
            continue

        sid = node.get('id')
        params = node.get('params') or {}
        child = params.get('definition')
        if child is None:
            ref = params.get('ref')
            if not (resolver and ref):
                raise ValueError(f'subflow {sid!r} has a ref {ref!r} but no '
                                 'resolver was supplied to expand it')
            child = resolver(ref)
            if not isinstance(child, dict):
                raise ValueError(f'subflow {sid!r} ref {ref!r} did not resolve '
                                 'to a definition')
        # Recursively flatten the child first.
        child = expand_subflows(child, resolver=resolver, _depth=_depth + 1)

        cnodes = [n for n in (child.get('nodes') or []) if isinstance(n, dict)]
        cedges = [e for e in (child.get('edges') or []) if isinstance(e, dict)]
        prefix = f'{sid}/'

        def _pid(cid: str) -> str:
            return prefix + cid

        child_starts = {n['id'] for n in cnodes if n.get('kind') == 'start'}
        child_stops = {n['id'] for n in cnodes if n.get('kind') == 'stop'}

        # Inner entry nodes = successors of a child start; exit nodes =
        # predecessors of a child stop. These become the rewire anchors.
        entries = [e['to'] for e in cedges if e.get('from') in child_starts]
        exits = [e['from'] for e in cedges if e.get('to') in child_stops]

        # Splice inner nodes (minus start/stop), namespaced.
        for cn in cnodes:
            if cn.get('id') in child_starts or cn.get('id') in child_stops:
                continue
            spliced = copy.deepcopy(cn)
            spliced['id'] = _pid(cn['id'])
            out_nodes.append(spliced)

        # Inner edges (minus those touching child start/stop), namespaced.
        for ce in cedges:
            s, d = ce.get('from'), ce.get('to')
            if s in child_starts or d in child_stops or s in child_stops or d in child_starts:
                continue
            out_edges.append({'from': _pid(s), 'to': _pid(d)})

        # Rewire parent edges that touched the subflow node.
        rewired: list[dict] = []
        for e in out_edges:
            if e.get('to') == sid:
                for ent in entries:
                    rewired.append({'from': e['from'], 'to': _pid(ent)})
            elif e.get('from') == sid:
                for ex in exits:
                    rewired.append({'from': _pid(ex), 'to': e['to']})
            else:
                rewired.append(e)
        out_edges = rewired

    result = {
        'schema': defn.get('schema', SCHEMA_ID),
        'name': defn.get('name', ''),
        'nodes': out_nodes,
        'edges': out_edges,
    }
    layout_definition(result)
    return result


def layout_definition(defn: dict, *, x_gap: int = 230, y_gap: int = 150,
                      x0: int = 40, y0: int = 30) -> dict:
    """Assign node ``pos`` by graph layering (pure; returns *defn*).

    The frontend is a thin renderer, so position computation lives here.
    Layers are derived by relaxing ``layer[v] = max(layer[u]+1)`` over
    every edge, bounded by ``len(nodes)`` passes so a cycle (e.g. an
    endpoint loop's critic→loop back-edge) can't spin forever. Nodes are
    then spread horizontally within their layer.

    Mutates each node's ``pos`` in place and returns the same dict for
    chaining. Nodes already carrying a plausible ``pos`` are repositioned
    too (the LLM rarely supplies good coordinates).
    """
    nodes = defn.get('nodes') or []
    edges = defn.get('edges') or []
    if not nodes:
        return defn

    ids = [n.get('id') for n in nodes if isinstance(n, dict) and n.get('id')]
    id_set = set(ids)
    indeg: dict[str, int] = {nid: 0 for nid in ids}
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in id_set and d in id_set:
            adj[s].append(d)
            indeg[d] += 1

    # BFS shortest-path layering from the sources. Using BFS (not
    # longest-path relaxation) means each node is assigned the first
    # depth it is reached at and never revisited, so a loop back-edge
    # (e.g. critic→loop) does NOT inflate downstream layers.
    from collections import deque

    seeds = [n.get('id') for n in nodes
             if isinstance(n, dict) and n.get('id')
             and (n.get('kind') == 'start' or indeg.get(n.get('id'), 0) == 0)]
    if not seeds and ids:
        seeds = [ids[0]]

    layer: dict[str, int] = {}
    queue: deque = deque()
    for sid in seeds:
        layer[sid] = 0
        queue.append(sid)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v not in layer:
                layer[v] = layer[u] + 1
                queue.append(v)

    # Any unreached node (disconnected) → place after the deepest layer.
    max_layer = max(layer.values()) if layer else 0
    for nid in ids:
        layer.setdefault(nid, max_layer + 1)

    # Group by layer.
    by_layer: dict[int, list[str]] = {}
    for nid in ids:
        by_layer.setdefault(layer[nid], []).append(nid)

    # ── Crossing minimization (Sugiyama ordering step) ──
    # Order nodes WITHIN each layer by the barycenter (mean order index)
    # of their neighbors in the adjacent layer, alternating down/up
    # sweeps until it settles. This pulls children directly under their
    # parents, so edges read as mostly-vertical, non-crossing lanes
    # instead of long diagonals. Only the x-order within a layer changes;
    # the layer (y) assigned above is untouched, so layering invariants
    # (loop back-edges stay shallow, orphans/cycles keep their depth) hold.
    undirected: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in id_set and d in id_set and s != d:
            undirected[s].append(d)
            undirected[d].append(s)

    order: dict[str, int] = {}
    for members in by_layer.values():
        for i, nid in enumerate(members):
            order[nid] = i

    layers_sorted = sorted(by_layer)
    for sweep in range(4):
        going_down = sweep % 2 == 0
        seq = layers_sorted if going_down else layers_sorted[::-1]
        for lyr in seq:
            adj_lyr = lyr - 1 if going_down else lyr + 1
            keyed = []
            for nid in by_layer[lyr]:
                refs = [order[v] for v in undirected[nid] if layer.get(v) == adj_lyr]
                # No neighbor in the reference layer → keep current index.
                bary = sum(refs) / len(refs) if refs else float(order[nid])
                keyed.append((bary, order[nid], nid))
            keyed.sort()
            by_layer[lyr] = [nid for _, _, nid in keyed]
            for i, nid in enumerate(by_layer[lyr]):
                order[nid] = i

    # Assign coordinates, centering each layer under the widest one.
    widest = max((len(v) for v in by_layer.values()), default=1)
    id_to_node = {n.get('id'): n for n in nodes if isinstance(n, dict)}
    for lyr in layers_sorted:
        members = by_layer[lyr]
        offset = (widest - len(members)) * x_gap // 2
        for i, nid in enumerate(members):
            node = id_to_node.get(nid)
            if node is not None:
                node['pos'] = {'x': x0 + offset + i * x_gap, 'y': y0 + lyr * y_gap}
    return defn


__all__ = [
    'SCHEMA_ID', 'KNOWN_ROLES', 'CONTROL_KINDS',
    'VALID_TIERS', 'VALID_ISOLATION', 'VALID_ARTIFACT_FORMATS', 'VALID_HUMAN_MODES',
    'VALID_EMITS', 'VALID_SCOPES', 'MAX_SUBFLOW_DEPTH', 'resolve_emits',
    'resolve_scope', 'ROLE_PARAM_SCHEMA', 'VALID_PARAM_KINDS',
    'VALID_IO_TYPES', 'MAX_IO_PORTS', 'DEFAULT_OUTPUT_NAME', 'IO_START_REF',
    'node_output_names', 'parse_io_ref',
    'role_param_schema', 'role_persona', 'render_role_brief',
    'first_executed_role', 'initial_phase_for_flow',
    'validate_definition', 'expand_subflows',
    'layout_definition', 'build_endpoint_definition', 'build_autopilot_definition',
]
