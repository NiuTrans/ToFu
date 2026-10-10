"""Runtime Typed-I/O state for orchestration execution.

The graph interpreter delegates its data plane here: producer output
publication, named input resolution and artifact change-manifest projection.
This state has no control-flow, agent, event or persistence dependency.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger
from lib.orchestration.io_contract import IO_START_REF
from lib.orchestration.io_values import node_output_names, parse_io_ref

logger = get_logger(__name__)


class OrchestrationDataflow:
    """Thread-safe output store and typed-input projector for one flow run."""

    def __init__(self, *, lock: Any | None = None):
        self._lock = lock or threading.Lock()
        self._outputs: dict[str, dict] = {}
        self._initial_context = ''

    def set_initial_context(self, value: str) -> None:
        """Publish the flow seed addressable through the ``start`` I/O ref."""
        with self._lock:
            self._initial_context = value or ''

    def publish_outputs(
        self,
        node: dict,
        output: str,
        state_changing_names: list,
        exploratory_count: int,
    ) -> None:
        """Publish one node's implicit or declared outputs."""
        node_id = node.get('id')
        if not node_id:
            return
        output_names = node_output_names(node)
        io_contract = (node.get('params') or {}).get('io')
        type_by_name: dict = {}
        if isinstance(io_contract, dict) and isinstance(
            io_contract.get('outputs'),
            list,
        ):
            for port in io_contract['outputs']:
                if isinstance(port, dict) and isinstance(port.get('name'), str):
                    type_by_name[port['name']] = port.get('type')

        manifest = None
        values: dict = {}
        for name in output_names:
            if type_by_name.get(name) in ('artifact', 'file'):
                if manifest is None:
                    manifest = build_change_manifest(
                        state_changing_names,
                        exploratory_count,
                    )
                values[name] = manifest
            else:
                values[name] = output
        with self._lock:
            self._outputs[node_id] = values

    def compose_inputs(self, node: dict) -> str | None:
        """Resolve declared inputs into labeled context sections.

        ``None`` means the node declares no strict input ports and the caller
        should retain legacy accumulating-context behavior. An empty string
        means strict inputs were declared but none currently resolve.
        """
        io_contract = (node.get('params') or {}).get('io')
        if not isinstance(io_contract, dict):
            return None
        inputs = io_contract.get('inputs')
        if not isinstance(inputs, list) or not inputs:
            return None

        with self._lock:
            output_store = {
                node_id: dict(values)
                for node_id, values in self._outputs.items()
            }
            seed = self._initial_context

        parts: list[str] = []
        for port in inputs:
            if not isinstance(port, dict):
                continue
            source_ref = port.get('from')
            label = port.get('name') or 'input'
            if not isinstance(source_ref, str) or not source_ref.strip():
                continue
            source_id, source_output = parse_io_ref(source_ref)
            if source_id == IO_START_REF:
                value = seed
            else:
                produced = output_store.get(source_id) or {}
                if source_output is not None:
                    value = produced.get(source_output)
                else:
                    value = next(iter(produced.values()), None) if produced else None
            if value is None or value == '':
                logger.debug(
                    '[FlowDataflow] input %r on %s unresolved (from=%r)',
                    label,
                    node.get('id'),
                    source_ref,
                )
                continue
            parts.append(f'## {label}\n{value}')
        return '\n\n'.join(parts)

    def output_snapshot(self) -> dict[str, dict]:
        """Return a detached diagnostic snapshot of published outputs."""
        with self._lock:
            return {
                node_id: dict(values)
                for node_id, values in self._outputs.items()
            }


def build_change_manifest(
    state_changing_names: list,
    exploratory_count: int,
) -> str:
    """Project raw tool names into a deterministic artifact manifest."""
    counts: dict[str, int] = {}
    for name in state_changing_names or []:
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return (
            '## Change manifest\n(no state-changing actions; '
            f'{exploratory_count} exploratory calls)'
        )
    lines = [
        f'- {tool} ×{count}' if count > 1 else f'- {tool}'
        for tool, count in sorted(counts.items())
    ]
    total = sum(counts.values())
    return (
        '## Change manifest\n'
        f'{total} state-changing action(s), '
        f'{exploratory_count} exploratory:\n' + '\n'.join(lines)
    )


__all__ = ['OrchestrationDataflow', 'build_change_manifest']
