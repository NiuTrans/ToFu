"""Recognize cookie-authenticated shell file downloads before subprocess spawn.

This module is deliberately pure: it parses only a narrow, single-command
``curl``/``wget`` shape and never sees browser or task authority.  The task
handler may route a positive match through the canonical server-download
service.  Ambiguous/dynamic cookie-bearing download shapes are marked blocked
so replayable browser secrets never reach a shell by accident.
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
        index += 1
    return in_single or in_double


def _has_shell_output_redirection(source: str) -> bool:
    """Recognize an unquoted ``>`` without interpreting its target."""
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
        elif char == '>' and not in_single and not in_double:
            return True
        index += 1
    return False


def _next(tokens: list[str], index: int) -> tuple[str, int]:
    if index + 1 >= len(tokens):
        return '', index
    return tokens[index + 1], index + 1


def _curl_intent(
    tokens: list[str],
    *,
    dynamic: bool,
    shell_output_redirect: bool,
) -> AuthenticatedDownloadIntent | None:
    has_cookie = False
    wants_file = False
    requested_output = ''
    urls: list[str] = []
    non_get = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if token == '-b' or lower == '--cookie':
            value, index = _next(tokens, index)
            has_cookie = bool(value)
        elif lower.startswith('--cookie=') or (
                token.startswith('-b') and token != '-b'):
            has_cookie = True
        elif token == '-H' or lower == '--header':
            value, index = _next(tokens, index)
            has_cookie = has_cookie or value.lstrip().lower().startswith('cookie:')
        elif lower.startswith('--header='):
            has_cookie = has_cookie or lower.split('=', 1)[1].lstrip().startswith('cookie:')
        elif token.startswith('-H') and token != '-H':
            has_cookie = has_cookie or token[2:].lstrip().lower().startswith('cookie:')
        elif token == '-o' or lower == '--output':
            requested_output, index = _next(tokens, index)
            wants_file = bool(requested_output)
        elif lower.startswith('--output='):
            requested_output = token.split('=', 1)[1]
            wants_file = bool(requested_output)
        elif token.startswith('-o') and token != '-o':
            requested_output = token[2:]
            wants_file = bool(requested_output)
        elif token in {'-O', '-J'} or lower in {
                '--remote-name', '--remote-header-name'}:
            requested_output = requested_output or '[remote filename]'
            wants_file = True
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
        wants_file = True
        requested_output = requested_output or '[shell redirection]'

    if non_get or not has_cookie or not wants_file:
        return None
    if dynamic:
        return AuthenticatedDownloadIntent(
            downloader='curl', url='', requested_output=requested_output,
            block_reason=(
                'cookie-authenticated file download uses shell expansion, '
                'redirection, a pipeline, or multiple commands'),
        )
    if len(urls) != 1:
        return AuthenticatedDownloadIntent(
            downloader='curl', url='', requested_output=requested_output,
            block_reason='cookie-authenticated file download must contain exactly one URL',
        )
    return AuthenticatedDownloadIntent(
        downloader='curl', url=urls[0], requested_output=requested_output)


def _wget_intent(
    tokens: list[str],
    *,
    dynamic: bool,
) -> AuthenticatedDownloadIntent | None:
    has_cookie = False
    requested_output = '[remote filename]'
    urls: list[str] = []
    non_get = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower == '--load-cookies':
            value, index = _next(tokens, index)
            has_cookie = bool(value)
        elif lower.startswith('--load-cookies='):
            has_cookie = True
        elif lower == '--header':
            value, index = _next(tokens, index)
            has_cookie = has_cookie or value.lstrip().lower().startswith('cookie:')
        elif lower.startswith('--header='):
            has_cookie = has_cookie or lower.split('=', 1)[1].lstrip().startswith('cookie:')
        elif token == '-O' or lower == '--output-document':
            requested_output, index = _next(tokens, index)
        elif lower.startswith('--output-document='):
            requested_output = token.split('=', 1)[1]
        elif lower == '--post-data' or lower.startswith('--post-data='):
            non_get = True
        elif lower.startswith(('http://', 'https://')):
            urls.append(token)
        index += 1

    if non_get or not has_cookie or requested_output == '-':
        return None
    if dynamic:
        return AuthenticatedDownloadIntent(
            downloader='wget', url='', requested_output=requested_output,
            block_reason=(
                'cookie-authenticated file download uses shell expansion, '
                'redirection, a pipeline, or multiple commands'),
        )
    if len(urls) != 1:
        return AuthenticatedDownloadIntent(
            downloader='wget', url='', requested_output=requested_output,
            block_reason='cookie-authenticated file download must contain exactly one URL',
        )
    return AuthenticatedDownloadIntent(
        downloader='wget', url=urls[0], requested_output=requested_output)


def _coarse_ambiguous_cookie_download(
    source: str,
    *,
    reason: str,
) -> AuthenticatedDownloadIntent | None:
    """Fail closed when shell wrapping prevents the strict parser from owning it."""
    match = re.search(
        r'(?:^|[\s;&|])(?:[^\s;&|]*/)?(curl|wget)(?=\s)',
        source,
        re.IGNORECASE,
    )
    if not match:
        return None
    downloader = match.group(1).lower()
    has_cookie = bool(re.search(
        r'(?:^|\s)(?:-b(?=\s|[^A-Za-z]|$)|--cookie(?:=|\s)|'
        r'--load-cookies(?:=|\s))|cookie\s*:',
        source,
        re.IGNORECASE,
    ))
    if downloader == 'curl':
        wants_file = bool(re.search(
            r'(?:^|\s)(?:-o(?=\s|[^A-Za-z]|$)|-O(?=\s|$)|'
            r'--output(?:=|\s)|--remote-name(?=\s|$))|>',
            source,
        ))
    else:
        wants_file = not bool(re.search(
            r'(?:^|\s)(?:-O\s+-|--output-document(?:=|\s)-)(?:\s|$)',
            source,
        ))
    if not has_cookie or not wants_file:
        return None
    return AuthenticatedDownloadIntent(
        downloader=downloader,
        url='',
        requested_output='[ambiguous shell destination]',
        block_reason=reason,
    )


def parse_authenticated_download_command(
    command: str,
) -> AuthenticatedDownloadIntent | None:
    """Return a narrow cookie-bearing file-download intent, else ``None``.

    Ordinary API inspection such as ``curl -H 'Cookie: …' URL`` is not
    rewritten because it has no file-output flag.  Uploads and non-GET methods
    are likewise outside this fallback.  A positive result contains no cookie
    or header value and is therefore safe to retain in task metadata.
    """
    source = str(command or '').strip()
    if not source:
        return None
    dynamic = _has_shell_control_syntax(source)
    try:
        tokens = shlex.split(source, posix=True)
    except ValueError:
        return _coarse_ambiguous_cookie_download(
            source, reason='cookie-authenticated file download has malformed quoting')
    if not tokens:
        return None
    executable = os.path.basename(tokens[0]).lower()
    if executable == 'curl':
        return _curl_intent(
            tokens,
            dynamic=dynamic,
            shell_output_redirect=_has_shell_output_redirection(source),
        )
    if executable == 'wget':
        return _wget_intent(tokens, dynamic=dynamic)
    return _coarse_ambiguous_cookie_download(
        source,
        reason=(
            'cookie-authenticated file download is wrapped by another shell '
            'command or environment assignment'),
    )


__all__ = [
    'AuthenticatedDownloadIntent', 'parse_authenticated_download_command',
]
