"""Per-task soft warnings, deployment defaults, and hard resource ceilings.

Cost, prompt, tool-output, and elapsed limits remain opt-in. Model API rounds
instead inherit one finite deployment profile when unset/zero; explicit task
values may raise or lower it but never cross the process hard ceiling.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from runtime_guards import resolve_resource_budget

from lib.log import get_logger


logger = get_logger(__name__)
MAX_TASK_API_ROUNDS = 1024
_DEFAULT_API_ROUND_RESOURCE = 'TOFU_TASK_MAX_API_ROUNDS'


@dataclass(frozen=True)
class BudgetReading:
    name: str
    used: float
    limit: float
    unit: str

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)


@dataclass(frozen=True)
class BudgetDecision:
    exceeded: BudgetReading | None
    warnings: tuple[BudgetReading, ...]
    remaining: dict[str, float]
    readings: tuple[BudgetReading, ...]


_LIMITS = (
    ('promptTokens', 'maxPromptTokens', 'tokens'),
    ('apiRounds', 'maxApiRounds', 'rounds'),
    ('toolOutputBytes', 'maxToolOutputBytes', 'bytes'),
    ('elapsedSeconds', 'maxTaskSeconds', 'seconds'),
)


def resolve_task_budget_config(
    cfg: Mapping[str, Any] | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return one task config with a finite, hard-capped model-round limit."""
    resolved = dict(cfg or {})
    requested = resolved.get('maxApiRounds')
    try:
        requested_rounds = (
            0 if isinstance(requested, bool) else int(requested or 0)
        )
    except (TypeError, ValueError, OverflowError):
        requested_rounds = 0
    if requested_rounds <= 0:
        requested_rounds = resolve_resource_budget(
            _DEFAULT_API_ROUND_RESOURCE,
            environment,
            maximum=MAX_TASK_API_ROUNDS,
        )
    resolved['maxApiRounds'] = max(
        1,
        min(MAX_TASK_API_ROUNDS, requested_rounds),
    )
    return resolved


def api_round_finalization_reserve(limit: float) -> int:
    """Reserve enough bounded rounds to implement, verify, and answer."""
    return min(64, max(1, int(limit) // 3))


def _positive_number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Budget] invalid numeric value %r: %s', value, exc)
        return 0.0
    return number if number > 0 else 0.0


def _prompt_tokens(usage: dict | None) -> float:
    usage = usage or {}
    # Provider payloads use one of these spellings. Select the first present
    # canonical field rather than summing aliases and double-charging a round.
    for key in ('input_tokens', 'prompt_tokens', 'inputTokens', 'promptTokens'):
        if key in usage:
            return _positive_number(usage.get(key))
    return 0.0


def usage_readings(task: dict, cfg: dict, *, usage: dict | None = None,
                   api_rounds: int = 0, now: float | None = None) \
        -> tuple[BudgetReading, ...]:
    created = _positive_number(task.get('_t_created') or task.get('created_at'))
    elapsed = max(0.0, (now if now is not None else time.time()) - created) \
        if created else 0.0
    used = {
        'promptTokens': _prompt_tokens(usage),
        'apiRounds': max(0, int(api_rounds or 0)),
        'toolOutputBytes': _positive_number(task.get('_toolOutputBytes')),
        'elapsedSeconds': elapsed,
    }
    readings = []
    for name, config_key, unit in _LIMITS:
        limit = _positive_number(cfg.get(config_key))
        if limit:
            readings.append(BudgetReading(name, used[name], limit, unit))
    return tuple(readings)


def evaluate_task_budget(task: dict, cfg: dict, *, usage: dict | None = None,
                         api_rounds: int = 0, now: float | None = None) \
        -> BudgetDecision:
    try:
        soft_ratio = float(cfg.get('taskBudgetSoftRatio') or 0.8)
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Budget] invalid soft ratio %r: %s',
                     cfg.get('taskBudgetSoftRatio'), exc)
        soft_ratio = 0.8
    soft_ratio = max(0.1, min(0.99, soft_ratio))
    readings = usage_readings(
        task, cfg, usage=usage, api_rounds=api_rounds, now=now)
    exceeded = next((reading for reading in readings
                     if reading.used >= reading.limit), None)
    warnings = tuple(reading for reading in readings
                     if reading.used >= reading.limit * soft_ratio
                     and reading.used < reading.limit)
    return BudgetDecision(
        exceeded=exceeded,
        warnings=warnings,
        remaining={reading.name: reading.remaining for reading in readings},
        readings=readings,
    )


def account_tool_output(task: dict, value: Any) -> int:
    """Add the UTF-8 wire size of one settled tool result to ``task``."""
    try:
        if isinstance(value, bytes):
            size = len(value)
        elif isinstance(value, str):
            size = len(value.encode('utf-8', errors='replace'))
        else:
            size = len(json.dumps(
                value, ensure_ascii=False, default=str,
                separators=(',', ':')).encode('utf-8', errors='replace'))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Budget] tool-output serialization fallback: %s', exc)
        size = len(str(value).encode('utf-8', errors='replace'))
    task['_toolOutputBytes'] = int(task.get('_toolOutputBytes') or 0) + size
    return size


__all__ = [
    'BudgetDecision',
    'BudgetReading',
    'MAX_TASK_API_ROUNDS',
    'account_tool_output',
    'api_round_finalization_reserve',
    'evaluate_task_budget',
    'resolve_task_budget_config',
    'usage_readings',
]
