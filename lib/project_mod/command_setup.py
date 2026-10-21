"""Pure, bounded admission view for shell setup, never executed-source rewriting.

Git environment overrides remain visible because they select metadata/index
ownership. Only leading inert scalar assignments and exact literal `command -v` discovery
are understood. A proven scalar executable can be referenced by the immediately
following simple command. A bounded `for NAME in <plain words>` loop is unrolled
under the same bounds so a value-named command word is still inspectable.
Unsupported grammar keeps the original conservative view; this is cooperative
scheduling, not proof that opaque programs are readonly.
"""
import re
import shlex

_ASSIGNMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', re.S)
_REFERENCE = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})')
_EXECUTABLE = re.compile(r'[A-Za-z0-9_./+-]+')
_DISCOVERY = re.compile(r'\$\(\s*command\s+-v\s+([A-Za-z0-9_./+-]+)\s*\)')
_MAX_SOURCE = 16_384
_MAX_BINDINGS = 32
_MAX_LOOP_CANDIDATES = 8
_MAX_LOOP_COMMANDS = 64
_LOOP_HEAD = re.compile(r'\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in(?:\s+(.*))?\Z', re.S)
_LOOP_KEYWORDS = frozenset({
    'for', 'while', 'until', 'do', 'done', 'if', 'then', 'elif', 'else', 'fi',
    'case', 'esac', 'select', 'function', 'time', 'coproc',
})
# A loop value is bound verbatim, so it must stay one plain shell word: no
# whitespace (unquoted use would word-split), no quote/metacharacter that the
# in-place substitution below could not reproduce.
_PLAIN_LOOP_VALUE = re.compile(r'[A-Za-z0-9_./:@%+=~-]+\Z')


def _substitution_end(source, start):
    depth, quote, index = 1, '', start + 2
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif not quote:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    raise ValueError('Unclosed substitution')


def _scan(source, *, words=False):
    """Return raw spans and separators, preserving quote/substitution context."""
    result, start, index, quote = [], 0, 0, ''
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'":
            if index + 1 == len(source) or source[index + 1] in '\r\n':
                raise ValueError('Unsupported continuation')
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif quote != "'" and source.startswith('$(', index):
            index = _substitution_end(source, index)
            continue
        elif not quote:
            separator = ''
            if words and char.isspace():
                separator = char
            elif not words and char in ';\n|&':
                separator = source[index:index + 2] if source[index:index + 2] in ('&&', '||') else char
            elif char in '()\r#':
                raise ValueError('Unsupported shell grouping/comment')
            if separator:
                result.append((source[start:index], separator))
                index += len(separator)
                start = index
                continue
        index += 1
    if quote:
        raise ValueError('Unclosed quote')
    result.append((source[start:], ''))
    return result


def _assignment_value(raw):
    """(inert, symbolic executable or None); unknown expansions never evaluate."""
    value = raw
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    discovery = _DISCOVERY.fullmatch(value)
    if discovery:
        return True, discovery.group(1)
    quote, index = '', 0
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif quote != "'" and char == '$':
            reference = _REFERENCE.match(raw, index)
            if reference is None:
                return False, None
            index = reference.end()
            continue
        elif quote != "'" and char == '`':
            return False, None
        elif not quote and char in '<>;&|(){}#':
            return False, None
        index += 1
    if quote:
        return False, None
    try:
        literal = shlex.split(raw, comments=False)
    except ValueError:
        return False, None
    executable = literal[0] if len(literal) == 1 and _EXECUTABLE.fullmatch(literal[0]) else None
    return True, executable


def _output_argument_view(tokens):
    """Inert echo/printf arguments do not turn a cwd into a write target.

    Only scalar references and a static grep substitution are understood. Real
    redirects, executable substitutions and unresolved command heads stay opaque.
    This is an admission projection, never an executed command transformation.
    """
    from .command_analysis import _mask_quoted_literals
    if not tokens or tokens[0] not in {'echo', 'printf'}:
        return tokens
    if any(char in _mask_quoted_literals(' '.join(tokens)) for char in '<>'):
        return tokens  # never discard a redirect or its dynamically selected target
    output = [tokens[0]]
    for token in tokens[1:]:
        result, quote, index = [], '', 0
        while index < len(token):
            char = token[index]
            if char == '\\' and quote != "'":
                result.append(token[index:index + 2])
                index += 2
                continue
            if char in "'\"" and (not quote or quote == char):
                quote = '' if quote else char
            elif quote != "'" and token.startswith('$(', index):
                end = _substitution_end(token, index)
                body = token[index + 2:end - 1]
                shape = _mask_quoted_literals(body)
                try:
                    argv = shlex.split(body, comments=False)
                except ValueError:
                    return tokens
                if (not argv or argv[0] not in {'grep', '/bin/grep', '/usr/bin/grep'}
                        or any(c in shape for c in '<>;&|()#\n\r')
                        or '$' in body or '`' in body):
                    return tokens
                result.append('0')
                index = end
                continue
            elif quote != "'" and char == '$':
                reference = _REFERENCE.match(token, index)
                if reference is not None:
                    index = reference.end()
                elif index + 1 < len(token) and token[index + 1] in '?0123456789':
                    index += 2
                else:
                    return tokens
                result.append('scalar')
                continue
            elif quote != "'" and char == '`':
                return tokens
            result.append(char)
            index += 1
        output.append(''.join(result))
    return output


