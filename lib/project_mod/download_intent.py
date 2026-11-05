"""Recognize cookie-authenticated shell file downloads before subprocess spawn.

This module is deliberately pure and never sees browser or task authority.
Only a narrow, standalone ``curl``/``wget`` shape can be redirected. Wrapped
or dynamic downloads are blocked, using the shared shell word/segment owners
instead of combining unrelated commands or literal program data. A cookie jar
that the same command's earlier cookie-free downloader wrote is that command's
own session, not browser cookie material. This is an intent recognizer, not
authorization or a sandbox for arbitrary programs.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex


@dataclass(frozen=True)
class AuthenticatedDownloadIntent:
    downloader: str
    url: str
    requested_output: str
    block_reason: str = ''

    @property
    def redirectable(self) -> bool:
        return bool(self.url) and not self.block_reason


def _has_shell_control_syntax(source: str) -> bool:
    """Return whether one command needs shell expansion/control semantics."""
    in_single = False
    in_double = False
    index = 0
    while index < len(source):
        char = source[index]
        if char == '\\' and not in_single and index + 1 < len(source):
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char in '\n\r':
            return True
        elif not in_single and char in '$`':
            return True
        elif not in_single and not in_double and char in ';&|<>(){}':
            return True
        elif not in_single and not in_double and char == '#' and (
                index == 0 or source[index - 1].isspace()
                or source[index - 1] in ';&|()'):
            return True
        index += 1
    return in_single or in_double


_NOT_A_FILE = frozenset({'', '-', '/dev/null'})  # unnamed, stdout or discarded


def _next(tokens: list[str], index: int) -> tuple[str, int]:
    if index + 1 >= len(tokens):
        return '', index
    return tokens[index + 1], index + 1


@dataclass(frozen=True)
class _TransferArgs:
    """Facts from one downloader argv; cookie values never leave the scan."""

    downloader: str
    cookie_data: bool  # inline cookie values or a Cookie: header
    cookie_files: tuple[str, ...]  # loaded before the transfer
    cookie_jars: tuple[str, ...]  # written by the downloader as it exits
    requested_output: str  # '' when the response is shown or discarded
    urls: tuple[str, ...]
    non_get: bool

    def replays_cookies(
            self, session_jars: set[str] | frozenset[str] = frozenset()) -> bool:
        """Whether it sends cookies other than its command's own session jars."""
        return self.cookie_data or any(
            path not in session_jars for path in self.cookie_files)


def _curl_args(tokens: list[str], *, shell_output_redirect: bool) -> _TransferArgs:
    cookie_values: list[str] = []
    cookie_jars: list[str] = []
    cookie_header = False
    requested_output = ''
    urls: list[str] = []
    non_get = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if token == '-b' or lower == '--cookie':
            value, index = _next(tokens, index)
            cookie_values.append(value)
        elif lower.startswith('--cookie='):
            cookie_values.append(token.split('=', 1)[1])
        elif token.startswith('-b') and token != '-b':
            cookie_values.append(token[2:])
        elif token == '-c' or lower == '--cookie-jar':
            value, index = _next(tokens, index)
            cookie_jars.append(value)
        elif lower.startswith('--cookie-jar='):
            cookie_jars.append(token.split('=', 1)[1])
        elif token.startswith('-c') and token != '-c':
            cookie_jars.append(token[2:])
        elif token == '-H' or lower == '--header':
            value, index = _next(tokens, index)
            cookie_header = cookie_header or value.lstrip().lower().startswith('cookie:')
        elif lower.startswith('--header='):
            cookie_header = cookie_header or lower.split('=', 1)[1].lstrip().startswith('cookie:')
        elif token.startswith('-H') and token != '-H':
            cookie_header = cookie_header or token[2:].lstrip().lower().startswith('cookie:')
        elif token == '-o' or lower == '--output':
            value, index = _next(tokens, index)
            if value not in _NOT_A_FILE:
                requested_output = value
        elif lower.startswith('--output='):
            value = token.split('=', 1)[1]
            if value not in _NOT_A_FILE:
                requested_output = value
        elif token.startswith('-o') and token != '-o':
            value = token[2:]
            if value not in _NOT_A_FILE:
                requested_output = value
        elif token in {'-O', '-J'} or lower in {
                '--remote-name', '--remote-header-name'}:
            requested_output = requested_output or '[remote filename]'
        elif token in {'-d', '-F', '-T'} or lower in {
            '--data', '--data-ascii', '--data-binary', '--data-raw',
            '--form', '--upload-file',
        } or lower.startswith((
            '--data=', '--data-ascii=', '--data-binary=', '--data-raw=',
            '--form=', '--upload-file=',
        )):
            non_get = True
        elif token == '-X' or lower == '--request':
            method, index = _next(tokens, index)
            non_get = method.upper() not in {'', 'GET', 'HEAD'}
        elif lower.startswith('--request='):
            non_get = lower.split('=', 1)[1].upper() not in {'GET', 'HEAD'}
        elif lower.startswith(('http://', 'https://')):
            urls.append(token)
        index += 1

    if shell_output_redirect:
        requested_output = requested_output or '[shell redirection]'
    # A -b value without "=" names a cookie file; "" only enables the engine.
    return _TransferArgs(
        downloader='curl',
        cookie_data=cookie_header or any('=' in value for value in cookie_values),
        cookie_files=tuple(
            value for value in cookie_values if value and '=' not in value),
        cookie_jars=tuple(cookie_jars),
        requested_output=requested_output,
        urls=tuple(urls),
        non_get=non_get,
    )


