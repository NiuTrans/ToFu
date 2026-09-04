"""Hugging Face Daily Papers vertical — trending/curated AI papers."""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _HEADERS, _TIMEOUT, logger

TYPE = 'hf_papers'
DOMAIN = 'academic'

_TRIGGER_RE = re.compile(
    r'\b(?:hugging\s*face|hf)\s+(?:daily\s+)?papers?\b'
    r'|\b(?:daily|trending|hot|latest|recent)\s+(?:ai\s+|ml\s+|research\s+)?papers?\b',
    re.IGNORECASE,
)
_STOPWORDS_RE = re.compile(
    r'\b(?:hugging\s*face|hf|daily|trending|hot|latest|recent|weekly|monthly|'
    r'this|past|week|month|day|days|papers?|on|about|for|the|in|ai|ml|research|'
    r'\d+)\b',
    re.IGNORECASE,
)


def detect(q):
    """Detect a Hugging Face Daily Papers intent. Returns tuple or None.

    Triggers on phrases like 'hf daily papers', 'huggingface papers',
    'trending papers', 'daily papers [topic]', optionally carrying a
    day/week/month window. The topic (if any) becomes the identifier;
    the window goes into params['period'].
    """
    if not _TRIGGER_RE.search(q):
        return None

    period = 'day'
    if re.search(r'\b(this\s+)?week|weekly|past\s+7\s+days?\b', q, re.IGNORECASE):
        period = 'week'
    elif re.search(r'\b(this\s+)?month|monthly|past\s+30\s+days?\b', q, re.IGNORECASE):
        period = 'month'

    # Topic = the query minus the trigger / period words.
    topic = _STOPWORDS_RE.sub(' ', q).strip()
    topic = re.sub(r'\s+', ' ', topic)

    return (TYPE, topic, {'period': period})


def _format_paper(p):
    """Format one HF paper record (the inner ``paper`` dict) into a bullet."""
    pid = p.get('id', '')
    title = (p.get('title') or '').strip().replace('\n', ' ')
    upvotes = p.get('upvotes', 0)
    summary = (p.get('ai_summary') or p.get('summary') or '').strip().replace('\n', ' ')
    line = f'- **{title}** (arXiv:{pid}, ▲{upvotes})'
    if summary:
        line += f'\n  {summary[:300]}'
    line += f'\n  https://huggingface.co/papers/{pid}'
    return line


def search(identifier, params):
    """HF Daily Papers: trending/curated papers by topic or period.

    With a topic (``identifier``), uses the keyword search endpoint. Without
    one, pulls the curated daily list for the period (day/week/month) and
    ranks by upvotes. All endpoints are public and unauthenticated.
    """
    period = (params or {}).get('period', 'day')
    topic = (identifier or '').strip()

    try:
        records = []
        if topic:
            resp = base.http_get('https://huggingface.co/api/papers/search',
                                 params={'q': topic}, headers=_HEADERS, timeout=_TIMEOUT)
            if not resp.ok:
                logger.warning('[Vertical] HF search HTTP %d for %r', resp.status_code, topic)
                return None
            records = resp.json() or []
            heading = f'## Hugging Face Papers — "{topic}"'
        else:
            days = {'day': 1, 'week': 7, 'month': 30}.get(period, 1)
            today = datetime.now(timezone.utc).date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

            def _one_day(date_str):
                r = base.http_get('https://huggingface.co/api/daily_papers',
                                  params={'date': date_str, 'limit': 50},
                                  headers=_HEADERS, timeout=_TIMEOUT)
                return r.json() if r.ok else []

            with ThreadPoolExecutor(max_workers=min(8, max(1, days))) as pool:
                futs = [pool.submit(_one_day, d) for d in dates]
                for fut in as_completed(futs):
                    try:
                        records.extend(fut.result() or [])
                    except Exception as e:
                        logger.debug('[Vertical] HF daily_papers day fetch failed: %s', e)
            heading = f'## Hugging Face Daily Papers — past {period} ({len(records)} papers)'

        if not records:
            return None

        # Each record wraps the paper under 'paper'; flatten + dedup by id.
        papers, seen = [], set()
        for rec in records:
            p = rec.get('paper') if isinstance(rec, dict) else None
            if not p:
                continue
            pid = p.get('id')
            if pid and pid not in seen:
                seen.add(pid)
                papers.append(p)

        papers.sort(key=lambda p: p.get('upvotes', 0), reverse=True)
        top = papers[:15]
        if not top:
            return None

        parts = [heading, ''] + [_format_paper(p) for p in top]
        items = []
        for p in top:
            pid = p.get('id', '')
            items.append({
                'title': (p.get('title') or '').strip().replace('\n', ' '),
                'snippet': (p.get('ai_summary') or p.get('summary') or '').strip()[:240],
                'url': f'https://huggingface.co/papers/{pid}' if pid else '',
                'arxiv_id': pid,
                'upvotes': p.get('upvotes', 0),
                'type': TYPE,
                'source': 'Hugging Face Papers',
            })
        return {'domain': DOMAIN, 'type': TYPE, 'identifier': topic or period,
                'content': '\n'.join(parts), 'source': 'Hugging Face Papers',
                'items': items}
    except Exception as e:
        logger.warning('[Vertical] HF Papers lookup failed for %r: %s', identifier, e)
        return None
