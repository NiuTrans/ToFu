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


def next_cron_run(cron_expr, from_dt=None, max_lookahead_hours=48):
    """Find the next datetime that matches the cron expression."""
    dt = from_dt or datetime.now()
    dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

    end = dt + timedelta(hours=max_lookahead_hours)
    while dt < end:
        if cron_matches(cron_expr, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


def describe_cron(cron_expr):
    """Human-readable description of a cron expression."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return cron_expr

    m, h, dom, mon, dow = fields

    parts = []

    # Time
    if m == '0' and h == '*':
        parts.append('every hour on the hour')
    elif m.startswith('*/'):
        parts.append(f'every {m[2:]} minutes')
    elif h == '*':
        parts.append(f'every hour at minute {m}')
    elif m == '0':
        parts.append(f'at {h}:00')
    else:
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
