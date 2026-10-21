"""Pure, bounded admission view for shell setup, never executed-source rewriting.

Git environment overrides remain visible because they select metadata/index
ownership. Only leading inert scalar assignments and exact literal `command -v` discovery
are understood. A proven scalar executable can be referenced by the immediately
following simple command. Bounded literal loops and fixed-leaf path version
probes expose executable identities without expanding paths or rewriting execution.
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
    'case', 'esac', 'select', 'function', 'time', 'coproc', '!',
})
# A loop value is bound verbatim, so it must stay one plain shell word: no
# whitespace (unquoted use would word-split), no quote/metacharacter that the
# in-place substitution below could not reproduce.
_PLAIN_LOOP_VALUE = re.compile(r'[A-Za-z0-9_./:@%+=~-]+\Z')

_PROBE_LEAF = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.+-]*\Z')
_HOME_PROBE_PREFIX = re.compile(r'^"\$(?:HOME|\{HOME\})(?=/|"/)')
_LOOP_STATEFUL_HEADS = frozenset({
    '.', 'alias', 'unalias', 'builtin', 'command', 'cd', 'pushd', 'popd',
    'declare', 'typeset', 'local', 'export', 'readonly', 'unset', 'read',
    'eval', 'exec', 'source', 'set', 'shift', 'getopts', 'let', 'trap',
    'hash', 'enable', 'shopt', 'setopt', 'unsetopt',
})


def _ordinary_loop_variable(name):
    # Shell-maintained variables (including _) cannot retain the loop binding
    # across predicates/printf. Uppercase/special setup is not a probe variable.
    return (name != '_' and not name.isupper()
            and not name.startswith(('BASH_', 'GIT_', 'ZSH_')))


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

    Only statically known single words qualify. Expansion or word splitting
    cannot be reproduced verbatim. An unmatched POSIX glob remains one literal
    loop value, while matches have filesystem-dependent values/cardinality;
    neither is a concrete list that this pure unroller may guess.
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
            elif not quote and (character in '*?[<>|&;(){}#'
                                or character == '~' and index == 0):
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
    output, quote, index = [], '', 0
    while index < len(token):
        character = token[index]
        if character == '\\' and quote != "'":
            output.append(token[index:index + 2])
            index += 2
            continue
        if character in "'\"" and (not quote or quote == character):
            quote = '' if quote else character
        elif quote != "'" and character == '`':
            return None
        elif quote != "'" and character == '$':
            reference = _REFERENCE.match(token, index)
            if reference is None or (reference.group(1) or reference.group(2)) != variable:
                return None
            output.append(value)
            index = reference.end()
            continue
        output.append(character)
        index += 1
    return ''.join(output) if not quote else None


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


def _probe_path_leaf(raw):
    """Invariant nominal executable, not a resolved path or existence proof.

    Only directory components may glob. A HOME prefix must be double-quoted,
    so even whitespace/metacharacters in the final child HOME remain path data.
    No environment, filesystem or shell is consulted; only the leaf is emitted
    for ordinary per-command classification, never for approval or execution.
    """
    from .command_analysis import _mask_quoted_literals, shell_source_has_expansion
    word = _HOME_PROBE_PREFIX.sub('"/home', raw, count=1)
    if shell_source_has_expansion(word, pathname=False):
        return None
    shape = _mask_quoted_literals(word)
    if (any(character in shape for character in '<>;&|(){}#')
            or shape.startswith('~')):
        return None
    decoded = shlex.split(word, comments=False)
    if len(decoded) != 1 or '/' not in decoded[0]:
        return None
    leaf = decoded[0].rsplit('/', 1)[1]
    return leaf if _PROBE_LEAF.fullmatch(leaf) and leaf not in {'.', '..'} else None


def _probe_output_word(raw, variable):
    """Decode literal output data and double-quoted loop references only."""
    output, quote, index = [], '', 0
    while index < len(raw):
        character = raw[index]
        if character == '\\' and quote != "'":
            output.append(raw[index:index + 2])
            index += 2
            continue
        if character in "'\"" and (not quote or quote == character):
            quote = '' if quote else character
        elif quote != "'" and character == '$':
            reference = _REFERENCE.match(raw, index)
            if (quote != '"' or reference is None
                    or (reference.group(1) or reference.group(2)) != variable):
                return None
            output.append('path')
            index = reference.end()
            continue
        elif quote != "'" and character == '`':
            return None
        elif not quote and character in '<>;&|(){}*?[]~#':
            return None
        output.append(character)
        index += 1
    decoded = shlex.split(''.join(output), comments=False)
    return decoded[0] if not quote and len(decoded) == 1 else None


def _inert_printf_format(value):
    # Only literal POSIX conversions qualify; shell %n/-v variable assignment
    # and unknown dialect-specific directives cannot preserve a loop binding.
    return (not value.startswith('-') and '%' not in re.sub(
        r'%(?:%|[-+ #0]*[0-9]*(?:\.[0-9]+)?[diouxXfFeEgGaAcsb])', '', value))


def _path_probe_view(variable, iterable, body):
    """Complete version-only body; symbolic union, not loop execution/unrolling.

    The optional exact -x guard selects no new identity. Inert output and one
    quoted variable version call cannot rebind the loop variable. Unknown
    grammar/effects decline the entire plan, preserving conservative admission.
    """
    references = {f'"${variable}"', f'"${{{variable}}}"'}
    body = list(body)
    tokens, separator = body[0]
    guard = tokens[1:] if tokens[:1] == ['if'] else tokens
    executable_guard = (len(guard) == 4 and guard[:2] == ['[', '-x']
                        and guard[2] in references and guard[3] == ']')
    if tokens[:1] == ['if']:
        if (not executable_guard or separator not in (';', '\n')
                or len(body) < 3 or body[-1][0] != ['fi']
                or body[1][0][:1] != ['then']):
            return None
        body = [(body[1][0][1:], body[1][1]), *body[2:-1]]
    elif executable_guard and separator == '&&':
        body = body[1:]
    projected, invocations = [], 0
    for tokens, separator in body:
        if not tokens or separator not in (';', '\n'):
            return None
        if len(tokens) == 2 and tokens[0] in references and tokens[1] in {'--version', '-v', '-V'}:
            projected.append((None, tokens[1]))
            invocations += 1
            continue
        if tokens[0] not in {'echo', 'printf'}:
            return None
        arguments = [_probe_output_word(token, variable) for token in tokens[1:]]
        if any(argument is None for argument in arguments):
            return None
        if tokens[0] == 'printf':
            if (not arguments or not _inert_printf_format(arguments[0])
                    or '$' in tokens[1] or '`' in tokens[1]):
                return None  # -v/%n can assign shell variables, not inert output.
        projected.append((shlex.join([tokens[0], *arguments]), None))
    if invocations != 1:
        return None
    words = [word for word, _ in _scan(iterable, words=True) if word.strip()]
    if not words or len(words) > _MAX_LOOP_CANDIDATES:
        return None
    leaves = [_probe_path_leaf(word) for word in words]
    if any(leaf is None for leaf in leaves) or len(leaves) * len(projected) > _MAX_LOOP_COMMANDS:
        return None
    # Keep each actual nominal name: no placeholder runtime/readonly exemption.
    # Version-only argv prevents a fabricated Git path from minting a hardened
    # inspection; the existing writer and detach owners classify every leaf.
    return ''.join((output if output is not None else shlex.join([leaf, flag])) + ';'
                   for leaf in leaves for output, flag in projected)


def _loop_command_preserves_binding(tokens):
    from .command_analysis import command_segment_output_targets
    # The shared redirect parser exposes the real first word, including a
    # redirect-prefixed assignment. Do not propagate through an opaque prefix.
    parsed = command_segment_output_targets(' '.join(tokens))
    words = parsed.words
    if (not parsed.complete or not words or _ASSIGNMENT.fullmatch(words[0])
            or words[0].rsplit('/', 1)[-1] in _LOOP_STATEFUL_HEADS):
        return False
    if words[0].rsplit('/', 1)[-1] == 'printf':
        return len(words) > 1 and _inert_printf_format(words[1])
    return True


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

    if separator == '&':
        return None  # The whole loop detaches too; never erase its closing &.
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
    if not _ordinary_loop_variable(variable):
        return None
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
    # An unmanaged `&` cannot be reproduced by repetition: the unrolled text
    # would describe a foreground command the shell never ran. A body whose
    # chain is left open (`&&`/`||`/`|` before `done`) cannot be repeated either.
    if any(span_separator == '&' for _, span_separator in body):
        return None
    if body[-1][1] not in (';', '\n'):
        return None

    probe = _path_probe_view(variable, iterable or '', body)
    if probe is not None:
        return probe, separator, following
    if any(token in _LOOP_KEYWORDS for tokens, _ in body for token in tokens):
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
            if not _loop_command_preserves_binding(substituted):
                return None  # Never substitute past assignments/stateful shell builtins.
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
        loop_context_proven = True
        previous = ''
        index = 0
        while index < len(segments):
            segment, separator = segments[index]
            index += 1
            if previous not in ('', ';', '\n', '&&') or separator in ('|', '||', '&'):
                bindings.clear()
            tokens = [word for word, _ in _scan(segment, words=True) if word.strip()]
            if tokens and tokens[0] == 'for':
                if not loop_context_proven:
                    return source  # Prior alias/builtin/control setup can change body semantics.
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
                if rest:
                    from .command_analysis import command_segment_output_targets
                    parsed = command_segment_output_targets(' '.join(rest))
                    if (not parsed.complete or not parsed.words
                            or _ASSIGNMENT.fullmatch(parsed.words[0])
                            or parsed.words[0].rsplit('/', 1)[-1]
                            in (_LOOP_STATEFUL_HEADS | _LOOP_KEYWORDS)):
                        loop_context_proven = False
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
