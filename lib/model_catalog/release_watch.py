"""Model release watch: upstream listings the Model registry withholds.

Entry points: ``release_watch_report`` and ``record_review``. The CLI and
scheduler predicate is ``scripts/model_release_watch.py``; the intake
procedure an agent follows for each candidate is docs/MODEL_REGISTRATION.md
"New-model intake workflow".

A candidate is a Model identity that only live public catalogues supply (the
dynamic-feed overlays carrying ``catalog_feed``) and that world-catalogue sync
withholds for missing admission facts — the sync's own projection, via
``world_catalog.withheld_catalog_models``. Repository-authored template rows
are owner-authored supply, not releases. A registration resolves itself: once
maintained knowledge makes the identity admissible it is no longer withheld,
so no separate "registered" flag can drift from the registry rule.

State (``data/config/model_release_watch.json``) is deployment maintainer
state, not owner data. The first report records the current candidates as a
baseline and reports none; a public catalogue first seen later contributes its
standing listings to the baseline the same way, so neither installation nor a
newly allowlisted feed floods the intake with old releases. Reviews keep a
``deferred`` or ``declined`` decision with reason, date and checked sources;
deferred candidates are listed in every report, but only unreviewed ones are
new. Reports never fetch (the daily model-intelligence update owns feed
refresh) and start no worker, cache or model call.
"""
from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlsplit

from lib.config_dir import config_path
from lib.json_store import JsonStoreReadError, read_json, update_json_atomic

STATE_PATH = config_path('model_release_watch.json')
STATE_VERSION = 1
DECISIONS = ('deferred', 'declined')
MAX_REASON_CHARS = 500
# The evidence-URL bound of registration_metadata.source_urls.
MAX_SOURCE_URLS = 8


class ReleaseWatchError(ValueError):
    """The watch state or a requested review cannot be used."""


def _lead(supply: dict) -> dict:
    lead = {'template': supply['template'], 'wire_model_ids': supply['wire_model_ids']}
    for field in ('context_window', 'capabilities', 'actual_pricing'):
        if supply.get(field):
            lead[field] = supply[field]
    return lead


def _sources(candidate: dict) -> set[str]:
    return {lead['template'] for lead in candidate['leads']}


def release_candidates(templates: list[dict]) -> tuple[dict, list[dict]]:
    """Live public catalogues and the withheld identities only they supply."""
    from lib.model_routing.world_catalog import withheld_catalog_models

    feeds = {row['key']: row['catalog_feed'] for row in templates if row.get('catalog_feed')}
    candidates = []
    for row in withheld_catalog_models(templates):
        sources = {supply['template'] for supply in row['supply']}
        if sources and sources <= feeds.keys():
            candidates.append({
                'id': f"{row['creator_id']}/{row['model_id']}",
                'creator_id': row['creator_id'], 'model_id': row['model_id'],
                'missing': row['missing'],
                'leads': [_lead(supply) for supply in row['supply']],
            })
    return feeds, sorted(candidates, key=lambda candidate: candidate['id'])


def _validated(state) -> dict:
    if (not isinstance(state, dict) or state.get('version') != STATE_VERSION
            or not isinstance(state.get('sources'), list)
            or not isinstance(state.get('baseline'), list)
            or not isinstance(state.get('reviews'), dict)):
        raise ReleaseWatchError('release watch state is unreadable; inspect it instead of re-baselining')
    return state


def _update_state(state_path: str, mutator):
    try:
        return update_json_atomic(state_path, mutator, default=None, strict=True)
    except JsonStoreReadError as exc:
        raise ReleaseWatchError(f'release watch state is unreadable: {exc}') from exc


def _load_templates(templates):
    if templates is not None:
        return templates
    from lib.provider.provider_template_recipes import load_provider_templates
    return load_provider_templates()