def _literal_loop_values(raw):
    """Decoded plain-word iterable of one loop, else None.

    Only statically known single words qualify: a value that could word-split,
    glob or expand would bind differently in ``"$name"`` and bare ``$name``
    uses, and the unrolled command would stop describing the real one.
    """
    words = [word for word, _ in _scan(raw, words=True) if word.strip()]
    if not words or len(words) > _MAX_LOOP_CANDIDATES:
        return None
    values = []
    for word in words:
        quote, index = '', 0
        while index < len(word):
            character = word[index]
            if character == '\\' and quote != "'":
                index += 2
                continue
            if character in "'\"" and (not quote or quote == character):
                quote = '' if quote else character
            elif quote != "'" and character in '$`':
                return None
            elif not quote and character in '*?[<>|&;(){}#':
                return None
            index += 1
        if quote:
            return None
        try:
            decoded = shlex.split(word, comments=False)
        except ValueError:
            return None
        if len(decoded) != 1 or _PLAIN_LOOP_VALUE.fullmatch(decoded[0]) is None:
            return None
        values.append(decoded[0])
    return values


def _substitute_loop_reference(token, variable, value):
    """Replace live ``$variable`` references in one token; None if unprovable.

    Single-quoted text and escaped dollars are literal to the shell and stay
    untouched. Any other reference (``$@``, another name) makes the loop
    opaque rather than silently binding a guessed value.
    """
    output, index = [], 0
    while index < len(token):
        character = token[index]
        if character == '\\':
            output.append(token[index:index + 2])
            index += 2
            continue
        if character == "'":
            end = token.find("'", index + 1)
            if end < 0:
                return None
            output.append(token[index:end + 1])
            index = end + 1
            continue
        if character == '`':
            return None
        if character == '$':
            braced = token.startswith('${' + variable + '}', index)
            if braced or token.startswith('$' + variable, index):
                end = index + len(variable) + (3 if braced else 1)
                following = token[end:end + 1]
                if braced or not (following.isalnum() or following == '_'):
                    output.append(value)
                    index = end
                    continue
            if _REFERENCE.match(token, index) is not None:
                return None
        output.append(character)
        index += 1
    return ''.join(output)


def _loop_extent(segments, start):
    """Join one loop's own spans up to ``done``; None when it is not closed.

    ``_scan`` splits on ``;``/newlines, so a loop is never one span. The joined
    text is the exact source substring: every span plus its separator.
    """
    limit = min(len(segments), start + _MAX_LOOP_COMMANDS + 4)
    for stop in range(start + 2, limit):
        if [word for word, _ in _scan(segments[stop][0], words=True)
                if word.strip()] == ['done']:
            joined = ''.join(span + separator for span, separator in segments[start:stop])
            return joined + segments[stop][0], segments[stop][1], stop + 1
    return None


def _loop_view(segments, start):
    """Unroll ``for V in <plain words>; do <simple commands>; done``.

    Bounded literal repetition, not a shell interpreter: at most
    ``_MAX_LOOP_CANDIDATES`` values and ``_MAX_LOOP_COMMANDS`` unrolled
    commands, one nesting level, simple body commands only. Every body command
    keeps its own separator, so a pipeline or ``&&`` chain inside the body is
    classified exactly as if it had been typed once per value. Anything else
    returns None and the caller keeps the original conservative source, which
    is what makes an unresolved ``"$p"`` command word refuse rather than
    borrow a guessed executable identity.
    """
    extent = _loop_extent(segments, start)
    if extent is None:
        return None
    from .command_heredoc import _without_descriptor_duplications
    segment, separator, following = extent
    if '<<' in segment:
        return None  # Heredoc payload is data, not repeatable command words.
    # `2>&1` is a descriptor alias, not background: blank it before splitting so
    # it cannot look like an unmanaged `&` or split a single command in two.
    parts = _scan(_without_descriptor_duplications(segment))
    if len(parts) < 3:
        return None
    match = _LOOP_HEAD.fullmatch(parts[0][0].strip())
    if match is None:
        return None
    variable, iterable = match.group(1), match.group(2)
    opening = [word for word, _ in _scan(parts[1][0], words=True) if word.strip()]
    if not opening or opening[0] != 'do':
        return None
    if [word for word, _ in _scan(parts[-1][0], words=True) if word.strip()] != ['done']:
        return None
    body = [(opening[1:], parts[1][1])]
    body.extend(([word for word, _ in _scan(span, words=True) if word.strip()], separator)
                for span, separator in parts[2:-1])
    if any(not tokens for tokens, _ in body):
        return None
    if any(token in _LOOP_KEYWORDS for tokens, _ in body for token in tokens):
        return None
    # An unmanaged `&` cannot be reproduced by repetition: the unrolled text
    # would describe a foreground command the shell never ran. A body whose
    # chain is left open (`&&`/`||`/`|` before `done`) cannot be repeated either.
    if any(span_separator == '&' for _, span_separator in body):
        return None
    if body[-1][1] not in (';', '\n'):
        return None
    values = _literal_loop_values(iterable or '')
    if values is None or len(values) * len(body) > _MAX_LOOP_COMMANDS:
        return None
    chunks = []
    for value in values:
        chunks.append(f'{variable}={value};')
        for tokens, span_separator in body:
            substituted = []
            for token in tokens:
                resolved = _substitute_loop_reference(token, variable, value)
                if resolved is None:
                    return None
                substituted.append(resolved)
            # Separators are reproduced verbatim so `2>&1` and `a | b` keep
            # their structure instead of turning into separate commands.
            chunks.append(' '.join(substituted) + span_separator)
    return ''.join(chunks), separator, following


