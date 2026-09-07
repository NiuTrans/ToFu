"""Gitignore suggestion registry — probe slow dirs on grep/find timeouts.

On a grep/find timeout, record_timeout_and_probe(base) runs a bounded
depth-1 scan of ``base`` and registers the largest sub-directories (by
direct-entry count) that are NOT already covered by ``.gitignore``.

Nothing is ever silently excluded from future searches.  The registry
is read by:

  * ``_run_rg`` / ``_run_gnu_grep`` — to append a footer to the timeout
    message so the model and user see which dirs are likely culprits.
  * ``routes/project.py`` — via ``GET /api/project/gitignore/suggestions``
    so the UI can surface a one-click "add to .gitignore" action.
    Only an explicit ``POST /api/project/gitignore/accept`` ever writes
    to the user's ``.gitignore`` file.
"""

import os
import threading
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# Tunables
_PROBE_BUDGET_S = 5.0            # hard wall for the depth-1 scan
_MIN_ENTRY_COUNT = 1000          # only suggest dirs this size or bigger
_MAX_SUGGESTIONS_PER_BASE = 5    # registry cap per project
_MAX_BASES = 128                  # distinct project roots retained process-wide
_SUGGESTION_TTL_S = 24 * 3600    # suggestions expire after 24 h
_TOP_N = 2                       # keep at most this many suggestions per timeout

# Directory names we will never auto-suggest — these are almost always source code
# and the user almost certainly doesn't want them gitignored.
_SOURCE_DIR_WHITELIST = frozenset({
    'lib', 'src', 'routes', 'static', 'app', 'server', 'client',
    'pages', 'components', 'test', 'tests', 'spec', 'specs',
    'docs', 'doc', 'scripts', 'tools', 'bin', 'api', 'core',
    'public', 'examples', 'example', 'samples',
})


_lock = threading.RLock()
# base_abs → [{'dir': str, 'entry_count': int, 'detected_at': float, 'reason': str}]
_registry: dict[str, list[dict]] = {}


def _prune_registry_locked(now: float) -> None:
    """Remove expired/empty roots; caller holds ``_lock``."""
    for registered_base, bucket in list(_registry.items()):
        fresh = [
            item for item in bucket
            if now - float(item.get('detected_at') or 0) < _SUGGESTION_TTL_S
        ]
        if fresh:
            _registry[registered_base] = fresh
        else:
            _registry.pop(registered_base, None)


def _admit_base_locked(base: str) -> None:
    """Make room for one cosmetic root without touching user files."""
    if base in _registry or len(_registry) < _MAX_BASES:
        return
    oldest = min(
        _registry,
        key=lambda registered_base: max(
            (float(item.get('detected_at') or 0)
             for item in _registry[registered_base]),
            default=0.0,
        ),
    )
    _registry.pop(oldest, None)


