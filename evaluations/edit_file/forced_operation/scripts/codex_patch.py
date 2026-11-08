"""Faithful Python port of Codex CLI apply_patch (vendored codex @ 5c5308fc9a9e, 2026-09-20).

Sources ported (codex-rs/apply-patch/src): parser.rs (lenient boundaries),
streaming_parser.rs (hunk grammar and error texts), seek_sequence.rs (match
ladder), file_update.rs (compute/apply replacements, NormalizeToLf default),
lib.rs (hunk order, summary text), invocation.rs (verification: every hunk is
computed before anything is written; two operations on one path are refused),
core/src/tools/handlers/apply_patch.rs ("apply_patch verification failed: ...").

The executor works on an in-memory {path: text} map. Model-facing success text is
the CLI summary ("Success. Updated the following files:"); Codex core may wrap
exec output differently (not verified).
"""
from __future__ import annotations

BEGIN = '*** Begin Patch'
END = '*** End Patch'
ADD = '*** Add File: '
DELETE = '*** Delete File: '
UPDATE = '*** Update File: '
MOVE = '*** Move to: '
EOF_MARKER = '*** End of File'
CTX = '@@ '
EMPTY_CTX = '@@'
ENV = '*** Environment ID:'


class ParseError(Exception):
    def __init__(self, message, line_number=None):
        super().__init__(message)
        self.message = message
        self.line_number = line_number

    def __str__(self):
        if self.line_number is None:
            return f'invalid patch: {self.message}'
        return f'invalid hunk at line {self.line_number}, {self.message}'


class ApplyError(Exception):
    pass


def _rust_lines(text):
    """str::lines(): split on \n, drop one trailing empty piece, strip a final \r."""
    if text == '':
        return []
    parts = text.split('\n')
    if parts[-1] == '':
        parts.pop()
    return [p[:-1] if p.endswith('\r') else p for p in parts]


def _check_strict(lines):
    first = lines[0].strip() if lines else None
    last = lines[-1].strip() if lines else None
    if first is not None and last is not None and first == BEGIN and last == END:
        return lines
    if first is not None and first != BEGIN:
        raise ParseError("The first line of the patch must be '*** Begin Patch'")
    raise ParseError("The last line of the patch must be '*** End Patch'")


def _check_lenient(lines):
    try:
        return _check_strict(lines)
    except ParseError as original:
        if len(lines) >= 2:
            first, last = lines[0], lines[-1]
            if first in ('<<EOF', "<<'EOF'", '<<"EOF"') and last.endswith('EOF') and len(lines) >= 4:
                return _check_strict(lines[1:-1])
        raise original


class _Chunk:
    def __init__(self, change_context=None):
        self.change_context = change_context
        self.old_lines = []
        self.new_lines = []
        self.is_end_of_file = False

    def push_context(self, line):
        self.old_lines.append(line)
        self.new_lines.append(line)


