"""Per-turn net-diff builder (Codex-inspired — codex-rs ``turn_diff_tracker.rs``).

codex-rs maintains a running unified diff from committed apply_patch deltas
without re-reading the workspace.  The Tofu analogue leans on the
modifications journal (``lib.project_mod.modifications``): every write tool
already records the PRE-image of each write (``originalContent``), so the
NET per-turn diff = the FIRST pre-image of each path vs its current disk
content — computed lazily at query time (context compaction), never on the
hot path, and bounded by size caps so a pathological file can never stall
the summarizer.

Consumers: automatic L2 (``compaction._layer2._compact``) and manual
/compact (``compaction._manual``) append the block to the summary text, so
the exact nature of code changes survives lossy compaction — not just
WHICH files, but WHAT changed.

Dependency direction: this module reads ``lib.project_mod`` + the
filesystem only; it never imports the orchestration loop (same rule as
``_derive``).
"""

from __future__ import annotations

import difflib
import os

from lib.log import get_logger

logger = get_logger(__name__)


def _turn_mods(task: dict, roots: list[str]) -> list[dict]:
    """Task-scoped journal mods, deduped to the FIRST record per (root, path).

    Mirrors the taskId-stamp / timestamp-fallback rule of
    ``_derive.derive_round_modified_files`` so both views of "this round's
    edits" can never drift apart.  The first record carries the true
    baseline pre-image; later records of the same path are intermediate
    states the net diff must skip.
    """
    from lib.project_mod import get_modifications

    conv_id = (task or {}).get('convId')
    task_id = (task or {}).get('id')
    task_start = (task or {}).get('created_at', 0)

    mods: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        root_mods = get_modifications(root, conv_id=conv_id) or []
        own = [m for m in root_mods if m.get('taskId') == task_id]
        if not own and task_start:
            own = [m for m in root_mods
                   if m.get('timestamp', 0) >= task_start]
        for m in own:
            key = (root, m.get('path', ''))
            if key in seen:
                continue
            seen.add(key)
            mods.append(m)
    return mods


def _decode_baseline(mod: dict):
    """Pre-image text of a mod, or None when the file did not exist.

    Returns ``(text_or_None, status)`` with status in
    ``('ok', 'created', 'binary')``.
    """
    if not mod.get('existed', True):
        return None, 'created'
    if 'originalContent' not in mod:
        # apply_diff records may carry only a reversePatch — no full
        # pre-image to diff against.
        return None, 'no_preimage'
    try:
        from lib.project_mod.modifications import _decode_original
        original = _decode_original(mod)
    except Exception as e:
        logger.debug('[TurnDiff] baseline decode failed for %s: %s',
                     mod.get('path'), e)
        return None, 'binary'
    if isinstance(original, bytes):
        return None, 'binary'
    return original, 'ok'


def _read_current(base_path: str, path: str, max_file_chars: int):
    """Current on-disk text, or a status marker. Never raises."""
    target = path if os.path.isabs(path) else os.path.join(base_path, path)
    try:
        size = os.path.getsize(target)
    except OSError:
        return None, 'missing'
    if size > max_file_chars * 2:
        return None, 'too_big'
    try:
        with open(target, 'rb') as f:
            raw = f.read(max_file_chars * 2 + 1)
    except OSError as e:
        logger.debug('[TurnDiff] read failed for %s: %s', path, e)
        return None, 'missing'
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None, 'binary'
    return text, 'ok'