def release_watch_report(*, templates: list[dict] | None = None,
                         state_path: str = STATE_PATH, now: datetime | None = None) -> dict:
    """Classify current candidates as new or deferred; initialize on first use.

    ``status`` is ``unavailable`` without a cached live catalogue (never
    evidence that nothing was released), ``initialized`` on the run that
    records the baseline, else ``ok``.
    """
    feeds, candidates = release_candidates(_load_templates(templates))
    if not feeds:
        return {'status': 'unavailable', 'feeds': {}, 'new': [], 'deferred': [], 'baseline': 0}
    stamp = (now or datetime.now()).isoformat(timespec='seconds')
    outcome: dict = {'status': 'ok'}

    def settle(state):
        if state is None:
            outcome['status'] = 'initialized'
            return {'version': STATE_VERSION, 'initialized_at': stamp, 'sources': sorted(feeds),
                    'baseline': [candidate['id'] for candidate in candidates], 'reviews': {}}
        state = _validated(state)
        fresh = feeds.keys() - set(state['sources'])
        if not fresh:
            outcome['state'] = state
            return None
        known = set(state['baseline']) | state['reviews'].keys()
        state['baseline'] += [candidate['id'] for candidate in candidates
                              if candidate['id'] not in known and _sources(candidate) <= fresh]
        state['sources'] = sorted(set(state['sources']) | fresh)
        return state

    state = _update_state(state_path, settle) or outcome['state']
    baseline, reviews = set(state['baseline']), state['reviews']
    return {
        'status': outcome['status'],
        'feeds': {key: {'url': feed.get('url', ''), 'fetched_at': feed.get('fetched_at'),
                        'stale': bool(feed.get('stale'))} for key, feed in sorted(feeds.items())},
        'new': [candidate for candidate in candidates
                if candidate['id'] not in baseline and candidate['id'] not in reviews],
        'deferred': [{**candidate, 'review': reviews[candidate['id']]} for candidate in candidates
                     if reviews.get(candidate['id'], {}).get('decision') == 'deferred'],
        'baseline': len(baseline),
    }


def _https(url) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password


def record_review(identity: str, decision: str, reason: str, source_urls=(), *,
                  templates: list[dict] | None = None, state_path: str = STATE_PATH,
                  today: date | None = None) -> dict:
    """Record a decision for a current candidate; never pre-suppress a future listing."""
    if decision not in DECISIONS:
        raise ReleaseWatchError(f'decision must be one of {", ".join(DECISIONS)}')
    reason = (reason or '').strip()
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise ReleaseWatchError(f'a review needs a reason of at most {MAX_REASON_CHARS} characters')
    urls = list(dict.fromkeys(source_urls))
    if len(urls) > MAX_SOURCE_URLS or not all(_https(url) for url in urls):
        raise ReleaseWatchError(f'sources must be at most {MAX_SOURCE_URLS} HTTPS URLs without credentials')
    _feeds, candidates = release_candidates(_load_templates(templates))
    if identity not in {candidate['id'] for candidate in candidates}:
        raise ReleaseWatchError(f'{identity} is not a current release candidate')
    review = {'decision': decision, 'reason': reason,
              'checked_at': (today or date.today()).isoformat(), 'source_urls': urls}

    def apply(state):
        if state is None:
            raise ReleaseWatchError('release watch is not initialized; run the report first')
        state = _validated(state)
        state['reviews'][identity] = review
        return state

    _update_state(state_path, apply)
    return review


def baseline_identities(*, state_path: str = STATE_PATH) -> list[str]:
    """Identities recorded as standing listings when their catalogue was first seen."""
    try:
        state = read_json(state_path, default=None, strict=True)
    except JsonStoreReadError as exc:
        raise ReleaseWatchError(f'release watch state is unreadable: {exc}') from exc
    return [] if state is None else sorted(_validated(state)['baseline'])


__all__ = ['DECISIONS', 'MAX_REASON_CHARS', 'MAX_SOURCE_URLS', 'ReleaseWatchError',
           'STATE_PATH', 'baseline_identities',
           'record_review', 'release_candidates', 'release_watch_report']