def _wget_args(tokens: list[str]) -> _TransferArgs:
    cookie_files: list[str] = []
    cookie_jars: list[str] = []
    cookie_header = False
    requested_output = '[remote filename]'
    urls: list[str] = []
    non_get = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower == '--load-cookies':
            value, index = _next(tokens, index)
            cookie_files.append(value)
        elif lower.startswith('--load-cookies='):
            cookie_files.append(token.split('=', 1)[1])
        elif lower == '--save-cookies':
            value, index = _next(tokens, index)
            cookie_jars.append(value)
        elif lower.startswith('--save-cookies='):
            cookie_jars.append(token.split('=', 1)[1])
        elif lower == '--header':
            value, index = _next(tokens, index)
            cookie_header = cookie_header or value.lstrip().lower().startswith('cookie:')
        elif lower.startswith('--header='):
            cookie_header = cookie_header or lower.split('=', 1)[1].lstrip().startswith('cookie:')
        elif token == '-O' or lower == '--output-document':
            value, index = _next(tokens, index)
            requested_output = '' if value in _NOT_A_FILE else value
        elif lower.startswith('--output-document='):
            value = token.split('=', 1)[1]
            requested_output = '' if value in _NOT_A_FILE else value
        elif lower == '--post-data' or lower.startswith('--post-data='):
            non_get = True
        elif lower.startswith(('http://', 'https://')):
            urls.append(token)
        index += 1

    return _TransferArgs(
        downloader='wget',
        cookie_data=cookie_header,
        cookie_files=tuple(path for path in cookie_files if path),
        cookie_jars=tuple(cookie_jars),
        requested_output=requested_output,
        urls=tuple(urls),
        non_get=non_get,
    )


def _download_intent(
    args: _TransferArgs,
    *,
    dynamic: bool,
    session_jars: set[str] | frozenset[str] = frozenset(),
) -> AuthenticatedDownloadIntent | None:
    if (args.non_get or not args.requested_output
            or not args.replays_cookies(session_jars)):
        return None
    if dynamic:
        return AuthenticatedDownloadIntent(
            downloader=args.downloader, url='',
            requested_output=args.requested_output,
            block_reason=(
                'cookie-authenticated file download uses shell expansion, '
                'redirection, a pipeline, or multiple commands'),
        )
    if len(args.urls) != 1:
        return AuthenticatedDownloadIntent(
            downloader=args.downloader, url='',
            requested_output=args.requested_output,
            block_reason='cookie-authenticated file download must contain exactly one URL',
        )
    return AuthenticatedDownloadIntent(
        downloader=args.downloader, url=args.urls[0],
        requested_output=args.requested_output)


def _malformed_download_intent(
    source: str,
    downloader: str,
) -> AuthenticatedDownloadIntent | None:
    """Fail closed only after a downloader executable head has been identified.

    Incomplete quoting cannot be executed successfully, but retain the legacy
    safe refusal. Never search another command or a quoted program for a head.
    Short options remain case-sensitive: grep's ``-B2`` is not curl's ``-b``.
    """
    has_cookie = bool(re.search(
        r'(?:^|\s)(?:-b(?=\s|[^A-Za-z]|$)|--cookie(?:=|\s)|'
        r'--load-cookies(?:=|\s))|(?i:cookie\s*:)', source))
    wants_file = downloader == 'wget' or bool(re.search(
        r'(?:^|\s)(?:-o(?=\s|[^A-Za-z]|$)|-O(?=\s|$)|'
        r'--output(?:=|\s)|--remote-name(?=\s|$))|>', source))
    if not has_cookie or not wants_file:
        return None
    return AuthenticatedDownloadIntent(
        downloader=downloader, url='',
        requested_output='[ambiguous shell destination]',
        block_reason='cookie-authenticated file download has malformed quoting')


