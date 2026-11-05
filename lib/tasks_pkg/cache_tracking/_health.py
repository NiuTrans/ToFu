"""Same-route prompt-cache read-share split detection.

A request population that stops reusing its prompt cache while another
population on the same model keeps reusing it is invisible round by round:
every cold round looks like "prefix byte-identical, provider reported less
reuse". The 2026-10-23..28 root-lane misses on the Sankuai Claude route hid
behind exactly that verdict for five days although the lane split was already
in the logs (docs/LLM_COST_OPTIMIZATION.md, "Gateway tool-name budget").

Owns:
  * ``SPLIT_DIMENSIONS`` - the record fields a split is measured on;
  * the divergence rule (``SPLIT_MIN_ROUNDS`` / ``SPLIT_DIVERGENCE_RATIO``);
  * ``parse_round_record`` - one ``[CacheRoundRecord]`` log line to a payload;
  * ``ReadShareTally`` / ``read_share_split`` - streaming aggregation shared by
    ``scripts/cache_waste_report.py`` and the nightly optimizer evidence;
  * ``read_share_monitor`` - the process-wide rolling monitor fed by
    ``_detect._emit_round_record``. It logs at most one ``[CacheHealth]``
    WARNING per model per window while a split persists and names the cold
    group for that round's record and break report.

It never changes a verdict bucket, a cache baseline or a request.
Dependencies: standard library and ``lib.log``.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
import json
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

RECORD_MARKER = '[CacheRoundRecord]'
SPLIT_MIN_ROUNDS = 20
SPLIT_DIVERGENCE_RATIO = 0.5
UNRECORDED_GROUP = '(unrecorded)'
MONITOR_WINDOW_S = 3600

SPLIT_ADVICE = (
    'Per-round "upstream" verdicts on the cold side do not establish an '
    'upstream fault: diff what the two groups send (Request Inspector raw '
    'archives) and A/B the difference with scripts/cache_ab_probe.py; see '
    'lib/tasks_pkg/cache_tracking/AGENTS.md.')


def _int_field(rec: dict, key: str) -> int | None:
    value = rec.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _tool_surface_group(rec: dict) -> str:
    tools = _int_field(rec, 'tools')
    if tools is None:
        return UNRECORDED_GROUP
    for edge in (16, 32, 64):
        if tools <= edge:
            return f'<={edge}' if edge == 16 else f'{edge // 2 + 1}-{edge}'
    return '>64'


def _tool_name_bytes_group(rec: dict) -> str:
    name_bytes = _int_field(rec, 'wire_tool_name_bytes')
    if name_bytes is None:
        return UNRECORDED_GROUP
    low = 0
    for edge in (128, 256, 512, 1024):
        if name_bytes <= edge:
            return f'<={edge}' if low == 0 else f'{low + 1}-{edge}'
        low = edge
    return '>1024'


# ``lane`` separates the root agent from child/transient invocations;
# ``tool_surface`` is the logical tool count the task offered; and
# ``tool_name_bytes`` is sum(len(name) + 1) over the provider-visible tool
# names actually sent, the property that decided reuse on the Sankuai route.
SPLIT_DIMENSIONS: dict[str, Callable[[dict], str]] = {
    'lane': lambda rec: str(rec.get('lane') or UNRECORDED_GROUP),
    'tool_surface': _tool_surface_group,
    'tool_name_bytes': _tool_name_bytes_group,
}


def parse_round_record(line: str) -> tuple[str, dict] | None:
    """Return ``(stamp, payload)`` for one ``[CacheRoundRecord]`` log line.

    ``stamp`` is the leading ``YYYY-MM-DD HH:MM:SS`` written by lib/log.py,
    or '' when the head is not a date: a torn write during log rotation can
    leave NUL padding that would sort below every real date and silently
    widen a time filter.
    """
    index = line.find(RECORD_MARKER)
    if index == -1:
        return None
    try:
        payload = json.loads(line[index + len(RECORD_MARKER):].strip())
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    stamp = line[:19]
    if not (len(stamp) == 19 and stamp[:4].isdigit()
            and stamp[4] == '-' and stamp[10] == ' '):
        stamp = ''
    return stamp, payload


def _share(read: int, write: int) -> float | None:
    return read / (read + write) if read + write else None


def _divergences(rows: list[dict], min_rounds: int,
                 divergence_ratio: float) -> list[dict]:
    """Per model: the coldest eligible group versus the warmest one.

    A model diverges when, among its recorded groups with at least
    ``min_rounds`` rounds, the coldest read share is below
    ``divergence_ratio`` of the warmest. Unrecorded rows are never compared.
    """
    found = []
    for model in sorted({row['model'] for row in rows}):
        eligible = [row for row in rows
                    if row['model'] == model
                    and row['group'] != UNRECORDED_GROUP
                    and row['rounds'] >= min_rounds
                    and row['read_share'] is not None]
        if len(eligible) < 2:
            continue
        cold = min(eligible, key=lambda row: (row['read_share'], row['group']))
        warm = max(eligible, key=lambda row: (row['read_share'], row['group']))
        if cold['read_share'] < warm['read_share'] * divergence_ratio:
            found.append({
                'model': model,
                'cold_group': cold['group'], 'cold_share': cold['read_share'],
                'cold_rounds': cold['rounds'],
                'warm_group': warm['group'], 'warm_share': warm['read_share'],
                'warm_rounds': warm['rounds'],
            })
    return found


def describe_divergence(dimension: str, divergence: dict) -> str:
    return (f"{dimension}={divergence['cold_group']} read back "
            f"{divergence['cold_share']:.0%} of its prompt over "
            f"{divergence['cold_rounds']:,} rounds while "
            f"{dimension}={divergence['warm_group']} read "
            f"{divergence['warm_share']:.0%} over "
            f"{divergence['warm_rounds']:,}")


class ReadShareTally:
    """Streaming totals per (dimension, model, group) over rounds with a predecessor.

    Memory grows with the number of distinct groups, never with the number of
    records, so a day of rotated logs can be folded line by line.
    """

    def __init__(self, dimensions: Iterable[str] | None = None):
        names = SPLIT_DIMENSIONS if dimensions is None else dimensions
        self._dimensions = {name: SPLIT_DIMENSIONS[name] for name in names}
        self._totals: dict[tuple[str, str, str], list[int]] = {}

    def add(self, rec: dict) -> None:
        if int(rec.get('call') or 0) <= 1:
            return
        model = str(rec.get('model') or UNRECORDED_GROUP)
        read = int(rec.get('cache_read') or 0)
        write = int(rec.get('cache_write') or 0)
        for dimension, group_of in self._dimensions.items():
            total = self._totals.setdefault(
                (dimension, model, group_of(rec)), [0, 0, 0])
            total[0] += 1
            total[1] += read
            total[2] += write

    def split(self, dimension: str, *, min_rounds: int = SPLIT_MIN_ROUNDS,
              divergence_ratio: float = SPLIT_DIVERGENCE_RATIO) -> dict:
        rows = [{'model': model, 'group': group, 'rounds': rounds,
                 'cache_read': read, 'cache_write': write,
                 'read_share': _share(read, write)}
                for (name, model, group), (rounds, read, write)
                in sorted(self._totals.items()) if name == dimension]
        divergences = _divergences(rows, min_rounds, divergence_ratio)
        return {'dimension': dimension, 'rows': rows,
                'divergent_models': [d['model'] for d in divergences],
                'divergences': divergences,
                'min_rounds': min_rounds,
                'divergence_ratio': divergence_ratio}


def read_share_split(records: Iterable[dict], dimension: str, *,
                     min_rounds: int = SPLIT_MIN_ROUNDS,
                     divergence_ratio: float = SPLIT_DIVERGENCE_RATIO) -> dict:
    """Read share per (model, group of ``dimension``) over rounds with a predecessor."""
    tally = ReadShareTally((dimension,))
    for rec in records:
        tally.add(rec)
    return tally.split(dimension, min_rounds=min_rounds,
                       divergence_ratio=divergence_ratio)


class ReadShareMonitor:
    """Rolling same-route split detector for the running service.

    Rounds fold into one cell per minute per (dimension, model, group); cells
    older than ``window_s`` are dropped, so state is bounded by the handful of
    groups per model times the window's minutes. A diverging model gets at
    most one WARNING per ``window_s``, and one INFO line when a warned split
    clears, so a share hovering at the threshold cannot flood the log.
    """

    def __init__(self, *, window_s: int = MONITOR_WINDOW_S,
                 min_rounds: int = SPLIT_MIN_ROUNDS,
                 divergence_ratio: float = SPLIT_DIVERGENCE_RATIO,
                 clock: Callable[[], float] = time.monotonic):
        self._window_s = window_s
        self._window_minutes = max(1, int(window_s // 60))
        self._min_rounds = min_rounds
        self._divergence_ratio = divergence_ratio
        self._clock = clock
        self._lock = threading.Lock()
        self._cells: dict[tuple[str, str], dict[str, deque]] = {}
        self._live: dict[str, tuple[dict[str, dict], float]] = {}
        self._warned_at: dict[str, float] = {}
        self._announced: set[str] = set()

    def observe(self, rec: dict) -> None:
        if int(rec.get('call') or 0) <= 1:
            return
        model = str(rec.get('model') or UNRECORDED_GROUP)
        read = int(rec.get('cache_read') or 0)
        write = int(rec.get('cache_write') or 0)
        now = self._clock()
        minute = int(now // 60)
        oldest_kept = minute - self._window_minutes
        warn = None
        cleared = None
        with self._lock:
            live = {}
            for dimension, group_of in SPLIT_DIMENSIONS.items():
                groups = self._cells.setdefault((dimension, model), {})
                cells = groups.setdefault(group_of(rec), deque())
                if cells and cells[-1][0] == minute:
                    cells[-1][1] += 1
                    cells[-1][2] += read
                    cells[-1][3] += write
                else:
                    cells.append([minute, 1, read, write])
                rows = []
                for group in list(groups):
                    group_cells = groups[group]
                    while group_cells and group_cells[0][0] <= oldest_kept:
                        group_cells.popleft()
                    if not group_cells:
                        del groups[group]
                        continue
                    group_read = sum(cell[2] for cell in group_cells)
                    group_write = sum(cell[3] for cell in group_cells)
                    rows.append({'model': model, 'group': group,
                                 'rounds': sum(cell[1] for cell in group_cells),
                                 'read_share': _share(group_read, group_write)})
                found = _divergences(rows, self._min_rounds,
                                     self._divergence_ratio)
                if found:
                    live[dimension] = found[0]
            if live:
                self._live[model] = (live, now)
                last = self._warned_at.get(model)
                if last is None or now - last >= self._window_s:
                    self._warned_at[model] = now
                    self._announced.add(model)
                    warn = live
            elif model in self._live:
                previous, _ = self._live.pop(model)
                if model in self._announced:
                    self._announced.discard(model)
                    cleared = previous
        if warn:
            logger.warning(
                '[CacheHealth] same-route prompt-cache split on model=%s over '
                'the last %d min: %s. %s', model, self._window_minutes,
                '; '.join(describe_divergence(dimension, divergence)
                          for dimension, divergence in sorted(warn.items())),
                SPLIT_ADVICE)
        if cleared:
            logger.info('[CacheHealth] same-route split on model=%s cleared '
                        '(%s)', model, ', '.join(sorted(cleared)))

    def cold_group(self, rec: dict) -> str:
        """Name the live split(s) whose cold side this round belongs to, else ''.

        A first call had nothing to read back, so it is never labelled.
        """
        if int(rec.get('call') or 0) <= 1:
            return ''
        model = str(rec.get('model') or UNRECORDED_GROUP)
        now = self._clock()
        with self._lock:
            entry = self._live.get(model)
            # A split last seen more than a window ago is not evidence about
            # this round; the model's next observation recomputes it.
            if entry is None or now - entry[1] > self._window_s:
                return ''
            live = entry[0]
            return ','.join(
                f"{dimension}={divergence['cold_group']}"
                for dimension, divergence in sorted(live.items())
                if SPLIT_DIMENSIONS[dimension](rec) == divergence['cold_group'])


read_share_monitor = ReadShareMonitor()