class _Parser:
    def __init__(self):
        self.buffer = ''
        self.mode = 'not_started'
        self.hunk_line = 0
        self.hunks = []
        self.environment_id = None
        self.line_number = 0

    def _ensure_update_not_empty(self, line):
        if self.hunks and self.hunks[-1]['type'] == 'update':
            chunks = self.hunks[-1]['chunks']
            if not chunks and self.mode == 'update':
                raise ParseError(f"Update file hunk for path '{self.hunks[-1]['path']}' is empty",
                                 self.hunk_line)
            if chunks and not chunks[-1].old_lines and not chunks[-1].new_lines:
                if line == END:
                    raise ParseError('Update hunk does not contain any lines', self.line_number)
                raise ParseError(
                    f"Unexpected line found in update hunk: '{line}'. Every line should start with "
                    "' ' (context line), '+' (added line), or '-' (removed line)", self.line_number)

    def _headers(self, trimmed):
        if self.mode == 'started' and trimmed.startswith(ENV):
            if self.environment_id is not None:
                raise ParseError('apply_patch environment_id cannot be specified more than once')
            value = trimmed[len(ENV):].strip()
            if not value:
                raise ParseError('apply_patch environment_id cannot be empty')
            self.environment_id = value
            return True
        if trimmed == END:
            self._ensure_update_not_empty(trimmed)
            self.mode = 'ended'
            return True
        if trimmed.startswith(ADD):
            self._ensure_update_not_empty(trimmed)
            self.hunks.append({'type': 'add', 'path': trimmed[len(ADD):], 'contents': ''})
            self.mode = 'add'
            return True
        if trimmed.startswith(DELETE):
            self._ensure_update_not_empty(trimmed)
            self.hunks.append({'type': 'delete', 'path': trimmed[len(DELETE):]})
            self.mode = 'delete'
            return True
        if trimmed.startswith(UPDATE):
            self._ensure_update_not_empty(trimmed)
            self.hunks.append({'type': 'update', 'path': trimmed[len(UPDATE):], 'move_path': None,
                               'chunks': []})
            self.mode = 'update'
            self.hunk_line = self.line_number
            return True
        return False

    def push(self, delta):
        for ch in delta:
            if ch == '\n':
                line, self.buffer = self.buffer, ''
                if line.endswith('\r'):
                    line = line[:-1]
                self.line_number += 1
                self._process(line)
            else:
                self.buffer += ch

    def finish(self):
        if self.buffer:
            line, self.buffer = self.buffer, ''
            self.line_number += 1
            if line.strip() == END:
                self._ensure_update_not_empty(line.strip())
                self.mode = 'ended'
            else:
                self._process(line)
        if self.mode != 'ended':
            raise ParseError("The last line of the patch must be '*** End Patch'")
        return self.hunks

    def _bad_header(self, trimmed):
        return ParseError(
            f"'{trimmed}' is not a valid hunk header. Valid hunk headers: '*** Add File: {{path}}', "
            "'*** Delete File: {path}', '*** Update File: {path}'", self.line_number)

    def _process(self, line):
        trimmed = line.strip()
        if self.mode == 'not_started':
            if trimmed == BEGIN:
                self.mode = 'started'
                return
            raise ParseError("The first line of the patch must be '*** Begin Patch'")
        if self.mode == 'started':
            if self._headers(trimmed):
                return
            raise self._bad_header(trimmed)
        if self.mode == 'add':
            if self._headers(trimmed):
                return
            if line.startswith('+'):
                self.hunks[-1]['contents'] += line[1:] + '\n'
                return
            raise self._bad_header(trimmed)
        if self.mode == 'delete':
            if self._headers(trimmed):
                return
            raise self._bad_header(trimmed)
        if self.mode == 'update':
            update_line = line.rstrip()
            if self._headers(update_line):
                return
            hunk = self.hunks[-1]
            chunks = hunk['chunks']
            if chunks and chunks[-1].is_end_of_file:
                if not update_line:
                    return
                if update_line != EMPTY_CTX and not update_line.startswith(CTX):
                    raise ParseError(
                        f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                        self.line_number)
            if not chunks and hunk['move_path'] is None and update_line.startswith(MOVE):
                hunk['move_path'] = update_line[len(MOVE):]
                return
            if ((update_line == EMPTY_CTX or update_line.startswith(CTX))
                    and chunks and not chunks[-1].old_lines and not chunks[-1].new_lines):
                raise ParseError(
                    f"Unexpected line found in update hunk: '{line}'. Every line should start with "
                    "' ' (context line), '+' (added line), or '-' (removed line)", self.line_number)
            if update_line == EMPTY_CTX:
                chunks.append(_Chunk())
                return
            if update_line.startswith(CTX):
                chunks.append(_Chunk(update_line[len(CTX):]))
                return
            if update_line == EOF_MARKER:
                if chunks and not chunks[-1].old_lines and not chunks[-1].new_lines:
                    raise ParseError('Update hunk does not contain any lines', self.line_number)
                if chunks:
                    chunks[-1].is_end_of_file = True
                return
            if line == '':
                if not chunks:
                    chunks.append(_Chunk())
                chunks[-1].push_context('')
                return
            if line.startswith(' '):
                if not chunks:
                    chunks.append(_Chunk())
                chunks[-1].push_context(line[1:])
                return
            if line.startswith('+'):
                if not chunks:
                    chunks.append(_Chunk())
                chunks[-1].new_lines.append(line[1:])
                return
            if line.startswith('-'):
                if not chunks:
                    chunks.append(_Chunk())
                chunks[-1].old_lines.append(line[1:])
                return
            if chunks and (chunks[-1].old_lines or chunks[-1].new_lines):
                raise ParseError(
                    f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                    self.line_number)
            raise ParseError(
                f"Unexpected line found in update hunk: '{line}'. Every line should start with "
                "' ' (context line), '+' (added line), or '-' (removed line)", self.line_number)
        if self.mode == 'ended':
            if trimmed:
                raise ParseError("The last line of the patch must be '*** End Patch'")


