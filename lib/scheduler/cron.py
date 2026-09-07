"""lib/scheduler/cron.py — Cron expression parsing and matching."""

from datetime import datetime, timedelta


def _parse_cron_field(field, min_val, max_val):
    """Parse a single cron field. Returns set of valid values."""
    values = set()

    for part in field.split(','):
        part = part.strip()

        # * or */N
        if part == '*':
            values.update(range(min_val, max_val + 1))
        elif part.startswith('*/'):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif '-' in part:
            # Range: 1-5 or 1-5/2
            range_part, *step_part = part.split('/')
            start, end = map(int, range_part.split('-'))
            step = int(step_part[0]) if step_part else 1
            values.update(range(start, end + 1, step))
        else:
            values.add(int(part))

    return values


def cron_matches(cron_expr, dt=None):
    """Check if a datetime matches a cron expression.

    Format: minute hour day_of_month month day_of_week
    Examples:
        '*/5 * * * *'     — every 5 minutes
        '0 9 * * *'       — daily at 9:00 AM
        '0 9 * * 1-5'     — weekdays at 9:00 AM
        '30 8,12,18 * * *' — at 8:30, 12:30, 18:30
        '0 0 1 * *'       — first day of month at midnight
    """
    if dt is None:
        dt = datetime.now()

    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f'Invalid cron expression (need 5 fields): {cron_expr}')

    minute_set = _parse_cron_field(fields[0], 0, 59)
    hour_set = _parse_cron_field(fields[1], 0, 23)
    dom_set = _parse_cron_field(fields[2], 1, 31)
    month_set = _parse_cron_field(fields[3], 1, 12)
    dow_set = _parse_cron_field(fields[4], 0, 6)  # 0=Monday in Python, but cron 0=Sunday

    # Convert cron dow (0=Sun) to Python dow (0=Mon)
    python_dow = set()
    for d in dow_set:
        python_dow.add((d - 1) % 7)  # 0(Sun)→6, 1(Mon)→0, ...

    return (dt.minute in minute_set and
            dt.hour in hour_set and
            dt.day in dom_set and
            dt.month in month_set and
            dt.weekday() in python_dow)


def next_cron_run(cron_expr, from_dt=None, max_lookahead_days=366):
    """Find the next datetime that matches the cron expression.

    Looks ahead up to ``max_lookahead_days`` (default ~1 year) so that
    sparse schedules like ``0 0 1 * *`` (monthly) or ``30 14 28 2 *``
    (once a year) resolve instead of returning ``None``.  Days that cannot
    possibly match (wrong month / day-of-month / day-of-week) are skipped
    whole, so the worst case is ~366 day-checks plus one day of
    minute-checks — not 527 040 minute iterations.
    """
    dt = from_dt or datetime.now()
    dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f'Invalid cron expression (need 5 fields): {cron_expr}')

    # Pre-parse the date fields once for day-level skipping. These mirror
    # cron_matches' AND semantics exactly, so a day we skip here could never
    # have matched cron_matches anyway.
    dom_set = _parse_cron_field(fields[2], 1, 31)
    month_set = _parse_cron_field(fields[3], 1, 12)
    dow_set = _parse_cron_field(fields[4], 0, 6)
    python_dow = {(d - 1) % 7 for d in dow_set}

    end = dt + timedelta(days=max_lookahead_days)
    while dt < end:
        day_ok = (dt.month in month_set and dt.day in dom_set
                  and dt.weekday() in python_dow)
        if not day_ok:
            # Jump straight to the start of the next day.
            dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cron_matches(cron_expr, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


def describe_cron(cron_expr):
    """Human-readable description of a cron expression.

    Handles minute/hour lists and ranges so multi-time schedules like
    ``30 8,12,18 * * *`` render fully ("at 08:30, 12:30, 18:30") instead
    of dropping all but the first hour.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return cron_expr

    m, h, dom, mon, dow = fields

    parts = []

    # Time
    if m.startswith('*/'):
        step = m[2:]
        parts.append(f'every {step} minutes' if h == '*'
                     else f'every {step} minutes during hour(s) {h}')
    elif m == '*':
        parts.append('every minute' if h == '*'
                     else f'every minute of hour(s) {h}')
    elif h == '*':
        parts.append(f'at minute {m} of every hour')
    else:
        # Both minute and hour are concrete — enumerate the actual times so
        # lists/ranges aren't silently truncated.
        try:
            minutes = sorted(_parse_cron_field(m, 0, 59))
            hours = sorted(_parse_cron_field(h, 0, 23))
            times = [f'{hh:02d}:{mm:02d}' for hh in hours for mm in minutes]
            if 0 < len(times) <= 8:
                parts.append('at ' + ', '.join(times))
            elif times:
                parts.append(f'at {len(times)} times daily '
                             f'({times[0]}…{times[-1]})')
        except (ValueError, TypeError):
            parts.append(f'at {h}:{m.zfill(2)}')

    # Day
    dow_names = {0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat'}
    if dom != '*' and mon != '*':
        parts.append(f'on day {dom} of month {mon}')
    elif dom != '*':
        parts.append(f'on day {dom} of each month')
    elif dow != '*':
        if dow == '1-5':
            parts.append('on weekdays')
        elif dow == '0,6':
            parts.append('on weekends')
        else:
            days = [dow_names.get(int(d.strip()), d) for d in dow.split(',')]
            parts.append(f'on {", ".join(days)}')
    else:
        if h != '*' and m != '*' and not m.startswith('*/'):
            parts.append('daily')

    return ', '.join(parts) if parts else cron_expr


__all__ = ['cron_matches', 'next_cron_run', 'describe_cron']
