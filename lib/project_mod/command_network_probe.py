"""Pure bounded HTTP-probe plan shared by writer admission and execution.

Entry point: prepare_network_probe(source) -> hardened source or None. Only
complete GET/HEAD stdout/dev-null probes, inert scalar setup, literal for lists,
and probe-result assignments qualify. Unknown grammar/options retain existing
writer admission. This is cooperative scheduling, never authorization, a shell
sandbox, or a stable workspace read. Execution inserts curl's first-option -q
so implicit curlrc cannot introduce unclaimed outputs. Approval/display retain
original source. Reuse the setup owner's quote/substitution scanner.
"""
import re
import shlex

from .command_setup import _scan, _substitution_end

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_REFERENCE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})')
_ASSIGNMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', re.S)
_RESERVED = {'IFS', 'ENV', 'BASH_ENV', 'PATH', 'CDPATH', 'HOME', 'CURL_HOME',
             'XDG_CONFIG_HOME', 'SHELLOPTS', 'BASHOPTS', 'PROMPT_COMMAND',
             'SSLKEYLOGFILE', 'QLOGDIR', 'CURL_MEMDEBUG'}
_FLAGS = {'--silent', '--show-error', '--fail', '--fail-with-body', '--head',
          '--include', '--location', '--globoff', '--disable', '--ipv4', '--ipv6'}
_SHORT_FLAGS = frozenset('sSfILigq46')
_VALUES = {'-H': 'header', '--header': 'header', '-o': 'output', '--output': 'output',
           '-w': 'format', '--write-out': 'format', '-X': 'method', '--request': 'method',
           '--url': 'url', '--noproxy': 'route', '-x': 'route', '--proxy': 'route',
           '--connect-timeout': 'number', '-m': 'number', '--max-time': 'number',
           '--retry': 'number', '--retry-delay': 'number', '--retry-max-time': 'number',
           '-A': 'header', '--user-agent': 'header', '-u': 'header', '--user': 'header'}
_MAX_SOURCE = 16_384
_MAX_WORDS = 256
_MAX_DEPTH = 8


def _ordinary_name(name):
    # Shell special integer/array variables (OPTIND, RANDOM, SECONDS, ...)
    # can evaluate assignment DATA as arithmetic and execute substitutions.
    # No setup binding to uppercase/special names is needed for HTTP probes;
    # quoted references to inherited/vault uppercase credentials remain data.
    return (not name.isupper() and name not in _RESERVED
            and not name.startswith(('LD_', 'DYLD_', 'GIT_', 'BASH_')))


def _word(raw, *, assignment=False):
    """Decode literal data/quoted scalar references, never executable expansion."""
    quote, index, output, dynamic = '', 0, [], False
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            if index + 1 == len(raw) or raw[index + 1] in '\r\n':
                raise ValueError('Unsupported continuation')
            output.append(raw[index:index + 2])
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif quote != "'" and char == '$':
            reference = _REFERENCE.match(raw, index)
            if reference is None or (not quote and not assignment):
                raise ValueError('Unresolved shell expansion')
            output.append('scalar')
            dynamic = True
            index = reference.end()
            continue
        elif quote != "'" and char == '`':
            raise ValueError('Executable expansion')
        elif not quote and char == '{' and index and raw[index - 1] == '%':
            placeholder = re.match(r'\{[A-Za-z_][A-Za-z0-9_]*\}', raw[index:])
            if placeholder is None:
                raise ValueError('Unsupported brace expansion')
            output.append(placeholder[0])
            index += len(placeholder[0])
            continue
        elif not quote and char in '<>;&|(){}*?[]~#':
            raise ValueError('Shell effect or expansion')
        output.append(char)
        index += 1
    if quote:
        raise ValueError('Unclosed quote')
    words = shlex.split(''.join(output), comments=False, posix=True)
    if len(words) != 1:
        raise ValueError('Not one scalar word')
    return words[0], dynamic


def _tokens(source, offset):
    result, cursor = [], offset
    for raw, separator in _scan(source, words=True):
        if raw.strip():
            result.append((raw, cursor))
        cursor += len(raw) + len(separator)
    if len(result) > _MAX_WORDS:
        raise ValueError('Probe word bound exceeded')
    return result


def _curl_arguments(arguments):
    urls, index = 0, 0
    while index < len(arguments):
        raw = arguments[index][0]
        token, dynamic = _word(raw)
        kind, value, value_dynamic = '', '', False
        if not dynamic and token in _FLAGS:
            index += 1
            continue
        if not dynamic and token in _VALUES:
            kind = _VALUES[token]
            index += 1
            if index == len(arguments):
                raise ValueError('Missing curl option value')
            value, value_dynamic = _word(arguments[index][0])
        elif not dynamic and token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            kind = _VALUES.get(option, '')
        elif not dynamic and token.startswith('-') and not token.startswith('--'):
            # Bundled flags may end in a value option: -sSo/dev/null, -wFORMAT.
            for position, flag in enumerate(token[1:], 1):
                if flag in _SHORT_FLAGS:
                    continue
                kind = _VALUES.get('-' + flag, '')
                if not kind:
                    raise ValueError('Unknown curl option')
                value = token[position + 1:]
                if not value:
                    index += 1
                    if index == len(arguments):
                        raise ValueError('Missing curl option value')
                    value, value_dynamic = _word(arguments[index][0])
                break
            else:
                index += 1
                continue
        elif token.startswith(('http://', 'https://')):
            kind, value, value_dynamic = 'url', token, dynamic
        if not kind:
            raise ValueError('Unknown curl argument')
        if kind == 'output' and (value_dynamic or value not in {'-', '/dev/null'}):
            raise ValueError('File output is a writer')
        if kind == 'format' and (value_dynamic or value.startswith('@') or '%output{' in value):
            raise ValueError('Write-out can write files')
        if kind == 'method' and (value_dynamic or value not in {'GET', 'HEAD'}):
            raise ValueError('Not a GET/HEAD probe')
        if kind == 'number' and (value_dynamic or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value)):
            raise ValueError('Unresolved numeric option')
        if kind == 'header' and value.startswith('@'):
            raise ValueError('File-sourced arguments are not probe data')
        if kind == 'url':
            if not value.startswith(('http://', 'https://')):
                raise ValueError('Not an explicit HTTP destination')
            urls += 1
        index += 1
    if not urls:
        raise ValueError('No HTTP destination')