def _execution_source_view(source: str, *, heredoc_expansion=False):
    """Separate live shell source from data without evaluating or rewriting it.

    Reuse the literal-runtime stdin view and substitution delimiter owner.
    Explicit substitutions are visited separately, including inside double
    quotes; their argv placeholders prevent flags leaking into the parent.
    Comments and single background separators need a lexical view because the
    shared pipeline splitter intentionally does not interpret those forms.
    Unsupported heredocs remain visible; this view grants no permission.
    The returned placeholder spans locate those masks: their words are not
    the text the shell will pass.
    """
    from .command_heredoc import command_stdin_intent_view
    from .command_setup import _drop_continuations, _substitution_end

    if heredoc_expansion:
        children = []  # only substitutions execute; quote bytes are body data
    else:
        source, children = command_stdin_intent_view(source)
        source = _drop_continuations(source)
    view = list(source)
    placeholders: list[tuple[int, int]] = []
    quote, index = '', 0
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'" and index + 1 < len(source):
            if ((not heredoc_expansion and quote != '"')
                    or source[index + 1] in '$`"\\\n'):
                index += 2
                continue
        if not heredoc_expansion and char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif quote != "'" and source.startswith('$(', index):
            try:
                end = _substitution_end(source, index)
            except ValueError:
                break  # malformed source stays with the anchored refusal
            children.append((source[index + 2:end - 1], False))
            view[index:end] = 'x' * (end - index)
            placeholders.append((index, end))
            index = end
            continue
        elif quote != "'" and char == '`':
            end = index + 1
            while end < len(source) and source[end] != '`':
                end += 2 if source[end] == '\\' else 1
            if end >= len(source):
                break
            children.append((source[index + 1:end], False))
            view[index:end + 1] = 'x' * (end + 1 - index)
            placeholders.append((index, end + 1))
            index = end + 1
            continue
        elif not heredoc_expansion and not quote and char == '#' and (
                index == 0 or source[index - 1].isspace()
                or source[index - 1] in ';&|()'):
            newline = source.find('\n', index)
            end = len(source) if newline < 0 else newline
            view[index:end] = ' ' * (end - index)
            index = end
            continue
        elif not heredoc_expansion and not quote and char == '&':
            if source[index:index + 2] == '&&':
                index += 2
                continue
            if (source[index - 1:index] not in {'<', '>'}
                    and source[index + 1:index + 2] != '>'):
                view[index] = ';'
        index += 1
    return ('' if heredoc_expansion else ''.join(view)), children, placeholders


def _invocation_source(source: str) -> str:
    """Find one executable head after understood syntax/program wrappers.

    Preserve raw words while peeling prefixes: quoted keywords/assignments
    are ordinary executable names, not shell syntax. Unknown wrappers stay
    opaque instead of searching their operands for a downloader.
    """
    from .command_analysis import (
        _ENV_ASSIGNMENT_GUARD_RE, _shell_words, _unwrap_command_parts,
        command_segment_wrapper_view,
    )

    keywords = {'do', 'then', 'else', 'elif', 'if', 'while', 'until', '!', '{'}
    while source:
        words = _shell_words(source, redirects=True)
        index = 0
        while index < len(words):
            word = words[index]
            raw = source[word.start:word.end]
            if (raw.isdigit() and index + 1 < len(words)
                    and word.end == words[index + 1].start
                    and words[index + 1].text in {'>', '>>', '<', '<>'}):
                index += 1  # adjacent IO descriptor; skip the redirect below
                continue
            if raw == word.text and raw in {'>', '>>', '<', '<<', '<<<', '<>'}:
                index += 2
            elif _ENV_ASSIGNMENT_GUARD_RE.match(raw) or raw in keywords:
                index += 1
            else:
                break
        if index >= len(words):
            return ''
        source = source[words[index].start:]
        view = command_segment_wrapper_view(source)
        if view != source:
            source = view
            continue
        words = _shell_words(source, redirects=True)
        parts = [word.text for word in words]
        child = _unwrap_command_parts(parts)
        if child != parts and child:
            source = source[words[len(parts) - len(child)].start:]
            continue
        head = os.path.basename(parts[0]).lower()
        if head == 'timeout':
            index = 1
            while index < len(parts) and parts[index].startswith('-'):
                option = parts[index]
                index += 2 if option in {'-s', '--signal', '-k', '--kill-after'} else 1
                if option == '--':
                    break
            index += 1  # timeout duration precedes the child executable
            if index < len(words):
                source = source[words[index].start:]
                continue
        return source
    return ''


