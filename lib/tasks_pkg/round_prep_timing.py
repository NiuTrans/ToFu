"""Round-preparation timing anchors: ROUND_START -> logical MODEL_REQUEST_START.

Responsibility
--------------
Attribute the silent window between a root round's open and its first logical
model request to named preparation steps, and log one ``[RoundPrepTiming]``
JSON record when that window reaches one second (format: docs/LOGGING.md).

Entry points
------------
``begin_round_prep`` / ``end_round_prep``: the root dispatch owns the carrier
lifetime. ``round_prep_step``: one step's monotonic self time. A nested step
pauses its enclosing step, so steps never double count; a dotted name such as
``inputs.guidance`` also reports as ``inputsMs.guidance``. ``round_prep_lap``:
a top-level boundary after an unwrapped call; it names every millisecond since
the previous boundary that no step charged, so a call is attributed without
re-indenting it. ``note_round_prep``: scalar facts such as the barrier's
pending events. ``finish_round_prep``: the logical request start consumes the
carrier and decides whether to log.

Dependencies: the standard library and ``lib.log``. No event, metric label,
durable record, thread or timer is created. A task without a carrier (native
policy, swarm sinks) pays one dict lookup per step.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from lib.log import get_logger

logger = get_logger(__name__)

ROUND_PREP_LOG_THRESHOLD_SECONDS = 1.0
# Always present in ``stepsMs`` (0 when a step did not run) so parsers can
# rely on the keys; ``other`` is the unattributed remainder of ``totalMs``.
ROUND_PREP_STEPS = (
    'round_open', 'hygiene', 'compaction', 'inputs', 'admission', 'build_body',
    'accumulator', 'barrier', 'dispatch', 'receipts', 'request',
)
_CARRIER_KEY = '_roundPrepTiming'
_IDENTITY_FIELDS = (
    ('task_id', 'id'), ('conversation_id', 'convId'), ('turn_id', '_turnId'),
    ('attempt_id', '_attemptId'), ('user_id', '_userId'),
)


class RoundPrepTiming:
    """Self time per step for one root round, on the dispatching thread."""

    __slots__ = ('round_num', 'started', 'steps', 'facts', 'active')

    def __init__(self, round_num: int, started: float):
        self.round_num = round_num
        self.started = started
        self.steps: dict[str, float] = {}
        self.facts: dict[str, object] = {}
        # (step name, monotonic instant its self time last resumed)
        self.active: tuple[str, float] | None = None

    def charge(self, name: str, seconds: float) -> None:
        self.steps[name] = self.steps.get(name, 0.0) + max(0.0, seconds)


def begin_round_prep(task: dict, round_num: int) -> RoundPrepTiming:
    timing = RoundPrepTiming(round_num, time.monotonic())
    task[_CARRIER_KEY] = timing
    return timing


def end_round_prep(task: dict, timing: RoundPrepTiming) -> None:
    """Retire an unconsumed carrier; a request that never started logs nothing."""
    if task.get(_CARRIER_KEY) is timing:
        task.pop(_CARRIER_KEY, None)


@contextmanager
def round_prep_step(task, name: str):
    timing = task.get(_CARRIER_KEY) if isinstance(task, dict) else None
    if not isinstance(timing, RoundPrepTiming):
        yield
        return
    entered = time.monotonic()
    outer = timing.active
    if outer is not None:
        timing.charge(outer[0], entered - outer[1])
    timing.active = (name, entered)
    try:
        yield
    finally:
        left = time.monotonic()
        current = timing.active
        timing.charge(name, left - (current[1] if current is not None else entered))
        timing.active = (outer[0], left) if outer is not None else None


def round_prep_lap(task, name: str) -> None:
    """Charge the not-yet-named time since the previous boundary to ``name``.

    Only a top-level boundary names time: inside an open step the elapsed time
    already belongs to that step, so the lap is a no-op there.
    """
    timing = task.get(_CARRIER_KEY) if isinstance(task, dict) else None
    if not isinstance(timing, RoundPrepTiming) or timing.active is not None:
        return
    elapsed = time.monotonic() - timing.started
    timing.charge(name, elapsed - sum(timing.steps.values()))


def note_round_prep(task, **facts) -> None:
    timing = task.get(_CARRIER_KEY) if isinstance(task, dict) else None
    if isinstance(timing, RoundPrepTiming):
        timing.facts.update(facts)


def _identity(task: dict) -> dict:
    fields = {}
    for field, key in _IDENTITY_FIELDS:
        value = task.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value != '':
            fields[field] = str(value)[:128]
    return fields


def finish_round_prep(task, *, span_id: str = '', request_tag: str = '',
                      tail: str = '') -> dict | None:
    """Consume the carrier at the logical request start; log once when slow.

    ``tail`` names the time since the last boundary, as a final lap would.
    Returns the record (logged or not) for the caller's diagnostics; any
    failure is swallowed because timing must never alter the request.
    """
    try:
        timing = task.pop(_CARRIER_KEY, None) if isinstance(task, dict) else None
        if not isinstance(timing, RoundPrepTiming):
            return None
        total = max(0.0, time.monotonic() - timing.started)
        if tail and timing.active is None:
            timing.charge(tail, total - sum(timing.steps.values()))
        steps = dict.fromkeys(ROUND_PREP_STEPS, 0.0)
        details: dict[str, dict[str, float]] = {}
        for name, seconds in timing.steps.items():
            group, _, detail = name.partition('.')
            steps[group] = steps.get(group, 0.0) + seconds
            if detail:
                details.setdefault(group, {})[detail] = round(seconds * 1000, 1)
        steps_ms = {name: round(seconds * 1000, 1) for name, seconds in steps.items()}
        steps_ms['other'] = round(max(0.0, total - sum(steps.values())) * 1000, 1)
        record = {
            **_identity(task),
            # 1-based like MODEL_REQUEST_START.roundNum and requestTag R<n>;
            # ROUND_START.roundNum is the 0-based index (this minus one).
            'roundNum': timing.round_num + 1,
            'requestTag': str(request_tag or '')[:80],
            'spanId': str(span_id or '')[:160],
            'observedAtMs': int(time.time() * 1000),
            'totalMs': round(total * 1000, 1),
            'stepsMs': steps_ms,
            **{f'{group}Ms': values for group, values in details.items()},
            **timing.facts,
        }
        if total >= ROUND_PREP_LOG_THRESHOLD_SECONDS:
            logger.info('[RoundPrepTiming] %s',
                        json.dumps(record, separators=(',', ':'), default=str))
        return record
    except Exception as error:
        logger.debug('[RoundPrepTiming] dropped: %s', type(error).__name__)
        return None
