"""lib/optimizer/analyzer.py — Evidence gathering + post-apply metrics.

Builds a compact ``EvidenceBundle`` summarising what happened in the
last 24 h, plus a ``prior_actions`` section describing the effect of
previously-applied actions.  Everything here is read-only except for
writing computed ``outcome_metric`` values back to the action log.
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from lib.config_dir import config_path as _config_path
from lib.log import APP_LOG, AUDIT_LOG_FILE, ERROR_LOG, get_logger

from . import storage

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Evidence model
# ══════════════════════════════════════════════════════════

@dataclass
class EvidenceBundle:
    window_hours: int = 24
    generated_at: str = ''
    # Aggregated counters (small, LLM-friendly)
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    tool_error_counts: dict[str, int] = field(default_factory=dict)
    top_search_domains: list[dict] = field(default_factory=list)
    irrelevant_dropped_domains: list[dict] = field(default_factory=list)
    audit_event_counts: dict[str, int] = field(default_factory=dict)
    error_log_excerpts: list[str] = field(default_factory=list)
    warn_log_excerpts: list[str] = field(default_factory=list)
    daily_report_snippets: list[dict] = field(default_factory=list)
    prior_actions: list[dict] = field(default_factory=list)
    # ── Expanded signals for non-search action types ──
    fetch_timeout_count: int = 0
    fetch_failure_count: int = 0
    rate_limit_429_count: int = 0
    prompt_too_long_count: int = 0
    context_near_full_count: int = 0
    compaction_trigger_count: int = 0
    model_switch_events: list[dict] = field(default_factory=list)
    top_cost_conversations: list[dict] = field(default_factory=list)
    failing_scheduled_tasks: list[dict] = field(default_factory=list)
    idle_proactive_tasks: list[dict] = field(default_factory=list)
    # Fingerprint-clustered recurring failures (the recurring/unresolved
    # issue surface the removed project_error_tracker.py once provided).
    recurring_issues: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════
#  Helpers: log tail + time filtering
# ══════════════════════════════════════════════════════════

_APP_LOG_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})')


def _safe_tail_lines(path: str, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Read the last ``max_bytes`` from a log file and return its lines.

    On missing/unreadable file → returns []."""
    try:
        if not os.path.isfile(path):
            return []
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                # Skip the (likely) partial first line
                f.readline()
            raw = f.read().decode('utf-8', errors='replace')
        return raw.splitlines()
    except Exception as e:
        logger.warning('[Optimizer.analyzer] failed to tail %s: %s', path, e)
        return []


def _parse_app_log_ts(line: str) -> datetime | None:
    m = _APP_LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(f'{m.group(1)} {m.group(2)}', '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] unparseable app.log ts %.40s: %s', line, e)
        return None


def _parse_audit_line(line: str) -> dict | None:
    try:
        return json.loads(line)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] non-JSON audit line (len=%d): %s', len(line), e)
        return None


def _audit_ts_aware(entry: dict) -> datetime | None:
    ts = entry.get('timestamp') or ''
    if not ts:
        return None
    try:
        if ts.endswith('Z'):
            ts = ts[:-1] + '+00:00'
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] bad audit ts %r: %s', ts, e)
        return None


# ══════════════════════════════════════════════════════════
#  Evidence collectors
# ══════════════════════════════════════════════════════════

