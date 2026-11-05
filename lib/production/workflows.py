"""Fixed, inspectable agent-tool workflows over the production stage owner.

Recipes supply trusted callbacks, node kinds and JSON output contracts. This
module validates and compiles that declaration into ordinary ``Stage`` objects;
``run_stages`` still owns execution, retry, cancellation and checkpoints. A
description grants no tool, model, filesystem or background-task authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
from itertools import islice
import json
from pathlib import Path
from typing import Any

from lib.production.stages import Stage, run_independent_stages, run_stages


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


@lru_cache(maxsize=1)
def _definition_validator():
    from jsonschema import Draft202012Validator
    path = Path(__file__).resolve().parents[2] / 'contracts/agent_tool_workflow_v1.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _output_validator(schema: dict):
    from jsonschema import Draft202012Validator
    # Contracts are self-contained. A validation gate must never fetch a
    # remote schema or consult a mutable external resource during recovery.
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$id', '$anchor', '$dynamicRef', '$dynamicAnchor'):
                    raise ValueError(f'output contract does not allow {key}')
                if key == '$ref' and (not isinstance(item, str) or
                                      not item.startswith('#/')):
                    raise ValueError('output contract references must be local')
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(deepcopy(schema))


@dataclass(frozen=True)
class WorkflowNode:
    """One fixed node; model work is contained within its trusted callback."""

    stage: Stage
    execution: str
    output_schema: dict


@dataclass(frozen=True)
class AgentToolWorkflow:
    """An authored sequence, never a model-supplied executable graph.

    Model nodes use the existing dispatch/invocation owners. Their output may
    contain content or proposals, but cannot replace the following nodes or
    declare delivery. Domain gates remain authoritative for artifact quality.
    """

    name: str
    version: str
    nodes: tuple[WorkflowNode, ...]
    mode: str = 'sequence'

    def describe(self) -> dict:
        rows = []
        names = set()
        for node in self.nodes:
            stage = node.stage
            if stage.name in names:
                raise ValueError(f'duplicate workflow node: {stage.name}')
            names.add(stage.name)
            if not callable(stage.run) or (stage.gate is not None and not callable(stage.gate)):
                raise ValueError(f'workflow node {stage.name} requires callable bindings')
            rows.append({
                'name': stage.name, 'execution': node.execution,
                'output_schema': deepcopy(node.output_schema),
                'gate': stage.gate is not None, 'retry': stage.retry,
                'resumable': stage.resumable,
                'checkpoint_version': stage.checkpoint_version,
                'resume_ttl_s': stage.resume_ttl_s,
            })
        definition = {'schema_version': 'tofu.agent-tool-workflow/v1',
                      'name': self.name, 'version': self.version,
                      'mode': self.mode, 'nodes': rows}
        try:
            _canonical(definition)
            _definition_validator().validate(definition)
            for row in rows:
                _output_validator(row['output_schema'])
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f'invalid agent-tool workflow: {exc}') from exc
        return definition

    @property
    def digest(self) -> str:
        return _digest(self.describe())


def _contract_gate(validator, gate):
    def validate(ctx, artifact):
        try:
            _canonical(artifact)
        except (TypeError, ValueError):
            return ['output contract: artifact must be finite JSON']
        errors = [f'output contract at {error.json_path[:256]}: {error.message[:1000]}'
                  for error in islice(validator.iter_errors(artifact), 8)]
        if errors:
            return errors
        return list(gate(ctx, artifact) or []) if gate is not None else []
    return validate


def run_workflow(workflow: AgentToolWorkflow, ctx: dict, *, state_path: str,
                 emit=None, abort_check=None, abort_exceptions: tuple = (),
                 dependent_stage_names: tuple[str, ...] = ()) -> dict:
    """Validate all nodes before work, then enter the existing stage executor.

    Prefix fingerprints bind checkpoints to graph order, node schemas and
    implementation versions. Changing one node invalidates its dependent
    suffix; unchanged prefixes retain the existing revalidation and TTL rules.
    """
    definition = workflow.describe()
    if dependent_stage_names and workflow.mode != 'independent':
        raise ValueError('dependent_stage_names requires independent workflow nodes')
    root = _digest({'name': workflow.name, 'version': workflow.version,
                    'mode': workflow.mode})
    prefix = root
    stages = []
    for node, row in zip(workflow.nodes, definition['nodes'], strict=True):
        prefix = _digest({'prefix': root if workflow.mode == 'independent' else prefix,
                          'node': row})
        stages.append(replace(
            node.stage,
            checkpoint_contract=prefix,
            gate=_contract_gate(_output_validator(row['output_schema']), node.stage.gate)))
    if emit is not None:
        # Same observational semantics as the underlying stage event sink.
        try:
            emit({'type': 'workflow_started', 'workflow': definition,
                  'workflow_digest': _digest(definition)})
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).debug('workflow event delivery failed', exc_info=True)
    if workflow.mode == 'independent':
        return run_independent_stages(
            stages, ctx, state_path=state_path, emit=emit,
            dependent_stage_names=dependent_stage_names,
            abort_check=abort_check, abort_exceptions=abort_exceptions)
    return run_stages(stages, ctx, state_path=state_path, emit=emit,
                      abort_check=abort_check, abort_exceptions=abort_exceptions)
