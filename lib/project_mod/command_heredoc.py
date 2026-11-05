"""Pure source views of literal runtime stdin; never rewrite executed source.

command_admission_view(source) is all-or-original writer analysis;
command_stdin_intent_view(source) separates data and executable stdin contexts.
Complete quoted Python/node/cat heredocs are opaque runtime or file-content
data, not readonly proof, authorization or a sandbox. This owner performs no
filesystem, subprocess or undo work.
"""
import os
import re
import shlex

from .command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE, _mask_quoted_literals, _split_pipeline_spans,
    _unwrap_command_parts, command_segment_output_targets,
)

_LITERAL_STDIN = re.compile(
    r'''(?:0)?<<[ \t]*(?:'(?P<single>[^'\r\n]*)'|"(?P<double>[^"\\$`\r\n]*)")'''
    r'''(?=[ \t;|&\n]|$)''')
_RUNTIME = re.compile(r'python(?:[0-9]+(?:\.[0-9]+)*)?|node')
_STATIC_STDIN = re.compile(r'(?:0)?<<[ \t]*(?P<plain>[A-Za-z_][A-Za-z0-9_]*)'
                           r'(?=[ \t;|&\n]|$)')
_SHELLS = frozenset({'sh', 'bash', 'dash', 'zsh', 'ksh'})
# A plain or special parameter expands to words only: it executes nothing and
# its value is never re-parsed for redirect operators. `$(`, `${`, `$((` and
# `$'` start constructs that may run code or hide a `<`.
_PLAIN_PARAMETER = re.compile(r'[A-Za-z0-9_@*#?$!-]')


def shell_input_source(words):
    """Identify explicit shell stdin, command-string or script argv sources.

    Callers remove shell redirects before passing argv. A long option such as
    --norc is not a short -c cluster; option values are not script paths. Unknown
    shell options cannot prove stdin inert. This is intent, never admission.
    """
    words = _unwrap_command_parts(list(words))
    if not words or os.path.basename(words[0]) not in _SHELLS:
        return None
    # `-c` selects command-string mode; the string is the first operand after
    # every option (`sh -c -e 'cmd'`, `sh -c -- 'cmd'`), not the next word.
    index, force_stdin, command = 1, False, False
    while index < len(words):
        word = words[index]
        if word in {'--', '-'}:
            index += 1
            break
        if word in {'-o', '+o', '-O', '+O', '--rcfile', '--init-file'}:
            index += 2
            continue
        if word.startswith('--'):
            index += 1
            continue
        if word.startswith(('-', '+')):
            if word.startswith('-') and 'c' in word[1:]:
                command = True
            elif word.startswith('-') and 's' in word[1:]:
                force_stdin = True
            index += 1
            continue
        break  # options after a script/positional operand are not shell flags
    if command:
        return 'command', words[index] if index < len(words) else None
    if force_stdin or index >= len(words) or words[index] in {'-', '/dev/stdin'}:
        return 'stdin', None
    return 'script', None


def _shell_reads_stdin(words):
    return shell_input_source(words) == ('stdin', None)

_DESCRIPTOR_REDIRECT = re.compile(r'(?<![\w<>])[0-9]*[<>]&[0-9]+(?=\s|$)')


def _without_descriptor_duplications(source):
    """Existing admission rule: unquoted numeric FD aliases open no file."""
    shape = _mask_quoted_literals(source)
    for redirect in reversed(tuple(_DESCRIPTOR_REDIRECT.finditer(shape))):
        source = (source[:redirect.start()]
                  + ' ' * (redirect.end() - redirect.start())
                  + source[redirect.end():])
    return source


