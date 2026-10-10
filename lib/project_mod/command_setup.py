"""Pure, bounded admission view for shell setup, never executed-source rewriting.

Git environment overrides remain visible because they select metadata/index
ownership. Only leading inert scalar assignments and exact literal `command -v` discovery
are understood. A proven scalar executable can be referenced by the immediately
following simple command. Unsupported grammar keeps the original conservative
view; this is cooperative scheduling, not proof that opaque programs are readonly.
"""
import re
import shlex

_ASSIGNMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', re.S)
_REFERENCE = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})')
_EXECUTABLE = re.compile(r'[A-Za-z0-9_./+-]+')
_DISCOVERY = re.compile(r'\$\(\s*command\s+-v\s+([A-Za-z0-9_./+-]+)\s*\)')
_MAX_SOURCE = 16_384
_MAX_BINDINGS = 32


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


def command_setup_view(source):
    """Drop understood setup only; retain arguments, redirects and real writes."""
    if not source or len(source) > _MAX_SOURCE:
        return source
    try:
        segments = _scan(source)
        output, bindings = [], {}
        previous = ''
        for segment, separator in segments:
            if previous not in ('', ';', '\n', '&&') or separator in ('|', '||', '&'):
                bindings.clear()
            tokens = [word for word, _ in _scan(segment, words=True) if word.strip()]
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
