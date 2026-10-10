"""lib/tool_changes.py — Extract file-change list from a tool-rounds blob.

This is the single, authoritative derivation of the file-changes summary
shown in the UI's file-changes bar. The frontend no longer parses tool
rounds itself — it POSTs to ``/api/v1/messages/extract-file-changes`` and
renders whatever this module returns (see ``static/js/ui/finish_info.js``
→ ``Api.conversations.extractFileChanges`` / ``extractFileChangesBatch``).
It exists so:

* The headless API can derive the same file-change summary the UI shows.
* The UI (both mid-stream and on reloaded conversations, before the
  orchestrator's git-history-based ``modifiedFileList`` is available)
  delegates here instead of carrying a parallel copy of the parsing
  rules. One source of truth.

The orchestrator's own derivation
(``lib/tasks_pkg/orchestrator.py:355-405``) uses ``get_modifications``
from the project file-history journal and produces the SAME output
shape — that path is preferred whenever it's available because it
reflects the actual disk state. ``extract_file_changes`` is a fallback
for clients that only have ``toolRounds`` (no git access). The output
shape is identical so callers can swap freely.

Output shape::

    [
      {"path": "src/foo.py", "action": "created", "ok": True,
       "count": 1, "pending": False, "root": "myproj"},
      ...
    ]

Pure function — no I/O, no logger spam, suitable for batch calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from lib.log import get_logger

logger = get_logger(__name__)

# Tools whose rounds carry file-change information.
_WRITE_TOOLS = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})
_DIFF_TOOLS = frozenset({'apply_diff', 'apply_diffs'})
_INSERT_TOOLS = frozenset({'insert_content', 'insert_contents'})
_UNIFIED_EDIT_TOOLS = frozenset({'edit_file'})


@dataclass
class FileChange:
    path: str
    action: str
    ok: bool = True
    count: int = 1
    pending: bool = False
    root: str = ''

    def to_dict(self) -> dict:
        out = {
            'path': self.path,
            'action': self.action,
            'ok': self.ok,
            'count': self.count,
            'pending': self.pending,
            'root': self.root,
        }
        return out


def _split_root(raw_path: Any) -> tuple[str, str]:
    """Parse a ``rootname:rel/path`` prefix.

    Mirrors the JS ``_splitRoot`` exactly:
      * Empty / non-string → ``('', '')``.
      * Absolute / home-relative paths (``/...``, ``~...``) → no root.
      * No colon, or colon at position 0 / >= 40 → no root.
      * Slash before colon (``foo/bar:baz``) → no root.
      * Otherwise → ``(prefix_before_colon, rest_or_'.')``.
    """
    if not raw_path or not isinstance(raw_path, str):
        return '', raw_path or ''
    if raw_path.startswith('/') or raw_path.startswith('~'):
        return '', raw_path
    ci = raw_path.find(':')
    if ci <= 0 or ci >= 40:
        return '', raw_path
    si = raw_path.find('/')
    if si != -1 and si < ci:
        return '', raw_path
    rest = raw_path[ci + 1:] or '.'
    return raw_path[:ci], rest


def _coerce_args(raw: Any) -> Optional[dict]:
    """Best-effort decode of round.toolArgs to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug('[tool_changes] toolArgs JSON decode failed (len=%d): %s', len(raw), e)
            return None
        return d if isinstance(d, dict) else None
    return None