def parse_patch(patch):
    lines = _rust_lines(patch.strip())
    body = _check_lenient(lines)
    parser = _Parser()
    parser.push('\n'.join(body))
    return parser.finish()


_PUNCT = {**{c: '-' for c in '\u2010\u2011\u2012\u2013\u2014\u2015\u2212'},
          **{c: "'" for c in '\u2018\u2019\u201a\u201b'},
          **{c: '"' for c in '\u201c\u201d\u201e\u201f'},
          **{c: ' ' for c in '\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000'}}


def _normalise(s):
    return ''.join(_PUNCT.get(c, c) for c in s.strip())


def seek_sequence(lines, pattern, start, eof):
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = len(lines) - len(pattern) if eof else start
    stop = len(lines) - len(pattern)
    for key in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip(), _normalise):
        for i in range(search_start, stop + 1):
            if all(key(lines[i + j]) == key(p) for j, p in enumerate(pattern)):
                return i
    return None


def derive_new_contents(original, path, chunks):
    lines = original.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    replacements = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            idx = seek_sequence(lines, [chunk.change_context], line_index, False)
            if idx is None:
                raise ApplyError(f"Failed to find context '{chunk.change_context}' in {path}")
            line_index = idx + 1
        if not chunk.old_lines:
            insertion = len(lines) - 1 if lines and lines[-1] == '' else len(lines)
            replacements.append((insertion, 0, list(chunk.new_lines)))
            continue
        pattern = chunk.old_lines
        new_slice = chunk.new_lines
        found = seek_sequence(lines, pattern, line_index, chunk.is_end_of_file)
        if found is None and pattern and pattern[-1] == '':
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == '':
                new_slice = new_slice[:-1]
            found = seek_sequence(lines, pattern, line_index, chunk.is_end_of_file)
        if found is None:
            raise ApplyError(f'Failed to find expected lines in {path}:\n' + '\n'.join(chunk.old_lines))
        replacements.append((found, len(pattern), list(new_slice)))
        line_index = found + len(pattern)
    replacements.sort(key=lambda r: r[0])
    for start, old_len, new_segment in reversed(replacements):
        for _ in range(old_len):
            if start < len(lines):
                del lines[start]
        for offset, new_line in enumerate(new_segment):
            lines.insert(start + offset, new_line)
    if not lines or lines[-1] != '':
        lines.append('')
    return '\n'.join(lines)


def apply_patch(patch, files, resolve):
    """Verify then apply. `files` maps canonical path -> text (mutated on success).

    `resolve(path_text)` maps a patch path to a canonical key or None (outside the
    sandboxed project). Returns (ok, model_text).
    """
    try:
        hunks = parse_patch(patch)
    except ParseError as e:
        return False, f'apply_patch verification failed: {e}'
    changes = {}
    order = []
    try:
        for hunk in hunks:
            key = resolve(hunk['path'])
            if key is None:
                raise ApplyError(f"Failed to read file to update {hunk['path']}")
            if key in changes:
                raise ApplyError(f'invalid patch: multiple operations target {hunk["path"]}')
            if hunk['type'] == 'add':
                changes[key] = ('add', hunk['contents'], None)
            elif hunk['type'] == 'delete':
                if key not in files:
                    raise ApplyError(f"Failed to read {hunk['path']}")
                changes[key] = ('delete', None, None)
            else:
                if key not in files:
                    raise ApplyError(f"Failed to read file to update {hunk['path']}: "
                                     'No such file or directory (os error 2)')
                new = derive_new_contents(files[key], hunk['path'], hunk['chunks'])
                move = resolve(hunk['move_path']) if hunk['move_path'] else None
                changes[key] = ('update', new, move)
            order.append((hunk, key))
    except ApplyError as e:
        return False, f'apply_patch verification failed: {e}'
    if not hunks:
        return False, 'No files were modified.'
    added, modified, deleted = [], [], []
    for hunk, key in order:
        kind, text, move = changes[key]
        if kind == 'add':
            files[key] = text
            added.append(hunk['path'])
        elif kind == 'delete':
            files.pop(key, None)
            deleted.append(hunk['path'])
        else:
            if move:
                files.pop(key, None)
                files[move] = text
                modified.append(hunk['move_path'])
            else:
                files[key] = text
                modified.append(hunk['path'])
    out = ['Success. Updated the following files:']
    out += [f'A {p}' for p in added] + [f'M {p}' for p in modified] + [f'D {p}' for p in deleted]
    return True, '\n'.join(out) + '\n'