def _wrapped_download_intent(source: str) -> AuthenticatedDownloadIntent | None:
    """Inspect each actual invocation independently; every match is blocked.

    The work list avoids Python recursion. Each child is strictly shorter
    executable source (a substitution or an explicit shell -c/eval operand),
    so it is finite and retains no process-global state. Arbitrary Python,
    grep, printf and other utility arguments are never parsed as shell code.

    One fact crosses invocations of a source: a cookie file that an earlier
    downloader wrote as its cookie jar (curl ``-c``, wget ``--save-cookies``)
    while loading no other cookies holds only that command's own session,
    not browser material. A writer that loads other cookies, or whose segment
    holds a substitution placeholder, withdraws the jars it may write. Source
    order states the command's evident intent; like the rest of this
    recognizer it is no execution proof, so control flow, concurrency and cwd
    changes between the two invocations are not modeled.
    """
    from .command_analysis import (
        _shell_words, _split_pipeline_spans, command_segment_output_targets,
    )
    from .command_heredoc import shell_input_source

    pending = [(source, False)]
    while pending:
        source, expansion_only = pending.pop()
        view, children, placeholders = _execution_source_view(
            source, heredoc_expansion=expansion_only)
        pending.extend(children)
        session_jars: set[str] = set()
        for start, end in _split_pipeline_spans(view):
            segment = view[start:end].strip()
            core = _invocation_source(segment)
            if not core:
                continue
            try:
                _shell_words(core, strict=True, redirects=True)
            except ValueError:
                words = _shell_words(core, redirects=True)
                head = os.path.basename(words[0].text).lower() if words else ''
                if head in {'curl', 'wget'}:
                    intent = _malformed_download_intent(core, head)
                    if intent is not None:
                        return intent
                continue
            words = list(command_segment_output_targets(core).words)
            if not words:
                continue
            head = os.path.basename(words[0]).lower()
            args = None
            if head == 'curl':
                args = _curl_args(
                    words,
                    shell_output_redirect=bool(command_segment_output_targets(segment).targets))
            elif head == 'wget':
                args = _wget_args(words)
            elif head in {'sh', 'bash', 'dash', 'zsh', 'ksh'}:
                shell_source = shell_input_source(words)
                if shell_source and shell_source[0] == 'command' and shell_source[1]:
                    pending.append((shell_source[1], False))
            elif head == 'eval' and len(words) > 1:
                pending.append((' '.join(words[1:]), False))
            if args is None:
                continue
            literal = not any(low < end and start < high for low, high in placeholders)
            intent = _download_intent(
                args, dynamic=True, session_jars=session_jars if literal else frozenset())
            if intent is not None:
                return intent
            if not literal and args.cookie_jars:
                session_jars.clear()  # a placeholder may name any earlier jar
            elif args.replays_cookies(session_jars):
                session_jars.difference_update(args.cookie_jars)
            else:
                session_jars.update(
                    jar for jar in args.cookie_jars if jar not in _NOT_A_FILE)
    return None


def parse_authenticated_download_command(
    command: str,
) -> AuthenticatedDownloadIntent | None:
    """Return a narrow cookie-bearing file-download intent, else ``None``.

    Only the original standalone downloader can be redirected. For compound
    commands, cookie/output/method flags must belong to the same downloader
    invocation; unrelated argv, comments and literal runtime stdin are data.
    Uploads, non-GET methods, ordinary API inspection (output to stdout or
    ``/dev/null``) and a compound command's own cookie-jar session stay outside
    this fallback. A positive result never contains cookie or header values.
    """
    source = str(command or '').strip()
    if not source:
        return None
    if not _has_shell_control_syntax(source):
        try:
            tokens = shlex.split(source, posix=True)
        except ValueError:
            tokens = []
        executable = os.path.basename(tokens[0]).lower() if tokens else ''
        if executable == 'curl':
            return _download_intent(
                _curl_args(tokens, shell_output_redirect=False), dynamic=False)
        if executable == 'wget':
            return _download_intent(_wget_args(tokens), dynamic=False)
    return _wrapped_download_intent(source)


__all__ = [
    'AuthenticatedDownloadIntent', 'parse_authenticated_download_command',
]