def extract_file_changes(tool_rounds: Iterable[dict]) -> List[FileChange]:
    """Walk a list of tool rounds and return the deduplicated file-change list.

    Output ordering reflects insertion order, matching the JS
    ``Array.from(map.values())`` semantics.

    Multi-root keying: entries are keyed by ``(root, path)`` so a file
    with the same relative path in two different workspace roots stays
    distinct.
    """
    if not tool_rounds:
        return []

    # Use a dict (insertion-ordered in 3.7+) keyed by "root|path".
    changes: "dict[str, FileChange]" = {}

    def _set(raw_path: str, *, action: str, ok: bool = True,
              count: int = 1, pending: bool = False) -> None:
        root, path = _split_root(raw_path)
        key = f'{root}|{path}'
        prev = changes.get(key)
        if prev is None:
            changes[key] = FileChange(
                path=path, action=action, ok=ok and (action != ''),
                count=count, pending=pending, root=root)
            return
        # Merge rules — same as JS:
        #   ok = prev.ok AND new.ok != False
        #   count += new.count
        #   action = new.action (mostly), but 'created' beats 'written'
        new_ok = prev.ok and (ok is not False)
        new_count = prev.count + count
        new_action = action
        if prev.action == 'created' and action == 'written':
            new_action = 'created'
        prev.ok = new_ok
        prev.count = new_count
        prev.action = new_action
        prev.pending = pending

    for round_ in tool_rounds:
        if not isinstance(round_, dict):
            continue
        tn = round_.get('toolName') or ''
        results = round_.get('results') or []

        # ── run_command: read meta.fileChanges if the tool surfaced one ──
        if tn == 'run_command' and results:
            meta = results[0] if isinstance(results[0], dict) else {}
            file_changes = meta.get('fileChanges')
            if isinstance(file_changes, list):
                for fc in file_changes:
                    if not isinstance(fc, dict):
                        continue
                    p = fc.get('path')
                    if not p:
                        continue
                    _set(p, action=fc.get('action') or 'modified', ok=True)
            continue

        if tn not in _WRITE_TOOLS:
            continue

        # ── In-progress / streaming write — show as pending ──
        if not results:
            if round_.get('status') == 'searching':
                args = _coerce_args(round_.get('toolArgs'))
                if args:
                    paths: List[str] = []
                    edits = args.get('edits')
                    if isinstance(edits, list):
                        for e in edits:
                            if isinstance(e, dict) and e.get('path'):
                                paths.append(e['path'])
                    elif args.get('path'):
                        paths.append(args['path'])
                    for p in paths:
                        root, path = _split_root(p)
                        key = f'{root}|{path}'
                        if key not in changes:
                            if tn in _UNIFIED_EDIT_TOOLS:
                                pending_action = 'editing…'
                            elif tn in _DIFF_TOOLS:
                                pending_action = 'patching…'
                            elif tn in _INSERT_TOOLS:
                                pending_action = 'inserting…'
                            else:
                                pending_action = 'writing…'
                            _set(p, action=pending_action, ok=True,
                                  count=0, pending=True)
            continue

        meta = results[0] if isinstance(results[0], dict) else {}
        ok = meta.get('writeOk') is not False

        # ── apply_diff / insert_content: walk edits[] ──
        if tn in _DIFF_TOOLS or tn in _INSERT_TOOLS or tn in _UNIFIED_EDIT_TOOLS:
            args = _coerce_args(round_.get('toolArgs'))
            if args:
                edits = args.get('edits')
                if isinstance(edits, list):
                    handled = False
                    for e in edits:
                        if isinstance(e, dict) and e.get('path'):
                            if tn in _UNIFIED_EDIT_TOOLS:
                                action = ('inserted' if str(e.get('operation', '')).startswith('insert_')
                                          else 'patched')
                            else:
                                action = ('inserted' if tn in _INSERT_TOOLS
                                          else 'patched')
                            _set(e['path'], action=action, ok=ok)
                            handled = True
                    if handled:
                        continue
                if args.get('path'):
                    action = ('inserted' if tn in _INSERT_TOOLS else 'patched')
                    _set(args['path'], action=action, ok=ok)
                    continue

        # ── Single-file write_file ──
        if tn == 'write_file':
            args = _coerce_args(round_.get('toolArgs'))
            if args and args.get('path'):
                badge = (meta.get('badge') or '').lower()
                action = 'created' if 'created' in badge else 'written'
                _set(args['path'], action=action, ok=ok)
                continue

        # ── Fallback: parse meta.title (✅/❌/📝 prefix + filename) ──
        # NOTE: build_project_tool_meta() DEFAULTS meta['title'] to the bare
        # tool name (e.g. 'apply_diff') and no write-tool builder overwrites
        # it with a filename. So when a write round's toolArgs carries no
        # usable path (e.g. a malformed / auto-repaired apply_diff), the title
        # is just the tool name — NOT a real file. Emitting it would surface a
        # bogus file-change entry literally named "apply_diff". Only use the
        # title when it's a genuine filename, i.e. differs from the tool name.
        title = meta.get('title') or ''
        if title.startswith(('✅', '❌', '📝')):
            # Strip the emoji and any trailing whitespace.
            title = title[1:].lstrip()
        if title and title != tn:
            if tn in _DIFF_TOOLS:
                action = 'patched'
            elif tn in _INSERT_TOOLS:
                action = 'inserted'
            else:
                action = 'written'
            _set(title, action=action, ok=ok)

    return list(changes.values())


def extract_file_changes_dicts(tool_rounds: Iterable[dict]) -> List[dict]:
    """Same as :func:`extract_file_changes` but returns plain dicts.

    Preferred for serialisation paths (route handlers, JSON dumps).
    """
    return [fc.to_dict() for fc in extract_file_changes(tool_rounds)]


__all__ = ['FileChange', 'extract_file_changes',
           'extract_file_changes_dicts']
