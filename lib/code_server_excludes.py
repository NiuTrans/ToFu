#!/usr/bin/env python3
"""Sync canonical fileWatcher/search excludes into code-server User settings.

Problem
-------
code-server (web VS Code) spawns a ``fileWatcher`` worker per workspace
root. Our project ships a canonical ``.vscode/settings.json`` with
``files.watcherExclude`` covering heavy/generated dirs (``swebench_workdir/``,
``data/pgdata/``, ``logs/``, ``node_modules/``, etc.) — but those settings
are **workspace-scoped**, loaded only when tofu itself is the workspace
root.

When the user opens the *parent* directory (common on shared dev boxes,
so they can see tofu alongside sibling projects), the workspace root
sits above tofu, our ``.vscode/settings.json`` is never loaded, and the
watcher happily recurses into ``swebench_workdir/eval/`` (3,637 full repo
checkouts). Each watcher worker balloons to ~6.7 GB tracking paths +
inotify state. On 2026-05-06 we saw 28 workers × 6.7 GB = **189 GB RSS
consumed by fileWatchers alone**, leaving the shared host with 21 GB
MemAvailable.

Fix
---
Also write the same excludes into the **User-scope** settings file
(``~/.local/share/code-server/User/settings.json``), which applies to
every workspace regardless of root. The sync is:

  * **Idempotent** — fast-path no-op when the canonical keys already match.
  * **Read-merge-write** — never clobbers unrelated keys (``mcopilot.*``,
    ``catpaw.*``, user theme, etc.).
  * **User-override-safe** — if the user explicitly set a glob to ``false``,
    we respect it (never flip it back on).
  * **JSONC-tolerant** — both input and output may contain ``//`` / ``/* */``
    comments and trailing commas; we parse loosely and re-emit plain JSON.
  * **Non-blocking** — runs once in a daemon thread at server startup;
    best-effort. Exceptions are logged but never propagate.
  * **Caveat** — code-server re-reads settings only on window reload, so
    the user must refresh the browser tab once for new excludes to take
    effect. We log this hint.

Source of truth
---------------
The canonical exclude set is loaded from the project's own
``.vscode/settings.json`` — so there is exactly **one** place to edit
excludes, and this sync always mirrors the latest rules.

Usage
-----
Called from ``server.py`` at startup::

    from lib.code_server_excludes import start_code_server_excludes_sync
    start_code_server_excludes_sync()
"""

import json
import os
import re
import sys
import threading

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Paths
# ═══════════════════════════════════════════════════════════════════════

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_VSCODE_SETTINGS = os.path.join(_BASE_DIR, '.vscode', 'settings.json')

# Candidate locations for code-server / VS Code User settings, in preference
# order. Only existing ones are updated.
#   * code-server (self-hosted web VS Code) — most common on shared dev boxes
#   * VS Code Remote-SSH server (when running the classic desktop)
#   * Classic desktop VS Code / VS Codium
_USER_SETTINGS_CANDIDATES = [
    os.path.expanduser('~/.local/share/code-server/User/settings.json'),
    os.path.expanduser('~/.vscode-server/data/User/settings.json'),
    os.path.expanduser('~/.config/Code/User/settings.json'),
    os.path.expanduser('~/.config/Code - Insiders/User/settings.json'),
    os.path.expanduser('~/.config/VSCodium/User/settings.json'),
]

# Keys we mirror from the project settings to the User settings. Limited
# to watcher / search / indexer excludes — the three categories that can
# directly OOM the host when walking into ``swebench_workdir/``.
_SYNCED_KEYS = (
    'files.watcherExclude',
    'search.exclude',
    'python.analysis.exclude',
)

# Max times per process to attempt the sync (across retries). Prevents
# a pathological loop if the settings file is somehow unwriteable.
_MAX_ATTEMPTS = 1


# ═══════════════════════════════════════════════════════════════════════
#  JSONC (VS Code settings) tolerant parse / emit
# ═══════════════════════════════════════════════════════════════════════

_TRAILING_COMMA_RE = re.compile(r',(\s*[}\]])')


