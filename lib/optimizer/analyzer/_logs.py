"""lib/optimizer/analyzer/_logs.py — app.log / error.log readers.

Log-tail helpers plus the ``app.log`` signal miner and the ``error.log``
excerpt collector.  These functions deliberately look up the mutable log
path constants (``APP_LOG`` / ``ERROR_LOG``) through the facade package
object so that ``monkeypatch.setattr(analyzer, "APP_LOG", ...)`` in tests
(and any hot-reload of the constants) is observed here byte-identically to
the pre-split single-module behaviour.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime

from lib.log import get_logger

from lib.optimizer import analyzer as _facade

logger = get_logger(__name__)


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


def _collect_error_log_excerpts(cutoff_local: datetime, max_lines: int = 40) -> list[str]:
    excerpts: list[str] = []
    for line in reversed(_safe_tail_lines(_facade.ERROR_LOG, max_bytes=2 * 1024 * 1024)):
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

    for line in _safe_tail_lines(_facade.APP_LOG):
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