def _gitignore_dir_names(base: str) -> set[str]:
    """Return the set of top-level directory names that ``base/.gitignore``
    already covers.

    Intentionally conservative — matches what our rg fallback parser does:
    plain directory entries like ``foo/`` or bare ``foo``, no globs, no
    anchoring beyond a leading ``/``.  Anything we can't parse cleanly is
    skipped so we never falsely claim "already ignored".
    """
    gi = os.path.join(base, '.gitignore')
    names: set[str] = set()
    if not os.path.isfile(gi):
        return names
    try:
        with open(gi, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('!'):
                    continue
                line = line.lstrip('/')
                if '*' in line or '?' in line:
                    continue
                name = line.rstrip('/')
                if '/' in name or not name:
                    continue
                names.add(name)
    except OSError as e:
        logger.debug('[GitignoreSuggest] failed to read %s: %s', gi, e)
    return names


def _probe_top_dirs(base: str, already_ignored: set[str]) -> list[dict]:
    """Depth-1 scan ranking subdirs of ``base`` by direct-entry count.

    Returns list sorted by entry_count desc.  Never exceeds ``_PROBE_BUDGET_S``.
    """
    results: list[dict] = []
    deadline = time.time() + _PROBE_BUDGET_S
    try:
        with os.scandir(base) as it:
            subs = [e for e in it if not e.name.startswith('.')]
    except OSError as e:
        logger.debug('[GitignoreSuggest] scandir(%s) failed: %s', base, e)
        return results

    for entry in subs:
        if time.time() > deadline:
            logger.debug('[GitignoreSuggest] probe budget exhausted at %s', entry.name)
            break
        name = entry.name
        if name in already_ignored or name in _SOURCE_DIR_WHITELIST:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError as _e_audit:
            logger.debug('[gitignore_suggest] _probe_top_dirs caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue
        count = 0
        try:
            with os.scandir(entry.path) as sub_it:
                for _ in sub_it:
                    count += 1
                    if count > 50000 or time.time() > deadline:
                        break
        except OSError as e:
            logger.debug('[GitignoreSuggest] scandir(%s) failed: %s', entry.path, e)
            continue
        if count >= _MIN_ENTRY_COUNT:
            results.append({'dir': name, 'entry_count': count})

    results.sort(key=lambda r: r['entry_count'], reverse=True)
    return results


def record_timeout_and_probe(base: str, reason: str = 'grep_timeout') -> list[dict]:
    """Probe ``base`` after a timeout and update the suggestion registry.

    Returns the currently-registered suggestions for ``base`` (a list of
    ``{dir, entry_count, detected_at, reason}`` dicts), suitable for
    inclusion in the tool's timeout footer.  Safe to call repeatedly; the
    probe is bounded and the registry dedupes by dir name.
    """
    if not base or not os.path.isdir(base):
        return []
    base = os.path.abspath(base)
    try:
        already = _gitignore_dir_names(base)
        candidates = _probe_top_dirs(base, already)
    except Exception as e:
        logger.warning('[GitignoreSuggest] probe failed for %s: %s', base, e, exc_info=True)
        return []

    now = time.time()
    new_entries = 0
    with _lock:
        _prune_registry_locked(now)
        bucket = list(_registry.get(base, []))
        existing = {item['dir']: item for item in bucket}
        for cand in candidates[:_TOP_N]:
            current = existing.get(cand['dir'])
            if current is not None:
                # A repeated timeout is fresh evidence. Refreshing the row
                # keeps active projects warm while the global cap evicts only
                # the least-recently observed cosmetic suggestion set.
                current.update({
                    'entry_count': cand['entry_count'],
                    'detected_at': now,
                    'reason': reason,
                })
                continue
            item = {
                'dir': cand['dir'],
                'entry_count': cand['entry_count'],
                'detected_at': now,
                'reason': reason,
            }
            bucket.append(item)
            existing[cand['dir']] = item
            new_entries += 1
        if len(bucket) > _MAX_SUGGESTIONS_PER_BASE:
            bucket.sort(key=lambda s: s['entry_count'], reverse=True)
            del bucket[_MAX_SUGGESTIONS_PER_BASE:]
        if bucket:
            _admit_base_locked(base)
            _registry[base] = bucket
        else:
            # The old setdefault path retained one empty key for every timed
            # out root even when the probe found nothing worth suggesting.
            _registry.pop(base, None)
        snapshot = [dict(s) for s in bucket]

    if new_entries:
        logger.info('[GitignoreSuggest] probed %s: %d new suggestion(s), %d total (reason=%s)',
                    base, new_entries, len(snapshot), reason)
        audit_log('gitignore_suggestion_detected', base=base, reason=reason,
                  new_count=new_entries, suggestions=[s['dir'] for s in snapshot])
    return snapshot


def get_suggestions(base: str) -> list[dict]:
    """Return non-expired suggestions for ``base`` without probing."""
    if not base:
        return []
    base = os.path.abspath(base)
    now = time.time()
    with _lock:
        _prune_registry_locked(now)
        return [dict(item) for item in _registry.get(base, [])]


def format_footer(suggestions: list[dict]) -> str:
    """Render a short human/LLM-readable footer describing suggestions.

    Returns empty string if there are no suggestions.
    """
    if not suggestions:
        return ''
    items = ', '.join(
        f'{s["dir"]}/ (~{s["entry_count"]} entries)' for s in suggestions[:_TOP_N]
    )
    return (
        f'\nLikely culprits not in .gitignore: {items}. '
        'Review and add via POST /api/project/gitignore/accept (Settings UI).'
    )


def _dismiss_one(base: str, dirs: list[str]) -> int:
    removed = 0
    with _lock:
        bucket = _registry.get(base, [])
        before = len(bucket)
        retained = [s for s in bucket if s['dir'] not in set(dirs)]
        if retained:
            _registry[base] = retained
        else:
            _registry.pop(base, None)
        removed = before - len(retained)
    return removed


def dismiss_suggestions(base: str, dirs: list[str]) -> int:
    """Remove the named dirs from the suggestion registry (no .gitignore write).

    Returns the number of entries actually removed.
    """
    if not base or not dirs:
        return 0
    base = os.path.abspath(base)
    removed = _dismiss_one(base, dirs)
    if removed:
        audit_log('gitignore_suggestion_dismissed', base=base, dirs=list(dirs), removed=removed)
    return removed


def accept_suggestions(base: str, dirs: list[str]) -> dict:
    """Append the named dirs to ``base/.gitignore`` as a block, with a header.

    Idempotent: dirs already present in .gitignore are skipped.  Writes are
    atomic-ish (read, append, write back).  Never writes an empty block.
    Returns ``{'added': [...], 'skipped_existing': [...], 'unknown': [...]}``.
    """
    if not base:
        return {'error': 'no base'}
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return {'error': f'not a directory: {base}'}

    # Validate: all requested dirs must be in the current suggestion set
    # (defense in depth — prevents API callers from writing arbitrary
    # strings into the user's .gitignore).
    known = {s['dir'] for s in get_suggestions(base)}
    valid: list[str] = []
    unknown: list[str] = []
    for d in dirs or []:
        d = (d or '').strip().rstrip('/')
        if not d or '/' in d or d.startswith('.'):
            unknown.append(d)
            continue
        if d in known:
            valid.append(d)
        else:
            unknown.append(d)

    if not valid:
        return {'added': [], 'skipped_existing': [], 'unknown': unknown}

    already = _gitignore_dir_names(base)
    to_add = [d for d in valid if d not in already]
    skipped_existing = [d for d in valid if d in already]

    if to_add:
        gi_path = os.path.join(base, '.gitignore')
        try:
            existing = ''
            if os.path.isfile(gi_path):
                with open(gi_path, errors='replace') as f:
                    existing = f.read()
            trailing = '' if existing.endswith('\n') or not existing else '\n'
            header = f'\n# ── Auto-added by grep_search timeout probe on {time.strftime("%Y-%m-%d")} ──\n'
            body = '\n'.join(f'{d}/' for d in to_add) + '\n'
            with open(gi_path, 'a') as f:
                # If file missing newline at EOF, fix that before the header.
                if trailing:
                    f.write(trailing)
                f.write(header)
                f.write(body)
        except OSError as e:
            logger.error('[GitignoreSuggest] failed to write %s: %s', gi_path, e, exc_info=True)
            return {'error': f'write failed: {e}'}

        audit_log('gitignore_auto_added', base=base, added=to_add,
                  skipped_existing=skipped_existing)
        logger.info('[GitignoreSuggest] appended %d entries to %s/.gitignore: %s',
                    len(to_add), base, to_add)

    # Remove accepted dirs from the registry (whether newly-added or already-present)
    _dismiss_one(base, valid)

    return {
        'added': to_add,
        'skipped_existing': skipped_existing,
        'unknown': unknown,
    }