def _strip_jsonc(text):
    """Remove JSONC line/block comments + trailing commas, string-aware.

    A naive regex strip is unsafe here because VS Code glob patterns like
    ``"**/data/**"`` contain ``*/`` and ``//`` substrings that look like
    comment delimiters. We do a small character-by-character scan that
    tracks whether we are currently inside a JSON string, and only strips
    comments when we are NOT inside one.

    Handles:
      * Line comments (``// ...`` to end-of-line) outside strings
      * Block comments (``/* ... */``, including multi-line) outside strings
      * Trailing commas before ``}`` or ``]`` (post-pass regex; safe because
        any ``,`` that survives the strip and is followed by ``}``/``]`` is
        outside a string by virtue of valid JSON tokenization).
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = ''
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                # Preserve escape sequence as-is
                out.append(text[i + 1])
                i += 2
                continue
            if ch == string_quote:
                in_string = False
            i += 1
            continue
        # Not in string
        if ch == '"' or ch == "'":
            in_string = True
            string_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == '/':
                # Line comment — skip to newline (preserve the newline so
                # line numbers are stable for error messages).
                j = text.find('\n', i + 2)
                if j == -1:
                    i = n
                else:
                    i = j
                continue
            if nxt == '*':
                # Block comment — skip to closing */
                j = text.find('*/', i + 2)
                if j == -1:
                    i = n
                else:
                    i = j + 2
                continue
        out.append(ch)
        i += 1
    stripped = ''.join(out)
    stripped = _TRAILING_COMMA_RE.sub(r'\1', stripped)
    return stripped


def _load_jsonc(path):
    """Load a JSONC file; return ``(data, raw_text)``.

    Raises ``FileNotFoundError`` if the file is missing, and
    ``ValueError`` if parsing fails even after stripping comments.
    """
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        logger.debug('[CSExcludes] Direct json.loads failed for %s: %s — retrying as JSONC',
                     path, e)
    stripped = _strip_jsonc(raw)
    try:
        return json.loads(stripped), raw
    except json.JSONDecodeError as e:
        raise ValueError(
            'Could not parse %s as JSON or JSONC: %s' % (path, e)
        ) from e


# ═══════════════════════════════════════════════════════════════════════
#  Core merge logic
# ═══════════════════════════════════════════════════════════════════════

def _merge_exclude_dict(existing, canonical):
    """Merge *canonical* glob→bool map into *existing*, respecting overrides.

    Rules:
      * If a glob is missing from *existing*, add it with the canonical value.
      * If *existing* already has the glob (regardless of True/False), keep
        the user's value. This respects explicit user opt-outs.

    Returns ``(merged_dict, added_globs)``.
    """
    if not isinstance(existing, dict):
        # User has some non-dict value here (or key is absent). Start fresh
        # from canonical — but preserve whatever was there under a .orig key
        # would be overkill; just overwrite with canonical.
        return dict(canonical), list(canonical.keys())

    merged = dict(existing)
    added = []
    for glob, val in canonical.items():
        if glob not in merged:
            merged[glob] = val
            added.append(glob)
    return merged, added


def _merge_exclude_list(existing, canonical):
    """Merge *canonical* list (e.g. python.analysis.exclude) into *existing*.

    Returns ``(merged_list, added_items)``.
    """
    if not isinstance(existing, list):
        return list(canonical), list(canonical)
    seen = set(existing)
    merged = list(existing)
    added = []
    for item in canonical:
        if item not in seen:
            merged.append(item)
            added.append(item)
            seen.add(item)
    return merged, added


def _compute_merge(user_settings, canonical_source):
    """Produce updated user settings and a per-key diff.

    Returns ``(updated_settings, diff)`` where *diff* maps key → list of
    added entries. An empty diff means nothing changed.
    """
    updated = dict(user_settings)
    diff = {}

    for key in _SYNCED_KEYS:
        if key not in canonical_source:
            continue
        canonical_val = canonical_source[key]
        existing_val = updated.get(key)

        if isinstance(canonical_val, dict):
            new_val, added = _merge_exclude_dict(existing_val, canonical_val)
            if added:
                updated[key] = new_val
                diff[key] = added
        elif isinstance(canonical_val, list):
            new_val, added = _merge_exclude_list(existing_val, canonical_val)
            if added:
                updated[key] = new_val
                diff[key] = added
        else:
            logger.debug('[CSExcludes] Unexpected type for %s in project settings: %r',
                         key, type(canonical_val))

    return updated, diff


# ═══════════════════════════════════════════════════════════════════════
#  Atomic write
# ═══════════════════════════════════════════════════════════════════════

def _atomic_write(path, text):
    """Write *text* to *path* atomically. Delegates to lib.json_store.

    Disables fsync to match the legacy behaviour (settings sync is best-
    effort and we don't pay the fsync cost for VS Code config files).
    """
    from lib.json_store import write_text_atomic
    write_text_atomic(path, text, fsync=False)


# ═══════════════════════════════════════════════════════════════════════
#  Sync entry point (single-shot, safe to call repeatedly)
# ═══════════════════════════════════════════════════════════════════════

def sync_once():
    """Attempt to sync excludes into every existing User settings file.

    Returns a summary dict for logging/testing::

        {
          'attempted': [path, ...],
          'updated':   [path, ...],
          'unchanged': [path, ...],
          'errors':    [{'path': ..., 'error': ...}, ...],
          'added_by_path': {path: {key: [globs...]}},
        }

    Never raises — all errors are caught and returned in the summary.
    """
    summary = {
        'attempted': [],
        'updated': [],
        'unchanged': [],
        'errors': [],
        'added_by_path': {},
    }

    # 1. Load canonical source of truth (project .vscode/settings.json)
    try:
        canonical, _ = _load_jsonc(_PROJECT_VSCODE_SETTINGS)
    except FileNotFoundError:
        logger.debug('[CSExcludes] Project .vscode/settings.json missing (%s) — '
                     'nothing to sync', _PROJECT_VSCODE_SETTINGS)
        return summary
    except ValueError as e:
        logger.warning('[CSExcludes] Could not parse canonical settings: %s', e)
        return summary

    # Sanity: canonical must have at least one of our synced keys, else
    # there's nothing to sync and we avoid touching User settings.
    if not any(k in canonical for k in _SYNCED_KEYS):
        logger.debug('[CSExcludes] Canonical settings has none of %s — skipping',
                     _SYNCED_KEYS)
        return summary

    # 2. For each candidate User settings file that exists, merge + write.
    for path in _USER_SETTINGS_CANDIDATES:
        if not os.path.exists(path):
            continue
        summary['attempted'].append(path)

        try:
            user_settings, _raw = _load_jsonc(path)
            if not isinstance(user_settings, dict):
                logger.warning('[CSExcludes] %s top-level is not an object — skipping',
                               path)
                continue

            updated, diff = _compute_merge(user_settings, canonical)
            if not diff:
                summary['unchanged'].append(path)
                logger.debug('[CSExcludes] %s already up-to-date', path)
                continue

            # Emit as plain JSON (2-space indent to match VS Code defaults).
            # Any comments in the original are lost — acceptable tradeoff,
            # and VS Code will re-accept this as valid settings.
            new_text = json.dumps(updated, indent=2, ensure_ascii=False) + '\n'
            _atomic_write(path, new_text)

            summary['updated'].append(path)
            summary['added_by_path'][path] = diff

            # Compact diff summary for logs
            added_summary = ', '.join(
                '%s+%d' % (k, len(v)) for k, v in diff.items()
            )
            logger.info(
                '[CSExcludes] Updated %s (%s). Window reload (or browser '
                'refresh) required for code-server to re-read settings.',
                path, added_summary,
            )
            audit_log(
                'code_server_excludes_synced',
                path=path,
                added=diff,
            )
        except Exception as e:
            logger.warning('[CSExcludes] Failed to sync %s: %s', path, e,
                           exc_info=True)
            summary['errors'].append({'path': path, 'error': str(e)})

    if not summary['attempted']:
        logger.debug('[CSExcludes] No User settings files found in any of %d candidates — '
                     'nothing to sync', len(_USER_SETTINGS_CANDIDATES))

    return summary


# ═══════════════════════════════════════════════════════════════════════
#  Public threaded entry point
# ═══════════════════════════════════════════════════════════════════════

_started = False
_start_lock = threading.Lock()


def start_code_server_excludes_sync():
    """Kick off the sync in a daemon thread (non-blocking, best-effort).

    Safe to call multiple times — only the first call spawns a thread.
    On non-Linux platforms this is still useful (VS Code desktop has a
    User settings file too), so we do NOT gate on platform.
    """
    global _started
    with _start_lock:
        if _started:
            logger.debug('[CSExcludes] Already started — skipping')
            return
        _started = True

    def _run():
        try:
            sync_once()
        except Exception as e:
            # Defense-in-depth — sync_once() already catches, but guard
            # the thread entrypoint as well.
            logger.error('[CSExcludes] Unexpected error in sync thread: %s',
                         e, exc_info=True)

    t = threading.Thread(
        target=_run,
        daemon=True,
        name='code-server-excludes-sync',
    )
    t.start()


if __name__ == '__main__':
    # CLI: python -m lib.code_server_excludes  → run once, print summary
    import pprint
    result = sync_once()
    pprint.pprint(result)
    sys.exit(0 if not result['errors'] else 1)
