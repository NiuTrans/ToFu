"""Model-facing file globs shared by ``search_files`` and ``find_files``.

Responsibility: parse one ``search_files`` include or ``find_files`` pattern,
refuse spellings that are malformed or that ripgrep would not treat as a
plain file selector, and select project-relative paths exactly as ripgrep's
``--iglob`` does. The tree-index prefilter and both Python walkers select
through ``ProjectGlob.matches``; ripgrep receives the validated glob
verbatim; fd (``--glob``, file names only) runs only when
``ProjectGlob.name_only``; GNU grep only through
``ProjectGlob.grep_includes()``. A backend that cannot express a glob exactly
yields to the Python walker, so one parse decides what every backend selects.

Dialect: ripgrep reads the glob as a gitignore line compiled by Rust
``globset`` with a literal separator, matched against the path's UTF-8 bytes.

* ``*`` and ``?`` never match '/'; ``?`` matches one byte. ``[...]`` (``!`` or
  ``^`` negates, ``a-z`` ranges) matches one byte of the UTF-8 encoding, '/'
  included when the class admits it. ``\\`` escapes the next character.
* ``**`` spans directories at the start, between two '/', or at the end of
  the glob or of a brace alternative; elsewhere it is ``*``.
* ``{a,b}`` alternatives nest and repeat. Each group needs two or more
  non-empty alternatives: globset drops empty ones and reads ``{a}`` as
  ``a`` while shells keep them, so those spellings are refused.
* A glob without '/' is matched as ``**/glob``: against the file name at any
  depth (a class admitting '/' can also span a directory boundary). A glob
  with '/' anywhere, braces included, must match the whole path relative to
  the project root; a leading '/' only anchors it there. Callers enumerate
  files beneath the requested path only, so no glob selects a file outside it.
* ASCII letters match case-insensitively unless the caller asks otherwise.

Also refused, because ripgrep would not select the matching files: a leading
'!' (exclusion), a leading '#' (comment), trailing unescaped whitespace
(trimmed by ripgrep, kept by fd) and a trailing '/' (directories only).

Matching never backtracks: a glob compiles to steps over the set of byte
offsets reachable so far, held as one Python int per path, so the cost is
linear in the glob and path lengths whatever the alternatives and stars.

Entry points: ``compile_glob``, ``ProjectGlob``, ``GlobPatternError``.
Dependencies: standard library only.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class GlobPatternError(ValueError):
    """A refused glob; the message names the repair."""


class _Token(NamedTuple):
    kind: str              # 'lit', 'any' ('?'), 'class', 'star', 'tree', 'group'
    source: str = ''       # 'star', 'tree': verbatim glob text
    char: str = ''         # 'lit'
    ranges: tuple = ()     # 'class': inclusive (low, high) character pairs
    negated: bool = False  # 'class'
    tree: str = ''         # 'tree': 'prefix' '**/', 'infix' '/**/', 'suffix' '/**'
    branches: tuple = ()   # 'group': one token tuple per alternative


_SLASH = ord('/')
_FNMATCH_SPECIAL = '*?[\\'


def _utf8(text):
    return text.encode('utf-8', 'surrogateescape')


def _star(source):
    return _Token('star', source=source)


def _parse_class(glob, index, tokens):
    """Append one ``[...]`` class starting at *index*; return the next index.

    Mirrors globset: a leading ']' or '-' is literal, a trailing '-' is
    literal, and no escapes are recognized inside the brackets.
    """
    index += 1
    negated = index < len(glob) and glob[index] in '!^'
    if negated:
        index += 1
    ranges = []
    first = True
    in_range = False
    while True:
        if index >= len(glob):
            raise GlobPatternError(
                "unclosed character class: add the missing ']' or match a "
                "literal '[' with '[[]'")
        char = glob[index]
        index += 1
        if char == ']' and not first:
            break
        if char == '-' and not first and not in_range:
            in_range = True
        elif in_range:
            low = ranges[-1][0]
            if char < low:
                raise GlobPatternError(
                    f"character range '{low}-{char}' runs backwards: write "
                    f"'{char}-{low}'")
            ranges[-1] = (low, char)
            in_range = False
        else:
            ranges.append((char, char))
        first = False
    if in_range:
        ranges.append(('-', '-'))
    tokens.append(_Token('class', ranges=tuple(ranges), negated=negated))
    return index


def _parse_star(glob, index, tokens, prev, in_group):
    """Append the token for the ``*`` at *index*; return (next index, prev).

    ``**`` follows globset: it spans directories only at the start of the
    glob or of an alternative, or between '/' and '/' or the end.
    """
    if glob[index + 1:index + 2] != '*':
        tokens.append(_star('*'))
        return index + 1, '*'
    after = glob[index + 2:index + 3]
    if not tokens:
        if after in ('', '/'):
            end = index + 2 + len(after)
            tokens.append(_Token('tree', source=glob[index:end], tree='prefix'))
            return end, after or '*'
        tokens.append(_star('**'))
        return index + 2, '*'
    if prev != '/':
        tokens.append(_star('**'))
        return index + 2, '*'
    if after == '' or (in_group and after in ',}'):
        tree, end, prev = 'suffix', index + 2, '*'
    elif after == '/':
        tree, end, prev = 'infix', index + 3, '/'
    else:
        tokens.append(_star('**'))
        return index + 2, '*'
    replaced = tokens.pop()
    if replaced.tree in ('prefix', 'suffix'):
        tree = replaced.tree
    tokens.append(_Token('tree', source=glob[index - 1:end], tree=tree))
    return end, prev


def _check_group(source, branches):
    if any(not branch for branch in branches):
        raise GlobPatternError(
            f"brace group '{source}' has an empty alternative, which "
            "ripgrep ignores: list every alternative in full, e.g. "
            "'{name,name.js}' instead of 'name{,.js}'")
    if len(branches) < 2:
        raise GlobPatternError(
            f"brace group '{source}' needs two or more comma-separated "
            "alternatives; match literal braces with '[{]' and '[}]'")


def _parse(glob):
    root = []
    groups = []            # open brace groups: (branches, start index)
    tokens = root
    prev = None            # previous glob character, as globset tracks it
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == '\\':
            if index + 1 == len(glob):
                raise GlobPatternError(
                    "trailing '\\' escapes nothing: match a literal "
                    "backslash with '[\\]'")
            prev = glob[index + 1]
            tokens.append(_Token('lit', char=prev))
            index += 2
            continue
        if char == '[':
            index = _parse_class(glob, index, tokens)
            prev = ']'
            continue
        if char == '*':
            index, prev = _parse_star(glob, index, tokens, prev, bool(groups))
            continue
        if char == '?':
            tokens.append(_Token('any'))
        elif char == '{':
            tokens = []
            groups.append(([tokens], index))
        elif char == ',' and groups:
            tokens = []
            groups[-1][0].append(tokens)
        elif char == '}':
            if not groups:
                raise GlobPatternError(
                    "'}' closes no brace group: match a literal '}' with '[}]'")
            branches, start = groups.pop()
            _check_group(glob[start:index + 1], branches)
            tokens = groups[-1][0][-1] if groups else root
            tokens.append(_Token('group', branches=tuple(
                tuple(branch) for branch in branches)))
        else:
            tokens.append(_Token('lit', char=char))
        prev = char
        index += 1
    if groups:
        raise GlobPatternError(
            "unclosed brace group: add the missing '}' or match a literal "
            "'{' with '[{]'")
    return tuple(root)


def _class_bytes(token, fold):
    """Bytes matched by the one-byte class globset compiles for *token*."""
    members = b''.join(
        re.escape(_utf8(low)) if low == high else
        re.escape(_utf8(low)) + b'-' + re.escape(_utf8(high))
        for low, high in token.ranges)
    regex = re.compile(b'[' + (b'^' if token.negated else b'') + members + b']',
                       re.IGNORECASE if fold else 0)
    return frozenset(
        byte for byte in range(256) if regex.fullmatch(bytes((byte,))))


def _admits_slash(tokens):
    return any(
        (token.kind == 'class' and _SLASH in _class_bytes(token, False))
        or (token.kind == 'group'
            and any(_admits_slash(branch) for branch in token.branches))
        for token in tokens)


# Program steps over byte offsets; a step is (opcode, operand).
_LITERAL, _BYTE, _STAR, _PREFIX, _INFIX, _SUFFIX, _GROUP, _LITERALS = range(8)
_TREE_OPCODES = {'prefix': _PREFIX, 'infix': _INFIX, 'suffix': _SUFFIX}
_SLASH_TABLE = 0           # predicate tables[0] is always the '/' byte


def _program(tokens, fold, tables):
    """Steps for *tokens*. Adjacent literals become one _LITERAL step, folded
    like the path; every other one-byte predicate becomes a translate table
    (member bytes to '1', others to '0') appended to *tables* once."""
    steps = []
    for token in tokens:
        if token.kind == 'lit':
            literal = _utf8(token.char)
            if fold:
                literal = literal.lower()     # ASCII only, like the path
            if steps and steps[-1][0] == _LITERAL:
                steps[-1] = (_LITERAL, steps[-1][1] + literal)
            else:
                steps.append((_LITERAL, literal))
        elif token.kind in ('any', 'class'):
            members = (_class_bytes(token, fold) if token.kind == 'class'
                       else frozenset(range(256)) - {_SLASH})
            table = bytes(0x31 if byte in members else 0x30
                          for byte in range(256))
            if table not in tables:
                tables.append(table)
            steps.append((_BYTE, tables.index(table)))
        elif token.kind == 'star':
            steps.append((_STAR, None))
        elif token.kind == 'tree':
            steps.append((_TREE_OPCODES[token.tree], None))
        else:
            branches = tuple(
                _program(branch, fold, tables) for branch in token.branches)
            if all(len(branch) == 1 and branch[0][0] == _LITERAL
                   for branch in branches):
                steps.append((_LITERALS, tuple(
                    branch[0][1] for branch in branches)))
            else:
                steps.append((_GROUP, branches))
    return tuple(steps)


class _Offsets:
    """Bit masks over the byte offsets of one path, computed on first use:
    bit i of a predicate mask is set when byte i satisfies the predicate."""

    __slots__ = ('data', 'folded', 'tables', 'masks', 'names', 'ends')

    def __init__(self, data, folded, tables):
        self.data = data
        self.folded = folded
        self.tables = tables
        self.masks = [None] * len(tables)
        self.names = None
        self.ends = (2 << len(data)) - 1      # offsets 0..len(data)

    def after(self, literal, state):
        """Offsets just past each *literal* that starts at an offset in
        *state* (non-empty)."""
        if not state & (state - 1):
            start = state.bit_length() - 1
            return state << len(literal) if self.folded.startswith(
                literal, start) else 0
        found = 0
        index = self.folded.find(literal, (state & -state).bit_length() - 1)
        while index >= 0:
            found |= 1 << index
            index = self.folded.find(literal, index + 1)
        return (state & found) << len(literal)

    def mask(self, index):
        value = self.masks[index]
        if value is None:
            # Reversed so the first byte lands in the lowest bit.
            text = self.data.translate(self.tables[index])[::-1]
            value = self.masks[index] = int(text, 2) if text else 0
        return value

    def non_slash(self):
        if self.names is None:
            self.names = self.ends >> 1
            if b'/' in self.data:
                self.names &= ~self.mask(_SLASH_TABLE)
        return self.names


def _run(steps, state, offsets):
    """Offsets where *steps* can end when they start at any offset in *state*."""
    for opcode, operand in steps:
        if opcode == _LITERAL:
            state = offsets.after(operand, state)
        elif opcode == _BYTE:
            state = (state & offsets.mask(operand)) << 1
        elif opcode == _STAR:
            # One addition carries every offset to the end of its run of
            # non-'/' bytes; xor keeps the offsets passed on the way.
            names = offsets.non_slash()
            state |= ((state & names) + names) ^ names
        elif opcode == _LITERALS:
            reached = 0
            for literal in operand:
                reached |= offsets.after(literal, state)
            state = reached
        elif opcode == _GROUP:
            reached = 0
            for branch in operand:
                reached |= _run(branch, state, offsets)
            state = reached
        else:
            slashes = offsets.mask(_SLASH_TABLE)
            if opcode == _PREFIX:        # '', or anything ending in '/'
                state |= (slashes & -(state & -state)) << 1
            elif opcode == _INFIX:       # '/', or '/' anything '/'
                state = (state & slashes) << 1
                state |= (slashes & -(state & -state)) << 1
            else:                        # '/' then anything
                first = state & slashes
                state = offsets.ends & -((first & -first) << 1)
        if not state:
            return 0
    return state


def _plain_run_length(tokens):
    """How many leading *tokens* are literals or one brace group of literal
    alternatives; one group keeps the spellings one per alternative."""
    grouped = False
    for length, token in enumerate(tokens):
        if token.kind == 'lit':
            continue
        if token.kind == 'group' and not grouped and all(
                part.kind == 'lit'
                for branch in token.branches for part in branch):
            grouped = True
            continue
        return length
    return len(tokens)


def _spellings(tokens, fold):
    """Every byte spelling of a plain run (see ``_plain_run_length``)."""
    spelled = [b'']
    for token in tokens:
        options = ([_utf8(token.char)] if token.kind == 'lit' else
                   [_utf8(''.join(part.char for part in branch))
                    for branch in token.branches])
        spelled = [head + tail for head in spelled for tail in options]
    return tuple(text.lower() if fold else text for text in spelled)


def _fnmatch_class_char(char):
    return char if char.isalnum() else '\\' + char


def _fnmatch_text(token, fold):
    """fnmatch(3) spelling of one brace-free token, or None when fnmatch
    cannot spell it exactly: ``?`` and negated or non-ASCII classes match
    one byte in ripgrep but one character in a UTF-8 locale."""
    if token.kind == 'lit':
        char = token.char
        if fold and char.isascii() and char.isalpha():
            return f'[{char.lower()}{char.upper()}]'
        return '\\' + char if char in _FNMATCH_SPECIAL else char
    if token.kind in ('star', 'tree'):
        return token.source
    if token.kind != 'class' or token.negated or not all(
            high.isascii() for _, high in token.ranges):
        return None
    # Members are listed one by one because fnmatch ranges follow the
    # locale's collation order rather than ripgrep's byte order.
    members = set()
    for low, high in token.ranges:
        for code in range(ord(low), ord(high) + 1):
            members.add(chr(code))
            if fold and chr(code).isalpha():
                members.add(chr(code).swapcase())
    # Escaping every non-alphanumeric member keeps ']', '-', '!', '^', '\\'
    # and '[' literal wherever sorting places them.
    return '[' + ''.join(map(_fnmatch_class_char, sorted(members))) + ']'


def _spelling_extent(tokens, fold):
    """``(count, total bytes)`` of the fnmatch spellings of *tokens*, counted
    without listing them, or None when some token has no exact spelling."""
    count, size = 1, 0
    for token in tokens:
        if token.kind == 'group':
            extents = [_spelling_extent(branch, fold)
                       for branch in token.branches]
            if None in extents:
                return None
            options = sum(branch_count for branch_count, _ in extents)
            length = sum(branch_size for _, branch_size in extents)
        else:
            text = _fnmatch_text(token, fold)
            if text is None:
                return None
            options, length = 1, len(_utf8(text))
        count, size = count * options, size * options + length * count
    return count, size


def _fnmatch_texts(tokens, fold):
    texts = ['']
    for token in tokens:
        if token.kind == 'group':
            options = [text for branch in token.branches
                       for text in _fnmatch_texts(branch, fold)]
        else:
            options = [_fnmatch_text(token, fold)]
        texts = [head + tail for head in texts for tail in options]
    return texts


class ProjectGlob:
    """One validated glob. ``matches`` takes a '/'-separated path relative to
    the project root; a bare file name suffices when ``name_only``."""

    __slots__ = ('glob', 'anchored', 'name_only', '_tokens', '_fold',
                 '_prefixes', '_suffixes', '_suffix_decides', '_everything',
                 '_steps', '_tables')

    def __init__(self, glob, tokens, anchored, case_sensitive):
        self.glob = glob
        self.anchored = anchored
        # Unanchored and unable to match '/': the file name alone decides,
        # exactly as fd --glob and GNU grep --include decide.
        self.name_only = not anchored and not _admits_slash(tokens)
        self._tokens = tokens
        self._fold = not case_sensitive
        self._everything = len(tokens) == 1 and tokens[0].tree == 'prefix'
        # Literal ends every match must have: cheap rejection before the
        # program runs, and the whole answer for '*.ext'-like globs.
        head = _plain_run_length(tokens)
        tail = _plain_run_length(tokens[::-1])
        self._prefixes = (_spellings(tokens[:head], self._fold)
                          if head and (anchored or self.name_only) else ())
        self._suffixes = (_spellings(tokens[len(tokens) - tail:], self._fold)
                          if tail else ())
        self._suffix_decides = (self.name_only and tokens[0].kind == 'star'
                                and tail == len(tokens) - 1)
        self._tables = [bytes(0x31 if byte == _SLASH else 0x30
                              for byte in range(256))]
        steps = _program(tokens, self._fold, self._tables)
        if not anchored and not self.name_only:
            steps = ((_PREFIX, None),) + steps  # ripgrep matches '**/' + glob
        self._steps = steps

    def matches(self, path):
        if self._everything:
            return True
        data = _utf8(path)
        if self.name_only:
            data = data[data.rfind(b'/') + 1:]
        folded = data.lower() if self._fold else data
        if self._suffixes and not folded.endswith(self._suffixes):
            return False
        if self._suffix_decides:
            return True
        if self._prefixes and not folded.startswith(self._prefixes):
            return False
        final = _run(self._steps, 1, _Offsets(data, folded, self._tables))
        return bool(final >> len(data) & 1)

    def grep_includes(self, argv_budget):
        """GNU grep ``--include=GLOB`` globs whose union selects exactly the
        files this glob selects, or None when no such list exists or its
        arguments would exceed *argv_budget* bytes.

        ``--include`` matches file names with case-sensitive fnmatch(3) and
        no alternation, so each brace alternative becomes its own glob and a
        case-insensitive glob spells letters as ``[xX]`` classes.
        """
        if not self.name_only:
            return None
        extent = _spelling_extent(self._tokens, self._fold)
        if extent is None:
            return None
        count, size = extent
        if size + count * len('--include=\0') > argv_budget:
            return None
        return _fnmatch_texts(self._tokens, self._fold)


def compile_glob(glob, *, case_sensitive=False):
    """Parse *glob*; raise ``GlobPatternError`` for a refused spelling."""
    if not glob:
        raise GlobPatternError("empty glob: use '*' to select every file")
    try:
        _utf8(glob)
    except UnicodeEncodeError:
        raise GlobPatternError('glob is not valid UTF-8 text') from None
    if glob[0] == '!':
        raise GlobPatternError(
            "a leading '!' would make ripgrep exclude these files instead of "
            "selecting them: name the files to select, or match a literal "
            "'!' with '\\!'")
    if glob[0] == '#':
        raise GlobPatternError(
            "a leading '#' makes ripgrep read the glob as a comment and "
            "select every file: match a literal '#' with '\\#'")
    if glob[-1].isspace() and not glob.endswith('\\ '):
        raise GlobPatternError(
            "trailing whitespace is dropped by ripgrep but kept by fd: "
            "remove it, or match a trailing space with '\\ '")
    if glob[-1] == '/':
        raise GlobPatternError(
            "a trailing '/' selects directories only, never files: use "
            "'dir/**' for the files below a directory")
    anchored = '/' in glob
    text = glob[1:] if glob[0] == '/' else glob
    return ProjectGlob(glob, _parse(text), anchored, case_sensitive)


__all__ = ['GlobPatternError', 'ProjectGlob', 'compile_glob']
