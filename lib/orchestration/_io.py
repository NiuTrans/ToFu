"""lib/orchestration/_io.py — Typed node I/O contract (the Dify-style dataflow axis).

Orthogonal to ``role`` (who) and ``emits`` (which side of the chat), a node
may declare a STRICT input/output contract under ``params.io``. This module
owns the I/O constants and the pure helpers that read + validate that block:
:func:`node_output_names`, :func:`parse_io_ref`, :func:`_validate_node_io`,
plus the list-normalizing :func:`_coerce_list` shared by the role-param and
brief-rendering code.

See :mod:`lib.orchestration` for the package overview.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


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