def _header_input_operators(header):
    """Unquoted '<' offsets, or None for unsupported/ambiguous shell source."""
    single = double = False
    offsets = []
    index = 0
    while index < len(header):
        char = header[index]
        if char == '\\' and not single:
            if index + 1 == len(header) or header[index + 1] == '\n':
                return None  # physical-line continuation needs a real shell parser
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and char == '$' and _PLAIN_PARAMETER.match(header, index + 1):
            pass
        elif not single and char in '$`':
            return None  # substitutions also execute inside double quotes
        elif not single and not double:
            if char in '()[{}]#\r':
                return None
            if char == '<':
                offsets.append(index)
        index += 1
    return None if single or double else offsets


# Wrappers that hand their stdin to the wrapped program unchanged, so
# ``timeout 60 python3 - <<'PY'`` feeds the body to python3 exactly like the
# bare form. An unknown wrapper option keeps the wrapper, and the heredoc stays
# opaque. Treating the wrapped body as shell source fenced the whole checkout.
_DURATION = re.compile(r'[0-9]+(?:\.[0-9]+)?[smhd]?\Z')
_NICE_ADJUSTMENT = re.compile(r'-n?-?[0-9]+\Z|--adjustment=-?[0-9]+\Z')


def _stdin_program_words(words):
    words = list(words)
    for _ in range(4):
        if not words:
            return words
        name, index = os.path.basename(words[0]), 1
        if name == 'timeout':
            while index < len(words) and words[index].startswith('-'):
                option = words[index]
                if option in ('-s', '--signal', '-k', '--kill-after'):
                    index += 2
                elif (option in ('--preserve-status', '--foreground', '-v', '--verbose')
                      or option.startswith(('--signal=', '--kill-after=', '-s', '-k'))):
                    index += 1
                else:
                    return words
            if index >= len(words) or not _DURATION.match(words[index]):
                return words
            index += 1
        elif name == 'nice':
            if index < len(words) and words[index] in ('-n', '--adjustment'):
                index += 2
            elif index < len(words) and _NICE_ADJUSTMENT.match(words[index]):
                index += 1
        elif name == 'stdbuf':
            while index < len(words) and words[index].startswith('-'):
                index += 2 if words[index] in ('-i', '-o', '-e', '--input', '--output', '--error') else 1
        elif name == 'env':
            while index < len(words) and (words[index].startswith('-')
                                          or _ENV_ASSIGNMENT_GUARD_RE.match(words[index])):
                if words[index] in ('-S', '--split-string'):
                    return words
                index += 2 if words[index] in ('-u', '--unset', '-C', '--chdir') else 1
        elif name == 'time':
            while index < len(words) and words[index] == '-p':
                index += 1
            if index < len(words) and words[index].startswith('-'):
                return words  # GNU time -o/-a write a report file
        elif name != 'nohup':
            return words
        words = words[index:]
    return words


def _runtime_header_view(header, operators, descriptor_view, *, intent=False):
    """Accept one literal stdin body; output redirects remain in the view."""
    if len(operators) != 2 or operators[1] != operators[0] + 1:
        return None  # multiple heredocs, here-strings and other input redirects
    start = operators[0]
    if start and header[start - 1] == '0':
        start -= 1
    if start and not header[start - 1].isspace():
        return None  # unsupported descriptors/attached words
    marker = _LITERAL_STDIN.match(header, start)
    expanded = marker is None
    if marker is None and intent:
        marker = _STATIC_STDIN.match(header, start)
    if marker is None:
        return None  # unknown, <<-, concatenated or escaped delimiters
    delimiter = (marker.group('plain') if expanded else
                 marker.group('single') if marker.group('single') is not None
                 else marker.group('double'))
    if not delimiter:
        return None
    # Marker syntax must be checked BEFORE descriptor masking: <<'PY'2>&1
    # concatenates delimiter words in the real shell, not a full quoted PY.
    view = descriptor_view[:start] + ' ' * (marker.end() - start) + descriptor_view[marker.end():]
    span = next(((begin, end) for begin, end in _split_pipeline_spans(descriptor_view)
                 if begin <= start < end), None)
    if span is None:
        return None
    try:
        words = (list(command_segment_output_targets(view[span[0]:span[1]]).words)
                 if intent else shlex.split(view[span[0]:span[1]], comments=False, posix=True))
    except ValueError:
        return None
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words = words[1:]
    words = _stdin_program_words(words)
    if not words:
        return None
    shell_body = intent and _shell_reads_stdin(words)
    # Git reads stdin only as data (a message, patch, object or path list) and
    # never runs it as shell source, so its quoted body is inert like cat's.
    if not (shell_body or _RUNTIME.fullmatch(os.path.basename(words[0]))
            or os.path.basename(words[0]) in ('cat', 'git')):
        return None  # unknown interpreters/wrappers stay opaque
    if intent and os.path.basename(words[0]) == 'cat':
        tail = view[span[1]:].lstrip()
        if tail.startswith('|') and not tail.startswith('||'):
            next_span = next(iter(_split_pipeline_spans(tail[1:])), None)
            if next_span is not None:
                downstream = tail[1:][next_span[0]:next_span[1]]
                shell_body = _shell_reads_stdin(
                    command_segment_output_targets(downstream).words)
    return view, delimiter, expanded, shell_body


