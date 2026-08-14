"""Per-task soft warnings and hard resource ceilings.

Limits are disabled when unset/zero, preserving the personal-install defaults.
The guard is intentionally pure apart from :func:`account_tool_output`, making
the boundary semantics easy to test without running an agent loop.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


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


_LIMITS = (
    ('promptTokens', 'maxPromptTokens', 'tokens'),
    ('apiRounds', 'maxApiRounds', 'rounds'),
    ('toolOutputBytes', 'maxToolOutputBytes', 'bytes'),
    ('elapsedSeconds', 'maxTaskSeconds', 'seconds'),
)


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


__all__ = ['BudgetDecision', 'BudgetReading', 'account_tool_output',
           'evaluate_task_budget', 'usage_readings']
