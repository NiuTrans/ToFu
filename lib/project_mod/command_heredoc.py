"""Admission-only view of literal runtime stdin; never rewrite executed source.

Entry point: command_admission_view(source). Complete quoted Python/node/cat
heredocs are opaque runtime or file-content data under the existing scheduling
policy, not a readonly proof or sandbox. Unknown shell grammar returns the original source.
This owner is pure: no filesystem, subprocess, authorization or undo work.
"""
import os
import re
import shlex

from .command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE, _mask_quoted_literals, _split_pipeline_spans,
)

_LITERAL_STDIN = re.compile(
    r'''(?:0)?<<[ \t]*(?:'(?P<single>[^'\r\n]*)'|"(?P<double>[^"\\$`\r\n]*)")'''
    r'''(?=[ \t;|&\n]|$)''')
_RUNTIME = re.compile(r'python(?:[0-9]+(?:\.[0-9]+)*)?|node')

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
        elif not single and char in '$`':
            return None  # substitutions also execute inside double quotes
        elif not single and not double:
            if char in '()[{}]#\r':
                return None
            if char == '<':
                offsets.append(index)
        index += 1
    return None if single or double else offsets


def _runtime_header_view(header, operators, descriptor_view):
    """Accept one literal stdin body; output redirects remain in the view."""
    if len(operators) != 2 or operators[1] != operators[0] + 1:
        return None  # multiple heredocs, here-strings and other input redirects
    start = operators[0]
    if start and header[start - 1] == '0':
        start -= 1
    if start and not header[start - 1].isspace():
        return None  # unsupported descriptors/attached words
    marker = _LITERAL_STDIN.match(header, start)
    if marker is None:
        return None  # unquoted/expanded, <<-, concatenated or escaped delimiters
    delimiter = (marker.group('single') if marker.group('single') is not None
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
        words = shlex.split(view[span[0]:span[1]], comments=False, posix=True)
    except ValueError:
        return None
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words = words[1:]
    if not words or not (_RUNTIME.fullmatch(os.path.basename(words[0]))
                         or os.path.basename(words[0]) == 'cat'):
        return None  # shells/wrappers can execute their stdin as shell source
    return view, delimiter


def command_admission_view(source):
    """Hide literal runtime bodies/inputredir only in writer admission.

    Preserve source offsets and the header newline; blank body/delimiter bytes
    including their newlines so payload quotes/operators never reach a shell
    splitter. Commands before/after the heredoc, including a chained command on
    its header, remain visible. One unsupported header fails closed for the
    entire view. Callers must retain original execution/approval/display/input
    observation source; this function grants no readonly or lifetime read claim.
    """
    if not source or '<<' not in source:
        return source
    output = []
    start = 0
    while start < len(source):
        newline = source.find('\n', start)
        end = len(source) if newline < 0 else newline + 1
        header = source[start:end]
        if '<<' not in header:
            if '`' in header:
                return source  # legacy substitutions may span physical lines
            from .command_setup import _scan
            try:
                _scan(header)  # cross-line quotes/continuations can change << grammar
            except ValueError:
                return source
            output.append(header)
            start = end
            continue  # complete unrelated shell syntax stays visible to admission
        descriptor_view = _without_descriptor_duplications(header)
        operators = _header_input_operators(descriptor_view)
        if operators is None:
            return source
        if not operators:
            output.append(header)
            start = end
            continue
        parsed = _runtime_header_view(header, operators, descriptor_view)
        if parsed is None or newline < 0:
            return source
        view, marker = parsed
        body_end = end
        while body_end < len(source):
            closing_newline = source.find('\n', body_end)
            closing_end = len(source) if closing_newline < 0 else closing_newline + 1
            line = source[body_end:closing_end]
            if line.rstrip('\n') == marker:
                output.extend((view, ' ' * (closing_end - end)))
                start = closing_end
                break
            body_end = closing_end
        else:
            return source  # missing exact delimiter (including trailing whitespace)
    return ''.join(output)
