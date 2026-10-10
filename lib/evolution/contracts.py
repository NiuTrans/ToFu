"""Contract authority for the self-evolution module.

Responsibility: capture finite contract originals once per process, lazily parse
and validate the P0 wire contracts
(``contracts/evolution_goal_v1.schema.json``, ``evolution_plan_v1``,
``evolution_episode_v1``) and own the module's typed errors. Entry points:
``load_schema`` / ``contract_validator`` / ``validate_goal`` / ``validate_plan`` /
``validate_episode``. Dependencies:
``jsonschema`` only; no storage, no routes.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import jsonschema

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / 'contracts'

GOAL_SCHEMA = 'evolution_goal_v1.schema.json'
PLAN_SCHEMA = 'evolution_plan_v1.schema.json'
EPISODE_SCHEMA = 'evolution_episode_v1.schema.json'
COST_PRIORS_SCHEMA = 'evolution_cost_priors_v1.schema.json'
STRATEGY_SCHEMA = 'strategy_template_v1.schema.json'
ANTI_PATTERN_SCHEMA = 'evolution_anti_pattern_v1.schema.json'
MUTATION_SCHEMA = 'evolution_mutation_v1.schema.json'
CAPABILITY_GAP_SCHEMA = 'evolution_capability_gap_v1.schema.json'
HOLDOUT_SCHEMA = 'evolution_holdout_v1.schema.json'
REGRESSION_SCHEMA = 'evolution_regression_v1.schema.json'
SKILL_SCHEMA = 'evolution_skill_v1.schema.json'
HYPOTHESIS_SCHEMA = 'evolution_hypothesis_v1.schema.json'
GATE_POLICY_SCHEMA = 'evolution_gate_policy_v1.schema.json'
DISTILLATION_SCHEMA = 'evolution_distillation_v1.schema.json'
OBSERVATION_SCHEMA = 'evolution_observation_v1.schema.json'
PROGRAM_SCHEMA = 'evolution_program_v1.schema.json'


class EvolutionContractError(ValueError):
    """A payload violated the wire contract; never persisted."""


class EvolutionStateError(RuntimeError):
    """A lifecycle refusal: missing record, unconfirmed plan, terminal re-settle,
    or learning rights exceeded by an exploratory episode."""


@lru_cache(maxsize=1)
def _schema_catalog() -> dict[str, str]:
    """Freeze finite contract originals on first use; parse each lazily."""
    return {path.name: path.read_text(encoding='utf-8')
            for path in CONTRACTS_DIR.iterdir()
            if path.name.endswith('.schema.json') and path.is_file()}


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    # Loaded contract bytes stay pinned while their Python consumers remain
    # alive. LRU eviction could reload changed files beside an old validator.
    # The frozen finite namespace bounds retained entries without evicting
    # authority; new contract names enter with a new application process.
    if type(name) is not str or name not in _schema_catalog():
        raise FileNotFoundError(f'Contract is outside the process catalogue: {name}')
    return json.loads(_schema_catalog()[name])


@lru_cache(maxsize=32)
def contract_validator(name: str, definition: str | None = None):
    schema = load_schema(name)
    if definition is not None:
        # Keep the root definition namespace available to sibling $ref links.
        schema = {'$schema': schema.get('$schema'), '$defs': schema['$defs'],
                  '$ref': f'#/$defs/{definition}'}
    validator_type = jsonschema.validators.validator_for(schema)
    validator_type.check_schema(schema)
    from referencing import Registry, Resource
    from referencing.exceptions import NoSuchResource
    def retrieve(uri):
        if uri != 'https://tofu.local/contracts/research_program_v1.schema.json':
            raise NoSuchResource(ref=uri)
        return Resource.from_contents(load_schema('research_program_v1.schema.json'))
    registry = Registry(retrieve=retrieve)
    if name == PROGRAM_SCHEMA:
        registry = registry.with_resource('urn:tofu:evolution-run:v1',
            Resource.from_contents(load_schema('evolution_run_v1.schema.json')))
    return validator_type(schema, registry=registry)


def validate_program(payload: dict, definition: str | None = None) -> dict:
    """Programs reference the run schema rather than copying model/budget policy."""
    try:
        contract_validator(PROGRAM_SCHEMA, definition).validate(payload)
    except jsonschema.ValidationError as exc:
        raise EvolutionContractError(f'evolution program: {exc.message[:300]}') from exc
    return payload


def _validate(payload: dict, schema_name: str, kind: str) -> dict:
    if not isinstance(payload, dict):
        raise EvolutionContractError(f'{kind} must be an object')
    try:
        contract_validator(schema_name).validate(payload)
    except jsonschema.ValidationError as exc:
        message = (exc.message or 'contract violation')[:300]
        raise EvolutionContractError(f'{kind}: {message}') from exc
    return payload


def validate_goal(payload: dict) -> dict:
    return _validate(payload, GOAL_SCHEMA, 'evolution goal')


def validate_plan(payload: dict) -> dict:
    return _validate(payload, PLAN_SCHEMA, 'evolution plan')


def validate_episode(payload: dict) -> dict:
    return _validate(payload, EPISODE_SCHEMA, 'evolution episode')


def validate_cost_priors(payload: dict) -> dict:
    return _validate(payload, COST_PRIORS_SCHEMA, 'evolution cost priors')


def validate_strategy(payload: dict) -> dict:
    return _validate(payload, STRATEGY_SCHEMA, 'strategy template')


def validate_anti_pattern(payload: dict) -> dict:
    return _validate(payload, ANTI_PATTERN_SCHEMA, 'evolution anti-pattern')


def validate_mutation(payload: dict) -> dict:
    return _validate(payload, MUTATION_SCHEMA, 'evolution mutation')


def validate_capability_gap(payload: dict) -> dict:
    return _validate(payload, CAPABILITY_GAP_SCHEMA, 'evolution capability gap')


def validate_holdout_item(payload: dict) -> dict:
    return _validate(payload, HOLDOUT_SCHEMA, 'evolution holdout item')


def validate_regression_receipt(payload: dict) -> dict:
    return _validate(payload, REGRESSION_SCHEMA, 'evolution regression receipt')


def validate_skill(payload: dict) -> dict:
    return _validate(payload, SKILL_SCHEMA, 'evolution forged skill')


def validate_hypothesis(payload: dict) -> dict:
    return _validate(payload, HYPOTHESIS_SCHEMA, 'evolution hypothesis')


def validate_gate_policy(payload: dict) -> dict:
    return _validate(payload, GATE_POLICY_SCHEMA, 'evolution gate policy')


def validate_distillation(payload: dict) -> dict:
    return _validate(payload, DISTILLATION_SCHEMA, 'evolution distillation')


def validate_observation(payload: dict) -> dict:
    return _validate(payload, OBSERVATION_SCHEMA, 'evolution observation')
