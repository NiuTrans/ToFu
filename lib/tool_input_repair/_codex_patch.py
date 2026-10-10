"""Codex CLI freeform ``apply_patch`` payload → ``edit_file`` conversion.

Models trained on Codex CLI transcripts emit file edits as a freeform
``*** Begin Patch … *** End Patch`` text (verified in openai/codex
codex-rs `apply_patch` grammar), not as a JSON ``edit_file`` payload. The
name alias routes ``apply_patch`` to the unified edit surface; this module
converts the patch TEXT into ``edit_file`` ``edits=[…]`` operations.

Conversion discipline (fail-closed, honest guidance):

* ``Update File`` hunks convert 1:1 to ``replace`` edits — context +
  removed lines form the anchor, context + added lines the content.
  Sequential hunk order maps onto edit_file's "later edits see earlier
  changes" semantics.
* ``Add File`` / ``Delete File`` / ``Move to`` have no edit_file
  equivalent (it edits existing files only). The whole call fails with a
  targeted hint naming the right tool rather than half-applying a patch.
* A hunk whose anchor block would be empty (pure insertion with no
  context lines) fails: anchoring is edit_file's authority, and guessing
  a position is how files get corrupted.

Only the conversion lives here; authority (path roots, approvals, anchor
uniqueness) stays with the ordinary edit_file contract downstream.
"""

from __future__ import annotations

from typing import Any

_BEGIN = '*** Begin Patch'
_END = '*** End Patch'
_ADD = '*** Add File:'
_DELETE = '*** Delete File:'
_UPDATE = '*** Update File:'
_MOVE = '*** Move to:'
_EOF_MARK = '*** End of File'

#: How far into the payload the begin marker may appear. Models sometimes
#: prepend a sentence; a marker deep in the text is more likely prose
#: quoting a patch than an actual patch payload.
_LOOKAHEAD_CHARS = 400


def looks_like_codex_patch(text: Any) -> bool:
    """Cheap gate: does this raw arguments string hold a codex patch?"""
    return isinstance(text, str) and _BEGIN in text[:_LOOKAHEAD_CHARS]


def _fail(reason: str) -> tuple[None, str]:
    return None, reason


def _convert_hunk(path: str, hunk: list[str],
                  ) -> tuple[dict[str, Any] | None, str]:
    """Convert one @@ section into a replace edit (or a failure reason)."""
    old_lines = [line[1:] for line in hunk if line[:1] in (' ', '-')]
    new_lines = [line[1:] for line in hunk if line[:1] in (' ', '+')]
    old_block = '\n'.join(old_lines)
    new_block = '\n'.join(new_lines)
    if not old_block.strip() and not new_block.strip():
        return None, ''  # empty hunk — nothing to convert, not an error
    if not old_block:
        return _fail(
            f'a hunk for `{path}` only adds lines without context lines '
            'to anchor on; re-issue with edit_file using operation '
            '"insert_after"/"insert_before" and an exact anchor line.')
    return {
        'path': path,
        'operation': 'replace',
        'anchor': old_block,
        'content': new_block,
    }, ''


def parse_codex_patch(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a freeform codex patch into edit_file arguments.

    Returns ``(args, '')`` on success, ``(None, reason)`` when the payload
    is patch-shaped but not convertible. ``reason`` is model-facing
    guidance, not a log string. Callers gate with
    :func:`looks_like_codex_patch` first.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.strip() == _BEGIN)
    except StopIteration:
        return _fail(f'missing `{_BEGIN}` marker')

    edits: list[dict[str, Any]] = []
    paths: list[str] = []
    index = start + 1
    saw_end = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped == _END:
            saw_end = True
            break
        if stripped.startswith(_ADD):
            return _fail(
                'this harness edits existing files with edit_file; create '
                f'`{stripped[len(_ADD):].strip()}` with a separate write_file '
                'call instead of apply_patch Add File.')
        if stripped.startswith(_DELETE):
            return _fail(
                'apply_patch Delete File is not supported here; to empty '
                f'`{stripped[len(_DELETE):].strip()}`, use edit_file with '
                'operation "replace" and empty content.')
        if stripped.startswith(_UPDATE):
            path = stripped[len(_UPDATE):].strip()
            if not path:
                return _fail('Update File requires a path')
            index += 1
            # Optional Move-to line — no edit_file equivalent.
            if index < len(lines) \
                    and lines[index].strip().startswith(_MOVE):
                return _fail(
                    'apply_patch Move to (rename) is not supported by '
                    'edit_file; apply the content edits without renaming, '
                    'then ask before moving the file.')
            # Hunks until the next file op / End Patch.
            hunk: list[str] = []
            while index < len(lines):
                body = lines[index]
                body_stripped = body.strip()
                if body.startswith('***'):
                    if body_stripped == _EOF_MARK:
                        # EOF-anchored hunk terminator: close the current
                        # hunk, the next line resumes file-op scanning.
                        index += 1
                    break
                if body_stripped.startswith('@@'):
                    edit, error = _convert_hunk(path, hunk)
                    if error:
                        return _fail(error)
                    if edit:
                        edits.append(edit)
                        paths.append(path)
                    hunk = []
                    index += 1
                    continue
                # Lenient grammar: codex requires a ' '/-/+ prefix, but
                # bare lines from weaker models are treated as context —
                # anchor exactness is enforced downstream, honestly.
                hunk.append(body if body[:1] in (' ', '-', '+')
                            else ' ' + body)
                index += 1
            edit, error = _convert_hunk(path, hunk)
            if error:
                return _fail(error)
            if edit:
                edits.append(edit)
                paths.append(path)
            continue
        return _fail(f'unrecognized patch line: {stripped[:80]!r}')

    if not saw_end:
        # A truncated stream may legitimately end mid-patch; the parsed
        # prefix is still convertible. Require at least one edit so an
        # abandoned header does not become a no-op success.
        if not edits:
            return _fail(
                f'missing `{_END}` and no complete file operation found')
    if not edits:
        return _fail('no convertible Update File hunks found')
    distinct = list(dict.fromkeys(paths))
    description = (f'Update {distinct[0]}' if len(distinct) == 1
                   else f'Update {len(distinct)} files via patch')
    return {'description': description[:120], 'edits': edits}, ''


__all__ = ['looks_like_codex_patch', 'parse_codex_patch']