def _collect_audit_events(cutoff_utc: datetime) -> tuple[dict[str, int], list[dict]]:
    """Return (event_counts, optimizer-related rows)."""
    counts: Counter = Counter()
    optimizer_events: list[dict] = []
    for line in _safe_tail_lines(AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry:
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        ev = str(entry.get('event') or 'unknown')
        counts[ev] += 1
        if ev.startswith('optimizer_'):
            # Keep a small summary (never the whole entry)
            optimizer_events.append({
                'event': ev,
                'timestamp': entry.get('timestamp'),
                'details_preview': json.dumps(
                    {k: v for k, v in entry.items() if k not in ('event', 'timestamp')},
                    ensure_ascii=False, default=str)[:300],
            })
    return dict(counts), optimizer_events


def _collect_error_log_excerpts(cutoff_local: datetime, max_lines: int = 40) -> list[str]:
    excerpts: list[str] = []
    for line in reversed(_safe_tail_lines(ERROR_LOG, max_bytes=2 * 1024 * 1024)):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        excerpts.append(line[:300])
        if len(excerpts) >= max_lines:
            break
    return list(reversed(excerpts))


def _collect_app_log_signals(cutoff_local: datetime) -> dict:
    """Mine logs/app.log for tool / fetch / LLM signals in the window."""
    tool_calls: Counter = Counter()
    tool_errors: Counter = Counter()
    irrelevant_dropped: Counter = Counter()
    warn_excerpts: list[str] = []

    fetch_timeout = 0
    fetch_failure = 0
    rate_limit_429 = 0
    prompt_too_long = 0
    context_near_full = 0
    compaction_trigger = 0

    # Regexes tuned to existing log patterns (lib/search, lib/tasks_pkg, etc.)
    tool_call_re = re.compile(r'\[Tool:([a-zA-Z0-9_]+)\] called')
    tool_fail_re = re.compile(r'\[Tool:([a-zA-Z0-9_]+)\] failed')
    dropped_re = re.compile(
        r'\[Search\].*?IRRELEVANT.*?(?:dropped|filtered).*?([\w.-]+\.[a-zA-Z]{2,})',
        re.IGNORECASE)
    fetch_timeout_re = re.compile(r'\[Fetch\].*Timeout', re.IGNORECASE)
    fetch_fail_re = re.compile(r'\[Fetch\].*(Request failed|failed for)',
                               re.IGNORECASE)
    rl_re = re.compile(r'\b(429|rate.?limit|RateLimitError)\b', re.IGNORECASE)
    prompt_too_long_re = re.compile(r'PromptTooLong|context.{0,4}length.{0,4}exceeded',
                                    re.IGNORECASE)
    ctx_full_re = re.compile(r'context.{0,4}(window|near|almost).{0,4}(full|limit)',
                             re.IGNORECASE)
    compaction_re = re.compile(r'\[Compaction\]|compaction_trigger|compact(ed|ing)',
                               re.IGNORECASE)

    for line in _safe_tail_lines(APP_LOG):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        m = tool_call_re.search(line)
        if m:
            tool_calls[m.group(1)] += 1
        m = tool_fail_re.search(line)
        if m:
            tool_errors[m.group(1)] += 1
        m = dropped_re.search(line)
        if m:
            irrelevant_dropped[m.group(1).lower()] += 1
        if fetch_timeout_re.search(line):
            fetch_timeout += 1
        if fetch_fail_re.search(line):
            fetch_failure += 1
        if rl_re.search(line):
            rate_limit_429 += 1
        if prompt_too_long_re.search(line):
            prompt_too_long += 1
        if ctx_full_re.search(line):
            context_near_full += 1
        if compaction_re.search(line):
            compaction_trigger += 1
        if ' WARNING ' in line and len(warn_excerpts) < 30:
            if any(tag in line for tag in ('[Search]', '[Fetch]', '[Tool:',
                                           '[LLM]', '[Compaction]', '[Dispatch]')):
                warn_excerpts.append(line[:300])

    top_dropped = [
        {'domain': d, 'count': n}
        for d, n in irrelevant_dropped.most_common(10)
    ]
    return {
        'tool_call_counts': dict(tool_calls),
        'tool_error_counts': dict(tool_errors),
        'irrelevant_dropped_domains': top_dropped,
        'warn_excerpts': warn_excerpts,
        'fetch_timeout_count': fetch_timeout,
        'fetch_failure_count': fetch_failure,
        'rate_limit_429_count': rate_limit_429,
        'prompt_too_long_count': prompt_too_long,
        'context_near_full_count': context_near_full,
        'compaction_trigger_count': compaction_trigger,
    }


def _collect_audit_secondary(cutoff_utc: datetime) -> dict:
    """Scan audit.log for structured cost / routing events.

    Returns ``model_switch_events`` (most recent 10).
    """
    model_switches: list[dict] = []
    for line in _safe_tail_lines(AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry:
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        if entry.get('event') == 'model_switch':
            model_switches.append({
                'timestamp': entry.get('timestamp'),
                'old': str(entry.get('old') or '')[:80],
                'new': str(entry.get('new') or '')[:80],
                'reason': str(entry.get('reason') or '')[:80],
                'error': str(entry.get('error') or '')[:160],
            })
    return {
        'model_switch_events': model_switches[-10:],
    }


# Error-log signature table — mirrors debug/triage_errors.py SIGNATURES so the
# nightly loop clusters error.log the same way the manual triage CLI does.
# (Kept in sync intentionally; first match wins.)
_ERROR_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ('PREMATURE STREAM CLOSE',   re.compile(r'PREMATURE STREAM CLOSE', re.I)),
    ('PREFIX MUTATION',          re.compile(r'PREFIX MUTATION', re.I)),
    ('run_command timed out',    re.compile(r'run_command timed out', re.I)),
    ('429 rate-limited',         re.compile(r'\b429\b.*rate.?limit', re.I)),
    ('DISCONNECTED PREMATURELY', re.compile(r'DISCONNECTED PREMATURELY', re.I)),
    ('tool handler raised',      re.compile(r'Tool handler \S+ raised', re.I)),
    ('AttributeError',           re.compile(r'AttributeError')),
    ('KeyError',                 re.compile(r'KeyError')),
    ('ConnectionError',          re.compile(r'ConnectionError|ConnectionResetError')),
    ('Timeout',                  re.compile(r'\bTimeout(Error)?\b')),
    ('Traceback',                re.compile(r'Traceback \(most recent call last\)')),
]


def _classify_error_signature(line: str) -> str:
    for label, rx in _ERROR_SIGNATURES:
        if rx.search(line):
            return label
    return ''


def _collect_recurring_issues(cutoff_local: datetime,
                              cutoff_utc: datetime,
                              min_count: int = 2) -> list[dict]:
    """Cluster failures into recurring-issue groups.

    Two independent sources are merged into one fingerprint → stats map:

      1. Structured ``tool_error`` audit events (emitted by the executor on
         a genuine tool-handler bug) — grouped by their precomputed
         ``fingerprint`` so the SAME bug across many tasks collapses to one
         row. This is the high-signal path; it carries exc_type + the tool.
      2. ``error.log`` lines grouped by ``_ERROR_SIGNATURES`` — a coarse
         net for failures that never reached the structured event (e.g.
         crashes outside the tool path).

    A cluster is "recurring" once it has ``>= min_count`` occurrences in the
    window. Returns the clusters sorted by count desc (capped), each with
    first/last-seen timestamps and a representative example — exactly the
    recurring/unresolved-issue view the loop previously lacked.
    """
    clusters: dict[str, dict] = {}

    def _bump(key: str, *, source: str, ts: datetime | None,
              example: str, **extra) -> None:
        c = clusters.get(key)
        if c is None:
            c = {'fingerprint': key, 'source': source, 'count': 0,
                 'first_seen': None, 'last_seen': None, 'example': example[:240]}
            c.update(extra)
            clusters[key] = c
        c['count'] += 1
        if ts is not None:
            if c['first_seen'] is None or ts < c['first_seen']:
                c['first_seen'] = ts
            if c['last_seen'] is None or ts > c['last_seen']:
                c['last_seen'] = ts

    # ── Source 1: structured tool_error audit events ──
    for line in _safe_tail_lines(AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry or entry.get('event') != 'tool_error':
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        fp = str(entry.get('fingerprint') or entry.get('detail') or 'unknown')
        _bump(f'tool_error::{fp}', source='tool_error',
              ts=ts, example=str(entry.get('detail') or fp),
              tool=entry.get('tool', '?'), exc_type=entry.get('exc_type', ''))

    # ── Source 2: error.log signature clustering ──
    for line in _safe_tail_lines(ERROR_LOG, max_bytes=2 * 1024 * 1024):
        ts = _parse_app_log_ts(line)
        if ts is not None and ts < cutoff_local:
            continue
        label = _classify_error_signature(line)
        if not label:
            continue
        _bump(f'errorlog::{label}', source='error_log', ts=ts, example=line)

    recurring = [c for c in clusters.values() if c['count'] >= min_count]
    recurring.sort(key=lambda c: c['count'], reverse=True)

    out: list[dict] = []
    for c in recurring[:15]:
        out.append({
            'fingerprint': c['fingerprint'][:200],
            'source': c['source'],
            'count': c['count'],
            'tool': c.get('tool', ''),
            'exc_type': c.get('exc_type', ''),
            'first_seen': c['first_seen'].isoformat() if c['first_seen'] else '',
            'last_seen': c['last_seen'].isoformat() if c['last_seen'] else '',
            'example': c['example'],
        })
    return out


def _collect_scheduler_signals() -> dict:
    """Mine scheduled_tasks rows for failing / idle-proactive tasks."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        rows = db.execute(
            'SELECT id, name, task_type, enabled, run_count, fail_count, '
            'poll_count, execution_count, last_poll_decision, '
            'last_execution_status, schedule '
            'FROM scheduled_tasks').fetchall()
    except Exception as e:
        logger.warning('[Optimizer.analyzer] scheduler scan skipped: %s', e)
        return {'failing_scheduled_tasks': [], 'idle_proactive_tasks': []}

    failing: list[dict] = []
    idle_proactive: list[dict] = []
    for r in rows:
        row = dict(r)
        run_count = int(row.get('run_count') or 0)
        fail_count = int(row.get('fail_count') or 0)
        if run_count >= 5 and fail_count >= max(3, run_count // 2):
            failing.append({
                'id': row['id'],
                'name': row.get('name', ''),
                'task_type': row.get('task_type', ''),
                'run_count': run_count,
                'fail_count': fail_count,
                'fail_ratio': round(fail_count / max(1, run_count), 2),
            })
        if row.get('task_type') == 'agent':
            poll = int(row.get('poll_count') or 0)
            execs = int(row.get('execution_count') or 0)
            if poll >= 20 and execs == 0:
                idle_proactive.append({
                    'id': row['id'],
                    'name': row.get('name', ''),
                    'poll_count': poll,
                    'execution_count': execs,
                    'schedule': row.get('schedule', ''),
                })
    return {
        'failing_scheduled_tasks': failing[:10],
        'idle_proactive_tasks': idle_proactive[:10],
    }


def _collect_cost_outliers() -> dict:
    """Surface top-cost conversations from daily_cost_cache (no full scan)."""
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            "SELECT conversations_json FROM daily_cost_cache "
            "ORDER BY date DESC LIMIT 1").fetchone()
    except Exception as e:
        logger.debug('[Optimizer.analyzer] cost cache scan skipped: %s', e)
        return {'top_cost_conversations': []}
    if not row:
        return {'top_cost_conversations': []}
    raw = row['conversations_json'] if isinstance(row, dict) else row[0]
    try:
        data = json.loads(raw or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] cost cache json invalid: %s', e)
        return {'top_cost_conversations': []}
    if not isinstance(data, dict):
        return {'top_cost_conversations': []}
    def _cost_of(v):
        # daily_cost_cache stores either a flat number (legacy) or
        # {"cost": <num>, ...} per conv (current).  Tolerate both.
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for key in ('cost', 'total', 'usd', 'total_cost'):
                if key in v:
                    try:
                        return float(v[key] or 0)
                    except (TypeError, ValueError) as _e_audit:
                        logger.debug('[analyzer] _cost_of caught %s: %s', type(_e_audit).__name__, _e_audit)
                        return 0.0
        return 0.0

    pairs = sorted(
        ((cid, _cost_of(v)) for cid, v in data.items()),
        key=lambda x: x[1], reverse=True)[:10]
    return {
        'top_cost_conversations': [
            {'conv_id': str(cid)[:16], 'cost_usd': round(cost, 4)}
            for cid, cost in pairs if cost > 0
        ],
    }


def _collect_conversation_tool_distribution(cutoff_local: datetime) -> dict:
    """Scan recent conversation messages for tool usage distribution.

    Best-effort — on any DB error we return empty counters."""
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        updated_ms = int(cutoff_local.timestamp() * 1000)
        rows = db.execute(
            'SELECT messages FROM conversations '
            'WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 200',
            [updated_ms]).fetchall()
    except Exception as e:
        logger.warning('[Optimizer.analyzer] conversation scan skipped: %s', e)
        return {'tool_counts': {}, 'search_urls': [], 'fetch_urls': []}

    tool_counts: Counter = Counter()
    search_urls: Counter = Counter()
    fetch_urls: Counter = Counter()

    for row in rows:
        raw = row['messages'] if isinstance(row, dict) else row[0]
        try:
            if isinstance(raw, (list, dict)):
                messages = raw
            else:
                messages = json.loads(raw or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Optimizer.analyzer] could not parse messages: %s', e)
            continue
        if not isinstance(messages, list):
            continue
        for m in messages:
            if not isinstance(m, dict):
                continue
            rounds = m.get('toolRounds') or m.get('searchRounds') or []
            if not isinstance(rounds, list):
                continue
            for r in rounds:
                if not isinstance(r, dict):
                    continue
                name = r.get('tool') or r.get('name') or ''
                if name:
                    tool_counts[name] += 1
                if name == 'web_search':
                    for res in (r.get('results') or [])[:10]:
                        url = (res or {}).get('url') if isinstance(res, dict) else ''
                        if url:
                            dom = _domain_of(url)
                            if dom:
                                search_urls[dom] += 1
                elif name == 'fetch_url':
                    args = r.get('args') or {}
                    url = args.get('url') if isinstance(args, dict) else ''
                    if url:
                        dom = _domain_of(url)
                        if dom:
                            fetch_urls[dom] += 1
    return {
        'tool_counts': dict(tool_counts),
        'search_urls': [{'domain': d, 'count': n} for d, n in search_urls.most_common(10)],
        'fetch_urls': [{'domain': d, 'count': n} for d, n in fetch_urls.most_common(10)],
    }


_URL_DOMAIN_RE = re.compile(r'https?://([^/\s]+)', re.IGNORECASE)


def _domain_of(url: str) -> str:
    m = _URL_DOMAIN_RE.match(url or '')
    if not m:
        return ''
    host = m.group(1).lower()
    # Strip leading www.
    if host.startswith('www.'):
        host = host[4:]
    # Strip port
    if ':' in host:
        host = host.split(':', 1)[0]
    return host


def _collect_daily_report_snippets(days: int = 7) -> list[dict]:
    """Return small snippets from the last N days of daily reports."""
    out: list[dict] = []
    reports_dir = _config_path('daily_reports')
    if not os.path.isdir(reports_dir):
        return out
    files = sorted(glob.glob(os.path.join(reports_dir, '*.json')), reverse=True)[:days]
    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception as e:
            logger.debug('[Optimizer.analyzer] daily report read failed %s: %s', fp, e)
            continue
        date_str = os.path.basename(fp).rsplit('.', 1)[0]
        summary = ''
        if isinstance(data, dict):
            summary = str(data.get('summary') or data.get('overview') or '')
            if not summary:
                # Try to pull a narrative field if present
                for key in ('narrative', 'report', 'analysis'):
                    v = data.get(key)
                    if isinstance(v, str) and v.strip():
                        summary = v
                        break
        out.append({'date': date_str, 'summary': summary[:500]})
    return out


# ══════════════════════════════════════════════════════════
#  Prior actions + post-apply metrics
# ══════════════════════════════════════════════════════════

def _count_irrelevant_dropped_for_domain(domain: str,
                                          cutoff_local: datetime) -> int:
    """Count ``[Search] ✗ IRRELEVANT dropped <domain>``-ish lines since cutoff."""
    if not domain:
        return 0
    pattern = re.compile(
        r'\[Search\].*?IRRELEVANT.*?' + re.escape(domain),
        re.IGNORECASE)
    count = 0
    for line in _safe_tail_lines(APP_LOG):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        if pattern.search(line):
            count += 1
    return count


def _count_tool_errors(cutoff_local: datetime) -> int:
    total = 0
    fail_re = re.compile(r'\[Tool:[^\]]+\] failed')
    for line in _safe_tail_lines(APP_LOG):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        if fail_re.search(line):
            total += 1
    return total


def _compute_post_apply_metrics(cutoff_local: datetime) -> list[dict]:
    """For each still-active applied action without a recorded outcome,
    compute a simple count-based metric and persist it."""
    summaries: list[dict] = []
    try:
        actions = storage.list_applied_actions(include_reverted=True, limit=100)
    except Exception as e:
        logger.warning('[Optimizer.analyzer] could not list prior actions: %s', e)
        return summaries

    for row in actions:
        action_type = row.get('p_action_type') or ''
        args_raw = row.get('p_action_args') or '{}'
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Optimizer.analyzer] bad action_args for %s: %s',
                         row.get('id'), e)
            args = {}

        outcome_raw = row.get('outcome_metric') or ''
        has_outcome = bool(outcome_raw and outcome_raw not in ('{}', 'null'))
        log_id = row['id']
        metric: dict[str, Any] = {}

        if action_type == 'block_search_domain':
            domain = str(args.get('domain') or '').lower()
            dropped = _count_irrelevant_dropped_for_domain(domain, cutoff_local)
            tool_errs = _count_tool_errors(cutoff_local)
            metric = {
                'domain': domain,
                'irrelevant_dropped_24h': dropped,
                'total_tool_errors_24h': tool_errs,
                'interpretation': (
                    'near-zero drops → block working; high drops → may no longer'
                    ' be needed or need broader match'),
            }
        else:
            metric = {'note': 'no auto-metric for this action_type'}

        if not has_outcome:
            try:
                storage.record_outcome_metric(log_id, metric)
            except Exception as e:
                logger.warning('[Optimizer.analyzer] record_outcome_metric '
                               'failed for %s: %s', log_id, e)

        summaries.append({
            'id': log_id,
            'proposal_id': row.get('proposal_id'),
            'action_type': action_type,
            'args': args,
            'applied_at': row.get('applied_at'),
            'expires_at': row.get('expires_at'),
            'reverted_at': row.get('reverted_at') or '',
            'proposal_status': row.get('p_status'),
            'outcome_metric': metric,
        })
    return summaries


# ══════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════

def gather_evidence(window_hours: int = 24) -> EvidenceBundle:
    """Build an EvidenceBundle covering the past ``window_hours``."""
    now_local = datetime.now()
    cutoff_local = now_local - timedelta(hours=window_hours)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    bundle = EvidenceBundle(
        window_hours=window_hours,
        generated_at=now_local.isoformat(),
    )

    app_signals = _collect_app_log_signals(cutoff_local)
    bundle.tool_call_counts = app_signals['tool_call_counts']
    bundle.tool_error_counts = app_signals['tool_error_counts']
    bundle.irrelevant_dropped_domains = app_signals['irrelevant_dropped_domains']
    bundle.warn_log_excerpts = app_signals['warn_excerpts']
    bundle.fetch_timeout_count = app_signals['fetch_timeout_count']
    bundle.fetch_failure_count = app_signals['fetch_failure_count']
    bundle.rate_limit_429_count = app_signals['rate_limit_429_count']
    bundle.prompt_too_long_count = app_signals['prompt_too_long_count']
    bundle.context_near_full_count = app_signals['context_near_full_count']
    bundle.compaction_trigger_count = app_signals['compaction_trigger_count']

    audit2 = _collect_audit_secondary(cutoff_utc)
    bundle.model_switch_events = audit2['model_switch_events']

    sched = _collect_scheduler_signals()
    bundle.failing_scheduled_tasks = sched['failing_scheduled_tasks']
    bundle.idle_proactive_tasks = sched['idle_proactive_tasks']

    cost = _collect_cost_outliers()
    bundle.top_cost_conversations = cost['top_cost_conversations']

    conv_signals = _collect_conversation_tool_distribution(cutoff_local)
    # Merge conv-side tool counts with log-side counts (log wins for per-tool
    # invocation count, conv-side fills any gaps)
    merged = dict(bundle.tool_call_counts)
    for k, v in conv_signals['tool_counts'].items():
        merged[k] = max(merged.get(k, 0), v)
    bundle.tool_call_counts = merged
    bundle.top_search_domains = conv_signals['search_urls']

    bundle.audit_event_counts, optimizer_audit = _collect_audit_events(cutoff_utc)
    bundle.error_log_excerpts = _collect_error_log_excerpts(cutoff_local)
    bundle.recurring_issues = _collect_recurring_issues(cutoff_local, cutoff_utc)
    bundle.daily_report_snippets = _collect_daily_report_snippets(days=7)
    bundle.prior_actions = _compute_post_apply_metrics(cutoff_local)

    # optimizer_audit is kept as debug-only detail — expose via warn_log_excerpts
    # so it shows up in the prompt without a dedicated field
    for row in optimizer_audit[:10]:
        bundle.warn_log_excerpts.append('[optimizer_audit] ' + row['details_preview'][:240])

    logger.info('[Optimizer.analyzer] evidence: tools=%d errors=%d top_domains=%d '
                'prior_actions=%d audit_events=%d recurring_issues=%d',
                len(bundle.tool_call_counts), len(bundle.tool_error_counts),
                len(bundle.top_search_domains), len(bundle.prior_actions),
                len(bundle.audit_event_counts), len(bundle.recurring_issues))
    return bundle