def build_turn_diff_block(
    task: dict | None,
    project_path: str | None,
    project_paths: list[str] | None = None,
    *,
    max_files: int = 6,
    max_file_chars: int = 64_000,
    max_diff_lines: int = 60,
    max_total_chars: int = 6_000,
) -> str | None:
    """Build a markdown block with this turn's net per-file unified diffs.

    Returns ``None`` when the task has no journal-stamped modifications or
    no project root — the caller then appends nothing (byte-identical to
    pre-feature behavior).

    Caps (presentation knobs, same class as ``max_files=8`` in
    ``_extract_recently_accessed_files`` — not §10.1 hyperparameters):
      * ``max_files``       — at most N files carry an inline diff.
      * ``max_file_chars``  — larger files get a stats line, not a diff
        (difflib is O(n²) in the worst case; the cap is the deterministic
        analogue of codex-rs's 100 ms diff timeout).
      * ``max_diff_lines``  — per-file diff body truncation.
      * ``max_total_chars`` — whole-block ceiling; overflow is summarised.
    """
    if not task:
        return None
    roots: list[str] = []
    for p in ([project_path] + list((project_paths or [])[1:])):
        if p and p not in roots:
            roots.append(p)
    if not roots:
        return None

    try:
        mods = _turn_mods(task, roots)
    except Exception as e:
        logger.debug('[TurnDiff] journal scan failed: %s', e)
        return None
    if not mods:
        return None

    sections: list[str] = []
    total_added = 0
    total_removed = 0
    files_changed = 0
    files_omitted = 0
    spent = 0

    for mod in mods:
        path = mod.get('path', '')
        if not path:
            continue
        base = mod.get('basePath') or roots[0]
        baseline, base_status = _decode_baseline(mod)
        current, cur_status = _read_current(base, path, max_file_chars)

        if baseline is not None and len(baseline) > max_file_chars:
            baseline = None
            base_status = 'too_big'

        if base_status == 'no_preimage' or cur_status == 'binary' \
                or base_status == 'binary':
            files_changed += 1
            files_omitted += 1
            continue

        if baseline is None and cur_status == 'missing':
            continue  # created then deleted within the turn — net zero.
        if baseline is not None and current is not None \
                and baseline == current:
            continue  # edited then reverted byte-identically.

        action = ('created' if baseline is None
                  else 'deleted' if cur_status == 'missing'
                  else 'modified')

        if len(sections) >= max_files or spent >= max_total_chars \
                or cur_status == 'too_big' or base_status == 'too_big':
            # Oversized/binary/over-budget: counted as changed, no diff body
            # and no line stats (line deltas from a truncated read would lie).
            files_changed += 1
            files_omitted += 1
            continue

        files_changed += 1

        old_lines = (baseline or '').splitlines()
        new_lines = (current or '').splitlines()
        diff = list(difflib.unified_diff(
            old_lines, new_lines, lineterm='', n=3))
        added = sum(1 for ln in diff
                    if ln.startswith('+') and not ln.startswith('+++'))
        removed = sum(1 for ln in diff
                      if ln.startswith('-') and not ln.startswith('---'))
        total_added += added
        total_removed += removed

        body = '\n'.join(diff)
        body_lines = diff
        if len(body_lines) > max_diff_lines:
            elided = len(body_lines) - max_diff_lines
            body = ('\n'.join(body_lines[:max_diff_lines])
                    + f'\n… ({elided} more diff lines)')
        header = f'--- a/{path}\n+++ b/{path}' if action == 'modified' \
            else (f'+++ b/{path} (new file)' if action == 'created'
                  else f'--- a/{path} (deleted)')
        section = f'**{path}** ({action}, +{added}/−{removed})\n' \
                  f'```diff\n{header}\n{body}\n```'
        if spent + len(section) > max_total_chars and sections:
            files_omitted += 1
            continue
        sections.append(section)
        spent += len(section)

    if not files_changed:
        return None

    stats = (f'{files_changed} file(s) changed this turn: '
             f'+{total_added}/−{total_removed}')
    if files_omitted:
        stats += (f' ({files_omitted} file(s) listed without diff — '
                  'oversized/binary/over budget)')
    block = f'### Files Modified This Turn (net diff)\n{stats}'
    if sections:
        block += '\n\n' + '\n\n'.join(sections)
    logger.info('[TurnDiff] task=%s files=%d +%d/−%d omitted=%d chars=%d',
                (task.get('id') or '')[:8], files_changed, total_added,
                total_removed, files_omitted, len(block))
    return block