class _ProbePlan:
    """Call-local parser state; no bindings, evaluation, cache, or subprocesses."""
    def __init__(self):
        self.insertions = []
        self.probes = 0

    def assignment(self, raw, offset, depth):
        match = _ASSIGNMENT.fullmatch(raw)
        if match is None or not _ordinary_name(match[1]):
            raise ValueError('Unsupported setup')
        value = match[2]
        value_offset = offset + match.start(2)
        if value.startswith('"') and value.endswith('"'):
            value, value_offset = value[1:-1], value_offset + 1
        if value.startswith('$('):
            end = _substitution_end(value, 0)
            if end != len(value):
                raise ValueError('Probe substitution must be the whole value')
            self.sequence(value[2:-1], value_offset + 2, depth + 1, loops=False)
        else:
            _word(match[2], assignment=True)

    def simple(self, tokens, depth):
        if not tokens:
            raise ValueError('Empty probe command')
        assignments = 0
        for raw, offset in tokens:
            if _ASSIGNMENT.fullmatch(raw) is None:
                break
            self.assignment(raw, offset, depth)
            assignments += 1
        remaining = tokens[assignments:]
        if not remaining:
            return
        raw_head, offset = remaining[0]
        head, dynamic = _word(raw_head)
        if dynamic:
            raise ValueError('Dynamic executable')
        if head in {'curl', '/bin/curl', '/usr/bin/curl'}:
            _curl_arguments(remaining[1:])
            self.probes += 1
            # Always insert as FIRST option; late -q does not disable curlrc.
            if remaining[1:2] and remaining[1][0] in {'-q', '--disable'}:
                return
            self.insertions.append(offset + len(raw_head))
        elif head in {'echo', 'printf', ':'} and not assignments:
            if head == 'printf':
                if len(remaining) < 2:
                    raise ValueError('Missing literal printf format')
                format_string, dynamic = _word(remaining[1][0])
                # Dynamic formats can become -v; %n destinations can evaluate
                # array subscripts. Only literal string-output formats qualify.
                if (dynamic or format_string.startswith('-')
                        or '%' in re.sub(r'%%|%[-0-9.]*s|%b', '', format_string)):
                    raise ValueError('Printf is not proven scalar output')
            for raw, _ in remaining[1:]:
                _word(raw)
        else:
            raise ValueError('Not probe/scalar output')

    def sequence(self, source, offset=0, depth=0, *, loops=True):
        if depth > _MAX_DEPTH:
            raise ValueError('Probe nesting bound exceeded')
        cursor, loop, need_do, commands = offset, False, False, 0
        for raw_segment, separator in _scan(source):
            tokens = _tokens(raw_segment, cursor)
            cursor += len(raw_segment) + len(separator)
            if separator not in {'', ';', '\n', '&&', '||', '|'}:
                raise ValueError('Detached/unknown shell control')
            if not tokens:
                if separator not in {'', '\n'} or need_do:
                    raise ValueError('Empty shell segment')
                continue
            if tokens[0][0] == 'for':
                if (not loops or loop or len(tokens) < 4 or tokens[2][0] != 'in'
                        or not _NAME.fullmatch(tokens[1][0])
                        or not _ordinary_name(tokens[1][0]) or separator not in {';', '\n'}):
                    raise ValueError('Unsupported for list')
                for raw, _ in tokens[3:]:
                    _, dynamic = _word(raw)
                    if dynamic:
                        raise ValueError('Nonliteral loop list')
                loop, need_do = True, True
                continue
            if need_do:
                if tokens[0][0] != 'do':
                    raise ValueError('Missing do')
                tokens, need_do = tokens[1:], False
                if not tokens:
                    continue
            elif tokens[0][0] == 'done':
                if not loop or len(tokens) != 1 or not commands:
                    raise ValueError('Unmatched done')
                loop = False
                continue
            self.simple(tokens, depth)
            commands += 1
        if loop or need_do or not commands:
            raise ValueError('Incomplete probe source')


def prepare_network_probe(source):
    """Return complete hardened probe source, else None with no partial exemption."""
    if not source or len(source) > _MAX_SOURCE:
        return None
    plan = _ProbePlan()
    try:
        plan.sequence(source)
    except ValueError:
        return None
    if not plan.probes:
        return None  # Do not widen ordinary shell admission without an HTTP probe.
    for offset in sorted(plan.insertions, reverse=True):
        source = source[:offset] + ' -q' + source[offset:]
    return source
