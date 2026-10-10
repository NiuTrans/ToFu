"""Immutable automation calendar queries over the shared five-field parser.

Entry point: CronSchedule. Queries take explicit aware clocks and return UTC
slots; no storage, timer, model call or hidden clock lives here. The automation
schema owns gap/fold, catch-up and calendar bounds. Legacy cron matching keeps
its existing owner in cron.py; this module supplies timezone-aware slot lookup.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from lib.automations.contract import SCHEDULE
from .cron import validate_cron_expression


@dataclass(frozen=True, slots=True, init=False)
class CronSchedule:
    expression: str
    timezone_name: str
    _zone: ZoneInfo
    _fields: tuple[frozenset[int], ...]

    def __init__(self, expression: str, timezone_name: str):
        # The standard library rejects absolute/relative TZ paths. Only named
        # IANA zones enter this boundary, never deployment-local file handles.
        zone = ZoneInfo(timezone_name)
        fields = validate_cron_expression(expression)
        # A leap year contains every Gregorian month/day combination. Weekday
        # constraints repeat over the schema-owned 400-year calendar cycle.
        if not any(day <= monthrange(2000, month)[1]
                   for month in fields[3] for day in fields[2]):
            raise ValueError('Cron expression selects no possible calendar date')
        object.__setattr__(self, 'expression', expression)
        object.__setattr__(self, 'timezone_name', timezone_name)
        object.__setattr__(self, '_zone', zone)
        object.__setattr__(self, '_fields', fields)

    @staticmethod
    def _utc(clock: datetime) -> datetime:
        if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError('Calendar queries require an explicit timezone-aware clock')
        return clock.astimezone(timezone.utc)

    def _dates(self, start: date, *, forward: bool):
        """Jump selected months/days, bounded by one Gregorian calendar cycle."""
        cycle = SCHEDULE['calendarCycleYears']
        years = (range(start.year, min(9999, start.year + cycle) + 1) if forward
                 else range(start.year, max(1, start.year - cycle) - 1, -1))
        months = sorted(self._fields[3], reverse=not forward)
        days = sorted(self._fields[2], reverse=not forward)
        weekdays = {(day - 1) % 7 for day in self._fields[4]}
        for year in years:
            for month in months:
                last_day = monthrange(year, month)[1]
                for day in days:
                    if day > last_day:
                        continue
                    candidate = date(year, month, day)
                    if (candidate.weekday() in weekdays
                            and (candidate >= start if forward else candidate <= start)):
                        yield candidate

    def _slots(self, day: date):
        for hour in sorted(self._fields[1]):
            for minute in sorted(self._fields[0]):
                wall = datetime(day.year, day.month, day.day, hour, minute)
                first = wall.replace(tzinfo=self._zone, fold=0)
                second = wall.replace(tzinfo=self._zone, fold=1)
                for local in ((first,) if first.utcoffset() == second.utcoffset()
                              else (first, second)):
                    try:
                        slot = local.astimezone(timezone.utc)
                        # A gap normalizes to a different wall time. Reject it;
                        # both folds of an actual repeated minute round-trip.
                        if slot.astimezone(self._zone).replace(tzinfo=None) == wall:
                            yield slot
                    except OverflowError:
                        # Only UTC conversion beyond datetime's finite year
                        # range is skipped; parsed zone/field errors propagate.
                        if day.year not in {1, 9999}:
                            raise

    def _nearest(self, clock: datetime, *, forward: bool) -> datetime | None:
        boundary = self._utc(clock)
        # datetime permits offsets strictly inside +/-24 hours. An instant
        # after this UTC clock cannot have a local date earlier than its UTC
        # date minus one; reverse lookup has the symmetric upper bound. This
        # also avoids overflowing a local clock near datetime's year limits.
        ordinal = boundary.date().toordinal() + (-1 if forward else 1)
        start = date.fromordinal(max(1, min(date.max.toordinal(), ordinal)))
        best = None
        for day in self._dates(start, forward=forward):
            if best is not None:
                # Offset bounds prove that later calendar dates cannot beat
                # the best UTC instant. Do not assume wall-time ordering in a
                # repeated hour; every candidate on its date is compared.
                if (day.toordinal() > best.date().toordinal() + 1 if forward
                        else day.toordinal() < best.date().toordinal() - 1):
                    break
            for slot in self._slots(day):
                eligible = slot > boundary if forward else slot <= boundary
                if eligible and (best is None or (slot < best if forward else slot > best)):
                    best = slot
        return best

    def next_after(self, clock: datetime) -> datetime | None:
        """The next distinct UTC slot, strictly after the supplied instant."""
        return self._nearest(clock, forward=True)

    def latest_between(self, after: datetime, through: datetime) -> datetime | None:
        """One latest UTC slot in (after, through], without replay enumeration."""
        lower, upper = self._utc(after), self._utc(through)
        if upper < lower:
            raise ValueError('Calendar interval regressed')
        if upper == lower:
            return None
        candidate = self._nearest(upper, forward=False)
        return candidate if candidate is not None and candidate > lower else None

    def due_slot(self, after: datetime, now: datetime, *, catch_up: str) -> datetime | None:
        """Select one latest slot, or only the current UTC minute in skip mode."""
        if catch_up not in {'latest', 'skip'}:
            raise ValueError('Unsupported automation catch-up policy')
        candidate = self.latest_between(after, now)
        if (catch_up == 'skip' and candidate is not None
                and candidate.replace(second=0, microsecond=0) != self._utc(now).replace(second=0, microsecond=0)):
            return None
        return candidate
