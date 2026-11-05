"""Pure, bounded shell structure for the catastrophic-delete and
dangerous-command guards.

Responsibility: split one shell command into simple commands, subshell groups
and the scripts nested inside them, so the guards
(``command_analysis._catastrophic_delete_detail`` and
``command_analysis.dangerous_command_rule``) judge only words a shell would
run as commands and never data. Entry point:
:func:`parse_shell_structure`. Dependencies: the standard library only; it
executes, reads and writes nothing.

Each construct is delimited the way a POSIX/bash parser delimits it:

* quoting: ``'…'``, ``"…"``, ``$'…'``, ``$"…"``, backslash escapes and
  line continuations;
* comments: ``#`` at the start of a word;
* substitutions ``$(…)``, ``…`` in backticks, ``$((…))``, ``${…}``,
  ``<(…)`` and ``>(…)``: every command or process substitution body is a
  nested script, parsed recursively;
* heredocs (``<<WORD``, ``<<-WORD``, quoted delimiters): the body is captured
  at the newline that ends its command line and is never tokenized as
  commands; an expanding (unquoted-delimiter) body contributes its command
  substitutions as nested scripts;
* here-strings and redirections, which are never command words;
* compound syntax: ``( … )`` groups, ``case … esac`` clauses, ``[[ … ]]``
  tests, ``(( … ))`` arithmetic, ``name()`` function definitions, array
  assignments. Reserved words (``if``, ``then``, ``do``, ``{`` …) stay leading
  words of the next simple command for the caller to strip.

Anything this parser cannot delimit raises :class:`ShellStructureUnknown`;
the caller decides how conservative to be. Whether a heredoc body or a
``-c`` string is executed by a shell is the caller's judgement, not syntax.
"""

import re
from collections import namedtuple

_MAX_DEPTH = 32

# A redirection operator at a word start: optional descriptor number or
# {varname}, then the operator. ``&>``/``&>>`` redirect both streams.
_REDIRECT = re.compile(
    r'(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:<<<|<<-|<<|<>|<&|>&|>>|>\||<|>)|&>>?')
_ARRAY_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=\Z')
_WORD_END = frozenset(' \t\n;&|()<>')
# Reserved words after which a new command may start in the same line.
_LEADING_RESERVED = frozenset({
    '!', '{', '}', 'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while',
    'until', 'time', 'function',
})


class ShellStructureUnknown(ValueError):
    """The source holds a construct this parser cannot delimit."""

    def __init__(self, reason, offset):
        super().__init__(f'{reason} at offset {offset}')
        self.reason = reason
        self.offset = offset


SimpleCommand = namedtuple(
    'SimpleCommand', ['words', 'redirects', 'separator', 'nested', 'heredocs'])
SimpleCommand.__doc__ = """One simple command.

``words``: raw word texts as written (quotes kept, line continuations
removed), redirections excluded. ``redirects``: ``(operator, raw target)``
pairs, here-strings included, heredocs excluded. ``separator``: the control
operator before the command (``''``, ``';'``, ``'\\n'``, ``'&'``, ``'&&'``,
``'||'``, ``'|'``). ``nested``: scripts run while expanding the words and
redirects (command/process substitutions). ``heredocs``: :class:`Heredoc`.
"""

Subshell = namedtuple(
    'Subshell', ['body', 'separator', 'redirects', 'nested', 'heredocs'])
Subshell.__doc__ = """A ``( … )`` group: *body* runs in a child shell."""


class Heredoc:
    """One heredoc: its body is stdin data for the command that owns it."""

    __slots__ = ('delimiter', 'quoted', 'strip_tabs', 'body', 'nested', 'offset')

    def __init__(self, delimiter, quoted, strip_tabs, offset):
        self.delimiter = delimiter
        self.quoted = quoted          # quoted delimiter: the body is literal
        self.strip_tabs = strip_tabs  # <<- strips leading tabs
        self.body = ''
        self.nested = ()              # substitutions an expanding body runs
        self.offset = offset


def parse_shell_structure(source):
    """Return the items (``SimpleCommand``/``Subshell``) of *source*, in order."""
    return _Lexer(source, 0, 0).script()


def shell_command_ranges(source):
    """Return source ranges of simple commands, including nested substitutions.

    Consumers may rewrite a proven command in place without interpreting
    separators, shell state, or heredoc contents a second time. Ranges can
    contain nested ranges; a consumer must not rewrite overlapping commands.
    Backtick bodies retain their separate escaped-source representation and
    are handled by consumers before this source-preserving pass.
    """
    lexer = _Lexer(source, 0, 0)
    lexer.command_ranges = []
    lexer.script()
    return tuple(sorted(lexer.command_ranges))


