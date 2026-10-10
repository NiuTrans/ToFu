"""Explicit cumulative policy for research actions; zero keeps progress unbounded.

Entry points: normalize_action_logical_budget and action_logical_budget_schema.
The observation contract owns the schema; existing meter snapshots own recovery
policy. This module creates no task, worker, storage record or replay authority.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _schema():
    path = Path(__file__).resolve().parents[2] / 'contracts/research_action_observation_v1.schema.json'
    return json.loads(path.read_text())['$defs']['actionLogicalBudget']


def action_logical_budget_schema():
    return deepcopy(_schema())


def normalize_action_logical_budget(value=None):
    from .action_observations import _validate
    if value is not None and not isinstance(value, dict):
        raise ValueError('logical_budget must be an object')
    budget = {'token_budget': 0, 'dispatch_budget': 0} | (value or {})
    _validate(budget, 'actionLogicalBudget')
    if any(type(budget[key]) is not int for key in ('token_budget', 'dispatch_budget')):
        raise ValueError('logical_budget values must be integers')
    return budget
