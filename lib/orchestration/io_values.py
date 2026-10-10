"""Pure Typed-I/O authored-value and runtime-reference helpers."""

from __future__ import annotations

from lib.orchestration.io_contract import DEFAULT_OUTPUT_NAME


def _coerce_list(value) -> list[str]:
    """Normalize a textarea/list field to non-empty string values."""
    if isinstance(value, str):
        items = value.split('\n')
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out = []
    for item in items:
        normalized = str(item).strip()
        if normalized:
            out.append(normalized)
    return out


def node_output_names(node: dict) -> list[str]:
    """Return declared output names or the legacy implicit text output."""
    io = (node.get('params') or {}).get('io')
    if isinstance(io, dict):
        outputs = io.get('outputs')
        if isinstance(outputs, list):
            names = [
                output.get('name')
                for output in outputs
                if isinstance(output, dict)
                and isinstance(output.get('name'), str)
                and output.get('name').strip()
            ]
            if names:
                return names
    return [DEFAULT_OUTPUT_NAME]


def parse_io_ref(ref: str) -> tuple[str, str | None]:
    """Split ``node.output`` into its producer and optional output name."""
    if not isinstance(ref, str):
        return '', None
    ref = ref.strip()
    if '.' in ref:
        node_id, _, output = ref.partition('.')
        return node_id, (output or None)
    return ref, None


__all__ = ['_coerce_list', 'node_output_names', 'parse_io_ref']