def _heredoc_delimiter(raw):
    """Dequote a heredoc delimiter word; any quoting makes the body literal."""
    quoted = any(char in raw for char in '\'"\\')
    out = []
    quote = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is None:
            if char in '\'"':
                quote = char
            elif char == '\\' and index + 1 < len(raw):
                index += 1
                out.append(raw[index])
            else:
                out.append(char)
        elif char == quote:
            quote = None
        elif (quote == '"' and char == '\\' and index + 1 < len(raw)
              and raw[index + 1] in '$`"\\'):
            index += 1
            out.append(raw[index])
        else:
            out.append(char)
        index += 1
    return ''.join(out), quoted


class _NotArithmetic(Exception):
    """``$((`` / ``((`` that does not close with ``))``: a nested subshell."""


class _Lexer:
    def __init__(self, source, depth, base):
        if depth > _MAX_DEPTH:
            raise ShellStructureUnknown('nesting too deep', base)
        self.s = source
        self.n = len(source)
        self.i = 0
        self.depth = depth
        self.base = base
        self.pending = []
        self.command_ranges = None

    def _fail(self, reason, offset=None):
        raise ShellStructureUnknown(reason, self.base + (self.i if offset is None else offset))

    # ── lists of commands ─────────────────────────────────────────────

    def script(self, closer=None):
        """Parse commands until EOF, a closing ``)`` or (``closer='case'``)
        the end of a case clause."""
        items = []
        separator = ''
        while True:
            self._blanks()
            if self.i >= self.n:
                if closer is not None:
                    self._fail(f'unclosed {"case" if closer == "case" else "("}')
                self._heredocs_at_eof()
                return tuple(items)
            char = self.s[self.i]
            if char == '\n':
                self._newline()
                if separator not in ('&&', '||', '|'):
                    separator = '\n'
                continue
            if char == '#':
                self._comment()
                continue
            if char == ')':
                if closer == ')':
                    self.i += 1
                    return tuple(items)
                self._fail('unbalanced )')
            if closer == 'case':
                for terminator in (';;&', ';;', ';&'):
                    if self.s.startswith(terminator, self.i):
                        self.i += len(terminator)
                        return tuple(items)
                if self._at_word('esac'):
                    return tuple(items)
            if char == ';':
                if self.s.startswith(';;', self.i):
                    self._fail("';;' outside a case clause")
                self.i += 1
                separator = ';'
                continue
            if char == '&' and not self.s.startswith('&>', self.i):
                if self.s.startswith('&&', self.i):
                    self.i += 2
                    separator = '&&'
                else:
                    self.i += 1
                    separator = '&'
                continue
            if char == '|':
                if self.s.startswith('||', self.i):
                    self.i += 2
                    separator = '||'
                else:
                    self.i += 2 if self.s.startswith('|&', self.i) else 1
                    separator = '|'
                continue
            if char == '(':
                items.append(self._group(separator))
                separator = ''
                continue
            items.extend(self._command(separator))
            separator = ''

    def _group(self, separator):
        if self.s.startswith('((', self.i):
            nested = []
            start = self.i
            text = self._arithmetic(nested, 2)
            if text is not None:
                command = [text]
                redirects, more, heredocs = self._trailing_redirects()
                return SimpleCommand(tuple(command), redirects, separator,
                                     tuple(nested) + more, heredocs)
            self.i = start
        self.i += 1
        body = self._nested_script()
        redirects, nested, heredocs = self._trailing_redirects()
        return Subshell(body, separator, redirects, nested, heredocs)

    def _trailing_redirects(self):
        redirects, nested, heredocs = [], [], []
        while True:
            self._blanks()
            redirect = self._redirect(nested, heredocs)
            if redirect is None:
                return tuple(redirects), tuple(nested), tuple(heredocs)
            if redirect:
                redirects.append(redirect)

    def _command(self, separator):
        start = self.i
        words, redirects, nested, heredocs = [], [], [], []
        dbracket = False
        while True:
            self._blanks(newlines=dbracket)
            if self.i >= self.n:
                break
            char = self.s[self.i]
            # Control operators end the command even inside [[ … ]]: bash has
            # its own test grammar, but a POSIX shell parses [[ as an ordinary
            # command name and runs what follows &&/|| as the next command.
            if char in '\n;|)' or (char == '&' and not self.s.startswith('&>', self.i)):
                break
            if dbracket:
                word = self._word(nested, dbracket=True)
                words.append(word)
                if word == ']]':
                    dbracket = False
                continue
            if char == '#':
                break  # a comment starts at a word start; the caller skips it
            leading = all(word in _LEADING_RESERVED for word in words)
            if char == '(':
                if words and words[-1] == 'for' and self.s.startswith('((', self.i):
                    text = self._arithmetic(nested, 2)
                    if text is None:
                        self._fail('unexpected (')
                    words.append(text)
                    continue
                if leading:
                    break  # `if ( … )`, `! ( … )`: a group starts a new command
                if self._function_parens():
                    words.append('()')
                    break
                self._fail('unexpected (')
            redirect = self._redirect(nested, heredocs)
            if redirect is not None:
                if redirect:
                    redirects.append(redirect)
                continue
            word = self._word(nested)
            words.append(word)
            if leading and word == 'case':
                return self._case(words, redirects, separator, nested, heredocs)
            if leading and word == '[[':
                dbracket = True
        if dbracket:
            self._fail('unclosed [[')
        if self.command_ranges is not None:
            self.command_ranges.append((start, self.i))
        return [SimpleCommand(tuple(words), tuple(redirects), separator,
                              tuple(nested), tuple(heredocs))]

    def _case(self, words, redirects, separator, nested, heredocs):
        self._blanks()
        words.append(self._word(nested))  # the subject
        self._blanks(newlines=True)
        if not self._at_word('in'):
            self._fail('case without in')
        self.i += 2
        words.append('in')
        items = [SimpleCommand(tuple(words), tuple(redirects), separator,
                               tuple(nested), tuple(heredocs))]
        while True:
            self._blanks(newlines=True, comments=True)
            if self.i >= self.n:
                self._fail('unclosed case')
            if self._at_word('esac'):
                self.i += 4
                break
            if self.s[self.i] == '(':
                self.i += 1
            pattern_nested = []
            while True:
                self._blanks(newlines=True)
                if self.i >= self.n:
                    self._fail('unclosed case pattern')
                char = self.s[self.i]
                if char == ')':
                    self.i += 1
                    break
                if char == '|':
                    self.i += 1
                    continue
                if char in '\n;&(<>':
                    self._fail('malformed case pattern')
                self._word(pattern_nested)
            if pattern_nested:
                items.append(SimpleCommand((), (), ';', tuple(pattern_nested), ()))
            items.extend(self.script('case'))
        trailing, more, trailing_heredocs = self._trailing_redirects()
        if trailing or more or trailing_heredocs:
            items.append(SimpleCommand((), trailing, ';', more, trailing_heredocs))
        return items

    # ── words ─────────────────────────────────────────────────────────

    def _word(self, nested, dbracket=False):
        start = self.i
        out = []
        s = self.s
        while self.i < self.n:
            char = s[self.i]
            if char in ' \t\n':
                break
            if char == ';' or (not dbracket and char in '&|)'):
                break
            if char in '<>' and not dbracket:
                if s.startswith('(', self.i + 1):
                    begin = self.i
                    self.i += 2
                    nested.append(self._nested_script())
                    out.append(s[begin:self.i])
                    continue
                break
            if char == '(' and not dbracket:
                so_far = ''.join(out)
                if _ARRAY_ASSIGNMENT.match(so_far) or so_far[-1:] in ('@', '!', '*', '+', '?'):
                    out.append(self._paren_words(nested))
                    continue
                break
            if char == '\\':
                if self.i + 1 >= self.n:
                    out.append(char)  # a final backslash stays literal
                    self.i += 1
                    continue
                if s[self.i + 1] == '\n':
                    self.i += 2  # line continuation
                    continue
                out.append(s[self.i:self.i + 2])
                self.i += 2
                continue
            if char == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    self._fail('unclosed single quote')
                out.append(s[self.i:end + 1])
                self.i = end + 1
                continue
            if char == '"':
                out.append(self._double_quoted(nested))
                continue
            if char == '$':
                out.append(self._dollar(nested))
                continue
            if char == '`':
                out.append(self._backtick(nested))
                continue
            out.append(char)
            self.i += 1
        if self.i == start:
            self._fail('expected a word')
        return ''.join(out)

    def _paren_words(self, nested):
        """``name=( … )`` / extglob ``@( … )``: words up to the matching ``)``."""
        begin = self.i
        self.i += 1
        while True:
            self._blanks(newlines=True, comments=True)
            if self.i >= self.n:
                self._fail('unclosed (', begin)
            char = self.s[self.i]
            if char == ')':
                self.i += 1
                return self.s[begin:self.i]
            if char == '|':
                self.i += 1
                continue
            if char == '(':
                self._paren_words(nested)
                continue
            if char in ';&<>':
                self._fail('unexpected operator in ( … )')
            self._word(nested)

    def _double_quoted(self, nested):
        begin = self.i
        self.i += 1
        out = ['"']
        while self.i < self.n:
            char = self.s[self.i]
            if char == '"':
                self.i += 1
                out.append('"')
                return ''.join(out)
            if char == '\\':
                if self.s.startswith('\n', self.i + 1):
                    self.i += 2
                    continue
                out.append(self.s[self.i:self.i + 2])
                self.i += 2
                continue
            if char == '$':
                out.append(self._dollar(nested, in_double=True))
                continue
            if char == '`':
                out.append(self._backtick(nested, in_double=True))
                continue
            out.append(char)
            self.i += 1
        self._fail('unclosed double quote', begin)

    def _dollar(self, nested, in_double=False):
        s = self.s
        begin = self.i
        if s.startswith('$((', begin):
            text = self._arithmetic(nested, 3)
            if text is not None:
                return text
            self.i = begin  # `$( (…) …)`: a substitution opening with a subshell
        if s.startswith('$(', begin):
            self.i += 2
            nested.append(self._nested_script())
            return s[begin:self.i]
        if s.startswith('${', begin):
            return self._parameter(nested, in_double)
        if s.startswith("$'", begin) and not in_double:
            index = begin + 2
            while index < self.n:
                if s[index] == '\\':
                    index += 2
                    continue
                if s[index] == "'":
                    self.i = index + 1
                    return s[begin:self.i]
                index += 1
            self._fail("unclosed $'", begin)
        if s.startswith('$"', begin) and not in_double:
            self.i += 1
            return '$' + self._double_quoted(nested)
        self.i += 1
        return '$'

    def _parameter(self, nested, in_double):
        begin = self.i
        self.i += 2
        while self.i < self.n:
            char = self.s[self.i]
            if char == '}':
                self.i += 1
                return self.s[begin:self.i]
            if char == '\\':
                self.i += 2
                continue
            if char == "'" and not in_double:
                end = self.s.find("'", self.i + 1)
                if end < 0:
                    self._fail('unclosed single quote')
                self.i = end + 1
                continue
            if char == '"':
                self._double_quoted(nested)
                continue
            if char == '$':
                self._dollar(nested, in_double=True)
                continue
            if char == '`':
                self._backtick(nested, in_double)
                continue
            self.i += 1
        self._fail('unclosed ${', begin)

    def _arithmetic(self, nested, opener):
        """Text of ``$((…))``/``((…))`` from ``self.i``, or None (not arithmetic)."""
        begin = self.i
        mark = len(nested)
        pending = len(self.pending)
        self.i += opener
        depth = 0
        try:
            while self.i < self.n:
                char = self.s[self.i]
                if char == '(':
                    depth += 1
                    self.i += 1
                elif char == ')':
                    if depth:
                        depth -= 1
                        self.i += 1
                    elif self.s.startswith('))', self.i):
                        inner_text = self.s[begin + opener:self.i]
                        self.i += 2
                        # bash evaluates the text as arithmetic; a POSIX shell
                        # reads $(( … ))'s twin `( ( … ) )` as nested subshells
                        # and RUNS its words (`(( rm -rf / ))` deletes under
                        # dash). Judge both readings.
                        try:
                            nested.append(_Lexer(inner_text, self.depth + 1,
                                                 self.base + begin + opener).script())
                        except ShellStructureUnknown:
                            pass
                        return self.s[begin:self.i]
                    else:
                        raise _NotArithmetic
                elif char == '$':
                    self._dollar(nested, in_double=True)
                elif char == '`':
                    self._backtick(nested)
                elif char == '"':
                    self._double_quoted(nested)
                elif char == "'":
                    end = self.s.find("'", self.i + 1)
                    if end < 0:
                        raise _NotArithmetic
                    self.i = end + 1
                elif char == '\\':
                    self.i += 2
                else:
                    self.i += 1
            raise _NotArithmetic
        except (_NotArithmetic, ShellStructureUnknown):
            del nested[mark:]
            del self.pending[pending:]
            self.i = begin
            return None

    def _backtick(self, nested, in_double=False):
        begin = self.i
        index = begin + 1
        body = []
        while index < self.n:
            char = self.s[index]
            if char == '\\' and index + 1 < self.n:
                follower = self.s[index + 1]
                if follower in '$`\\' or (in_double and follower == '"'):
                    body.append(follower)
                    index += 2
                    continue
                if follower == '\n':
                    index += 2
                    continue
                body.append(char)
                index += 1
                continue
            if char == '`':
                self.i = index + 1
                inner = _Lexer(''.join(body), self.depth + 1, self.base + begin + 1)
                nested.append(inner.script())
                return self.s[begin:self.i]
            body.append(char)
            index += 1
        self._fail('unclosed backtick', begin)

    def _nested_script(self):
        """Body of ``$(``/``<(``/``>(``/``(`` starting at ``self.i``, through ``)``."""
        if self.depth >= _MAX_DEPTH:
            self._fail('nesting too deep')
        outer = self.pending
        self.pending = []
        self.depth += 1
        try:
            body = self.script(')')
        finally:
            self.depth -= 1
            inner, self.pending = self.pending, outer
        # A heredoc opened inside a substitution that closed on the same line
        # is read at the enclosing line's newline.
        self.pending.extend(inner)
        return body

    # ── redirections and heredocs ────────────────────────────────────

    def _redirect(self, nested, heredocs):
        """Parse a redirection at ``self.i``: the (op, target) pair, ``()``
        for a heredoc, or None when no redirection starts here."""
        match = _REDIRECT.match(self.s, self.i)
        if match is None:
            return None
        operator = match.group(0)
        if operator in ('<', '>') and self.s.startswith('(', match.end()):
            return None  # process substitution word
        begin = self.i
        self.i = match.end()
        self._blanks()
        if self.i >= self.n or self.s[self.i] in '\n;&|)':
            self._fail('redirection without a target')
        bare = operator.lstrip('0123456789')
        if bare.startswith('{'):
            bare = bare[bare.index('}') + 1:]
        if bare in ('<<', '<<-'):
            delimiter, quoted = _heredoc_delimiter(self._word(nested))
            if not delimiter:
                self._fail('empty heredoc delimiter')
            heredoc = Heredoc(delimiter, quoted, bare == '<<-', self.base + begin)
            self.pending.append(heredoc)
            heredocs.append(heredoc)
            return ()
        return operator, self._word(nested)

    def _newline(self):
        self.i += 1
        pending, self.pending = self.pending, []
        for heredoc in pending:
            self._heredoc_body(heredoc)

    def _heredoc_body(self, heredoc):
        start = self.i
        while self.i < self.n:
            end = self.s.find('\n', self.i)
            line_end = self.n if end < 0 else end
            line = self.s[self.i:line_end]
            if (line.lstrip('\t') if heredoc.strip_tabs else line) == heredoc.delimiter:
                heredoc.body = self.s[start:self.i]
                self.i = self.n if end < 0 else end + 1
                break
            self.i = self.n if end < 0 else end + 1
        else:
            heredoc.body = self.s[start:]  # delimited by end of input
        if not heredoc.quoted and heredoc.body:
            heredoc.nested = _Lexer(heredoc.body, self.depth + 1,
                                    self.base + start).expanding_body()

    def _heredocs_at_eof(self):
        self.pending = []  # no newline follows: the bodies are empty

    def expanding_body(self):
        """Scripts run by expanding a heredoc body: quotes are literal there."""
        nested = []
        while self.i < self.n:
            char = self.s[self.i]
            if char == '\\':
                self.i += 2
            elif char == '$':
                self._dollar(nested, in_double=True)
            elif char == '`':
                self._backtick(nested)
            else:
                self.i += 1
        return tuple(nested)

    # ── lexical helpers ───────────────────────────────────────────────

    def _blanks(self, newlines=False, comments=False):
        while self.i < self.n:
            char = self.s[self.i]
            if char in ' \t':
                self.i += 1
            elif char == '\\' and self.s.startswith('\n', self.i + 1):
                self.i += 2
            elif newlines and char == '\n':
                self._newline()
            elif comments and char == '#':
                self._comment()
            else:
                return

    def _comment(self):
        end = self.s.find('\n', self.i)
        self.i = self.n if end < 0 else end

    def _at_word(self, word):
        end = self.i + len(word)
        return (self.s.startswith(word, self.i)
                and (end >= self.n or self.s[end] in _WORD_END))

    def _function_parens(self):
        index = self.i + 1
        while index < self.n and self.s[index] in ' \t':
            index += 1
        if index < self.n and self.s[index] == ')':
            self.i = index + 1
            return True
        return False
