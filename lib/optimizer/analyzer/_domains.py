"""lib/optimizer/analyzer/_domains.py — domain helpers + daily reports.

The ``_URL_DOMAIN_RE`` / ``_domain_of`` normaliser, daily-report snippet
reader, and the per-domain IRRELEVANT-drop counter used by post-apply
metrics.  Mutable log paths are read via the facade package for test
monkeypatching.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _safe_tail_lines, _parse_app_log_ts

logger = get_logger(__name__)


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


def _count_irrelevant_dropped_for_domain(domain: str,
                                          cutoff_local: datetime) -> int:
    """Count ``[Search] ✗ IRRELEVANT dropped <domain>``-ish lines since cutoff."""
    if not domain:
        return 0
    pattern = re.compile(
        r'\[Search\].*?IRRELEVANT.*?' + re.escape(domain),
        re.IGNORECASE)
    count = 0
    for line in _safe_tail_lines(_facade.APP_LOG):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        if pattern.search(line):
            count += 1
    return count