def _literal_stdin_view(source, *, intent):
    """Shared grammar owner; an unknown suffix never proves another body."""
    children = []
    if not source or '<<' not in source:
        return source, children
    output = []
    start = 0

    def unresolved():
        return (''.join(output) + source[start:], children) if intent else (source, [])

    while start < len(source):
        newline = source.find('\n', start)
        end = len(source) if newline < 0 else newline + 1
        header = source[start:end]
        if '<<' not in header:
            if '`' in header:
                return unresolved()  # legacy substitutions may span physical lines
            from .command_setup import _scan
            try:
                spans = _scan(header)  # cross-line quotes/continuations change << grammar
                if ''.join(raw + separator for raw, separator in spans) != header:
                    return unresolved()  # the physical newline is not a command boundary
            except ValueError:
                return unresolved()
            output.append(header)
            start = end
            continue  # complete unrelated shell syntax stays visible to admission
        descriptor_view = _without_descriptor_duplications(header)
        operators = _header_input_operators(descriptor_view)
        if operators is None:
            return unresolved()
        if not operators:
            output.append(header)
            start = end
            continue
        parsed = _runtime_header_view(header, operators, descriptor_view, intent=intent)
        if parsed is None or newline < 0:
            return unresolved()
        view, marker, expanded, shell_body = parsed
        body_end = end
        while body_end < len(source):
            closing_newline = source.find('\n', body_end)
            closing_end = len(source) if closing_newline < 0 else closing_newline + 1
            line = source[body_end:closing_end]
            if line.rstrip('\n') == marker:
                if intent and (expanded or shell_body):
                    body = source[end:body_end]
                    if expanded:
                        children.append((body, True))  # heredoc expansion: quotes are data
                    if shell_body:
                        children.append((body, False))  # explicit interpreter stdin
                output.extend((view, ' ' * (closing_end - end)))
                start = closing_end
                break
            body_end = closing_end
        else:
            return unresolved()  # missing exact delimiter (including trailing whitespace)
    return ''.join(output), children


def command_admission_view(source):
    """Hide complete literal runtime stdin only for all-or-original admission.

    Preserve source offsets and the header newline; blank body/delimiter bytes
    including their newlines so payload quotes/operators never reach a shell
    splitter. Commands before/after the heredoc and header chains stay visible.
    One unsupported header returns the entire original source. Callers retain
    original execution/approval/display/input observations; no readonly or
    lifetime read claim is granted.
    """
    return _literal_stdin_view(source, intent=False)[0]


def command_stdin_intent_view(source):
    """Return source view and separate executable/expanded stdin contexts.

    Intent analysis retains proven prefixes before an unknown suffix, never
    grants writer exemption, and preserves original execution/authority bytes.
    Static unquoted heredoc bodies require expansion rules (quotes are data);
    direct shell stdin and cat directly piped to a shell remain executable.
    """
    return _literal_stdin_view(source, intent=True)