def command_setup_view(source):
    """Drop understood setup only; retain arguments, redirects and real writes."""
    if not source or len(source) > _MAX_SOURCE:
        return source
    try:
        segments = _scan(source)
        output, bindings = [], {}
        previous = ''
        index = 0
        while index < len(segments):
            segment, separator = segments[index]
            index += 1
            if previous not in ('', ';', '\n', '&&') or separator in ('|', '||', '&'):
                bindings.clear()
            tokens = [word for word, _ in _scan(segment, words=True) if word.strip()]
            if tokens and tokens[0] == 'for':
                loop = _loop_view(segments, index - 1)
                if loop is None:
                    return source
                unrolled, separator, index = loop
                # The unrolled text already ends with the body's own `;`; a
                # following `&&`/`||`/`|` replaces that separator, exactly as
                # it followed `done` in the executed command.
                if separator in ('&&', '||', '|'):
                    unrolled = unrolled[:-1] if unrolled.endswith((';', '\n')) else unrolled
                    unrolled += separator
                output.append(unrolled)
                bindings.clear()
                previous = separator
                continue
            exported = bool(tokens and tokens[0] == 'export')
            assignment_tokens = tokens[1:] if exported else tokens
            setup, offset = {}, 0
            for token in assignment_tokens:
                assignment = _ASSIGNMENT.fullmatch(token)
                if assignment is None:
                    break
                name, raw = assignment.groups()
                if name in {'IFS', 'ENV', 'BASH_ENV', 'CDPATH'} or name.startswith('GIT_'):
                    return source
                inert, executable = _assignment_value(raw)
                if not inert:
                    return source
                setup[name] = executable
                offset += 1
            if exported and (not offset or offset != len(assignment_tokens)):
                return source  # flags, bare names and export redirects remain opaque
            if offset and offset == len(assignment_tokens):
                if separator not in ('', ';', '\n', '&&'):
                    return source
                bindings.update(setup)
                if len(bindings) > _MAX_BINDINGS:
                    return source
                output.append(':' + separator)
            else:
                rest = tokens[offset:]
                if rest:
                    head = rest[0]
                    if head.startswith('"') and head.endswith('"'):
                        head = head[1:-1]
                    reference = _REFERENCE.fullmatch(head)
                    if reference:
                        name = reference.group(1) or reference.group(2)
                        executable = bindings.get(name)
                        if executable is None:
                            return source
                        rest[0] = shlex.quote(executable)
                output.append(' '.join(_output_argument_view(rest)) + separator)
                # A shell command may change variables (`read`, functions, etc.).
                # Do not carry a guessed binding past any non-setup command.
                bindings.clear()
            previous = separator
        return ''.join(output)
    except ValueError:
        return source


def command_cwd_setup_refusal(source, *, inherited_cdpath=''):
    """A literal relative cd may not borrow a cwd ignoring command-owned CDPATH."""
    from .command_analysis import (
        _ENV_ASSIGNMENT_GUARD_RE, _mask_quoted_literals, _split_pipeline, _unwrap_command_parts,
    )
    shape = _mask_quoted_literals(source)
    supplied = re.search(r'(?:^|[\s;])CDPATH=', shape)
    if not inherited_cdpath and not supplied:
        return None
    if len(source) > _MAX_SOURCE:
        return ('Error [workspace-cwd-ambiguous]: CDPATH setup exceeds static admission bounds. '
                'Use an absolute working_dir/cd target; command was not started.')
    for segment in _split_pipeline(source):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = _unwrap_command_parts(words)
        if not words or words[0] != 'cd':
            continue
        arguments = words[1:]
        while arguments and arguments[0] in ('-L', '-P', '--'):
            arguments = arguments[1:]
        if arguments and not arguments[0].startswith(('/', './', '../')):
            return ('Error [workspace-cwd-ambiguous]: CDPATH can redirect relative cd outside '
                    'the claimed checkout. Use an absolute or ./-prefixed cd target '
                    'or working_dir; command was not started.')
    return None
