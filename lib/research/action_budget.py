"""Explicit cumulative policy for research actions; zero keeps progress unbounded.

Entry points: normalize_action_logical_budget and action_logical_budget_schema.
The observation contract owns the schema; existing meter snapshots own recovery
policy. This module creates no task, worker, storage record or replay authority.
"""
from copy import deepcopy
from functools import lru_cache

from lib.error_envelope import BadRequest
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _schema():
    path = Path(__file__).resolve().parents[2] / 'contracts/research_action_observation_v1.schema.json'
    return json.loads(path.read_text())['$defs']['actionLogicalBudget']


def action_logical_budget_schema():
    return deepcopy(_schema())


def normalize_action_logical_budget(value=None):
    import jsonschema
    from .action_observations import _validator
    if value is not None and not isinstance(value, dict):
        raise BadRequest('logical_budget must be an object', field='logical_budget')
    budget = {'token_budget': 0, 'dispatch_budget': 0} | (value or {})
    try:
        _validator('actionLogicalBudget').validate(budget)
    except jsonschema.ValidationError as exc:
        raise BadRequest(f'logical_budget: {exc.message[:200]}',
                         field='logical_budget') from exc
    if any(type(budget[key]) is not int for key in ('token_budget', 'dispatch_budget')):
        raise BadRequest('logical_budget values must be integers', field='logical_budget')
    return budget
