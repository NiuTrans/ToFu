"""Pure bounded HTTP-probe plan shared by writer admission and execution.

Entry points: prepare_network_probe(source) -> hardened source or None. Only
complete GET/HEAD stdout/dev-null probes, inert scalar setup, literal for lists,
and probe-result assignments qualify. Unknown grammar/options retain existing
writer admission. This is cooperative scheduling, never authorization, a shell
sandbox, or a stable workspace read. Execution inserts curl's first-option -q
so implicit curlrc cannot introduce unclaimed outputs. Approval/display retain
original source. Reuse the setup owner's quote/substitution scanner.

prepare_file_transfers(source) hardens each curl/wget command of a larger
program in place (first-option -q / --no-config, inherited debug sinks unset)
and prepare_file_transfer reports its complete local write set, so the writer
owner claims only named outputs. A known-option table bounds both: any other
option, an expanded output or request value, or a shell that can rebind what
the transfer runs keeps ordinary admission.
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
# Transfer mode (never probes): request bodies and uploads only READ local
# files, and each output kind names the one local file it writes. Remote-named
# outputs (-O/-J), config files (-K), --next, --variable/--expand-* and every
# option missing here keep the conservative cwd fence.
_TRANSFER_FLAGS = _FLAGS | {
    '--insecure', '--compressed', '--verbose', '--get', '--no-buffer', '--http1.1',
    '--http2', '--no-progress-meter', '--location-trusted', '--fail-early',
    '--path-as-is', '--create-dirs',
}
_TRANSFER_SHORT_FLAGS = _SHORT_FLAGS | frozenset('kvGN')
_TRANSFER_VALUES = {
    **_VALUES,
    '-d': 'data', '--data': 'data', '--data-raw': 'data', '--data-binary': 'data',
    '--data-ascii': 'data', '--data-urlencode': 'data', '--json': 'data',
    '-F': 'data', '--form': 'data', '--form-string': 'data',
    '-T': 'data', '--upload-file': 'data', '-b': 'data', '--cookie': 'data',
    '--cacert': 'data', '--capath': 'data', '-E': 'data', '--cert': 'data',
    '--key': 'data', '-e': 'data', '--referer': 'data', '-r': 'data', '--range': 'data',
    '--limit-rate': 'data', '--max-redirs': 'number',
    # ``-`` selects stdout for these streams.
    '-D': 'stream', '--dump-header': 'stream', '-c': 'stream', '--cookie-jar': 'stream',
    '--trace': 'stream', '--trace-ascii': 'stream', '--stderr': 'stream',
    '--libcurl': 'file', '--etag-save': 'file', '--hsts': 'file', '--alt-svc': 'file',
    '--output-dir': 'directory',
}
_OUTPUT_KINDS = frozenset({'output', 'stream', 'file', 'directory'})
# Without -O, wget names its download after the URL inside the cwd.
_WGET_FLAGS = {'--quiet', '--no-verbose', '-nv', '--verbose', '--server-response',
               '--no-check-certificate', '--inet4-only', '--inet6-only', '--https-only',
               '--no-proxy', '--content-on-error', '--show-progress', '--no-config'}
_WGET_SHORT_FLAGS = frozenset('qvS46')
_WGET_VALUES = {
    '-O': 'output', '--output-document': 'output', '-T': 'number', '--timeout': 'number',
    '-t': 'number', '--tries': 'number', '--read-timeout': 'number',
    '--connect-timeout': 'number', '--dns-timeout': 'number', '--waitretry': 'number',
    '--max-redirect': 'number', '--header': 'header', '-U': 'header',
    '--user-agent': 'header', '--user': 'header', '--password': 'header',
    '--http-user': 'header', '--http-password': 'header', '--method': 'method',
    '--post-data': 'data', '--post-file': 'data', '--body-data': 'data',
    '--body-file': 'data', '--referer': 'data', '--load-cookies': 'data',
    '--ca-certificate': 'data', '--progress': 'data',
}
_CURL_HEADS = frozenset({'curl', '/bin/curl', '/usr/bin/curl'})
_WGET_HEADS = frozenset({'wget', '/bin/wget', '/usr/bin/wget'})
# Reserved words that introduce the transfer inside a compound command.
_COMPOUND_WORDS = frozenset({'if', 'then', 'elif', 'else', 'do', 'while', 'until', '!', '{'})
_COMPOUND_CLOSERS = {'done': 'do', 'fi': 'if', 'esac': 'case', '}': '{'}
# Same-shell builtins that can rebind PATH, variables or the cwd, or run code.
_STATE_BUILTINS = frozenset({
    'export', 'declare', 'typeset', 'local', 'readonly', 'read', 'mapfile', 'readarray',
    'getopts', 'let', 'unset', 'set', 'shopt', 'alias', 'enable', 'hash', 'builtin',
    'source', '.', 'eval', 'exec', 'trap', 'pushd', 'popd',
})
_LOOP_HEADER = re.compile(r'(?:^|[\s;&|(])(?:for|select)\s+(\S+)(?:\s+in\b([^;\n]*))?')
_FUNCTION_DEFINITION = re.compile(r'(?:^|[\s;&|(])(?:function\s|[^\s;&|()<>]+\s*\(\s*\))')
_MAX_SOURCE = 16_384
_MAX_WORDS = 256
_MAX_DEPTH = 8
CURL_FILE_ENV_PREFIX = 'env -u SSLKEYLOGFILE -u QLOGDIR -u CURL_MEMDEBUG '


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


def _option_items(arguments, flags, short_flags, values):
    """Yield (kind, option, value, dynamic) per argument; flags yield kind 'flag'."""
    index = 0
    while index < len(arguments):
        token, dynamic = _word(arguments[index][0])
        index += 1
        if not dynamic and token in flags:
            yield 'flag', token, '', False
            continue
        kind, option, value, value_dynamic = '', token, '', False
        if not dynamic and token in values:
            kind = values[token]
            if index == len(arguments):
                raise ValueError('Missing option value')
            value, value_dynamic = _word(arguments[index][0])
            index += 1
        elif not dynamic and token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            kind = values.get(option, '')
        elif not dynamic and token.startswith('-') and not token.startswith('--') and token != '-':
            # Bundled flags may end in a value option: -sSo/dev/null, -wFORMAT, -qO-.
            for position, flag in enumerate(token[1:], 1):
                if flag in short_flags:
                    yield 'flag', '-' + flag, '', False
                    continue
                option = '-' + flag
                kind = values.get(option, '')
                if not kind:
                    raise ValueError('Unknown option')
                value = token[position + 1:]
                if not value:
                    if index == len(arguments):
                        raise ValueError('Missing option value')
                    value, value_dynamic = _word(arguments[index][0])
                    index += 1
                break
            else:
                continue
        elif token.startswith(('http://', 'https://')):
            kind, value, value_dynamic = 'url', token, dynamic
        if not kind:
            raise ValueError('Unknown argument')
        yield kind, option, value, value_dynamic


def _checked_value(kind, value, dynamic, *, transfer):
    """Refuse values that could write, run helpers, or hide an option."""
    if kind == 'format' and (dynamic or value.startswith('@') or '%output{' in value):
        raise ValueError('Write-out can write files')
    if kind == 'method' and (dynamic or not (re.fullmatch(r'[A-Z]+', value) if transfer
                                             else value in {'GET', 'HEAD'})):
        raise ValueError('Unresolved request method')
    if kind == 'number' and (dynamic or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value)):
        raise ValueError('Unresolved numeric option')
    if kind == 'header' and value.startswith('@') and not transfer:
        raise ValueError('File-sourced arguments are not probe data')
    if kind == 'data' and dynamic:
        raise ValueError('Unresolved request data')
    if kind == 'url' and not value.startswith(('http://', 'https://')):
        raise ValueError('Not an explicit HTTP destination')


def _curl_arguments(arguments, *, transfer=False):
    """Return curl's local writes as (scope, cwd-relative path) pairs.

    Probe mode admits only GET/HEAD to stdout or /dev/null. Transfer mode adds
    any literal method, local request bodies/uploads (reads) and named outputs:
    FILE claims, except an --output-dir directory and the parents --create-dirs
    makes, which are TREE claims.
    """
    outputs, downloads, directory, create_dirs, urls = [], [], None, False, 0
    for kind, option, value, dynamic in _option_items(
            arguments, _TRANSFER_FLAGS if transfer else _FLAGS,
            _TRANSFER_SHORT_FLAGS if transfer else _SHORT_FLAGS,
            _TRANSFER_VALUES if transfer else _VALUES):
        if kind == 'flag':
            create_dirs = create_dirs or option == '--create-dirs'
            continue
        if kind in _OUTPUT_KINDS:
            if dynamic or not value or re.search(r'#[0-9]', value):
                raise ValueError('Unresolved file output')
            if kind == 'output' and value in {'-', '/dev/null'}:
                continue
            if not transfer:
                raise ValueError('File output is a writer')
            if kind == 'output':
                downloads.append(value)
            elif kind == 'directory':
                directory = value
            elif value != '/dev/null' and not (kind == 'stream' and value == '-'):
                outputs.append(('file', value))
            continue
        _checked_value(kind, value, dynamic, transfer=transfer)
        urls += kind == 'url'
    if not urls:
        raise ValueError('No HTTP destination')
    if directory is not None:
        # curl joins every -o name below the directory: DIR/NAME.
        if any('..' in value.split('/') for value in downloads):
            raise ValueError('Output escapes its output directory')
        return (*outputs, ('tree', directory))
    for value in downloads:
        parent = value.rsplit('/', 1)[0] if '/' in value.strip('/') else ''
        if create_dirs and parent:
            if '..' in parent.split('/'):
                raise ValueError('Created directories escape the output path')
            outputs.append(('tree', parent))
        else:
            outputs.append(('file', value))
    return tuple(outputs)


def _wget_arguments(arguments):
    """Return wget's local writes; only an explicit -O document qualifies."""
    outputs, documents, urls = [], 0, 0
    for kind, _option, value, dynamic in _option_items(
            arguments, _WGET_FLAGS, _WGET_SHORT_FLAGS, _WGET_VALUES):
        if kind == 'flag':
            continue
        if kind == 'output':
            if dynamic or not value:
                raise ValueError('Unresolved wget document')
            documents += 1
            if value not in {'-', '/dev/null'}:
                outputs.append(('file', value))
            continue
        _checked_value(kind, value, dynamic, transfer=True)
        urls += kind == 'url'
    if not urls or not documents:
        raise ValueError('wget writes a URL-named file into the cwd')
    return tuple(outputs)


def _check_scalar_printf(arguments):
    """Refuse printf forms that assign or evaluate: -v, dynamic formats, %n."""
    if not arguments:
        raise ValueError('Missing literal printf format')
    format_string, dynamic = _word(arguments[0][0])
    # Dynamic formats can become -v; %n destinations can evaluate
    # array subscripts. Only literal string-output formats qualify.
    if (dynamic or format_string.startswith('-')
            or '%' in re.sub(r'%%|%[-0-9.]*s|%b', '', format_string)):
        raise ValueError('Printf is not proven scalar output')


class _ProbePlan:
    """Call-local parser state; no bindings, evaluation, cache, or subprocesses."""
    def __init__(self, *, transfer=False):
        self.insertions = []
        self.probes = 0
        self.transfer = transfer
        self.outputs = []

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
        if head in _CURL_HEADS:
            self.outputs.extend(_curl_arguments(remaining[1:], transfer=self.transfer))
            self.probes += 1
            # Always insert as FIRST option; late -q does not disable curlrc.
            if remaining[1:2] and remaining[1][0] in {'-q', '--disable'}:
                return
            self.insertions.append((offset + len(raw_head), ' -q'))
        elif self.transfer and head in _WGET_HEADS:
            self.outputs.extend(_wget_arguments(remaining[1:]))
            self.probes += 1
            # wgetrc can add a log, cookie or background output file.
            if remaining[1:2] and remaining[1][0] == '--no-config':
                return
            self.insertions.append((offset + len(raw_head), ' --no-config'))
        elif head in {'echo', 'printf', ':'} and not assignments:
            if head == 'printf':
                _check_scalar_printf(remaining[1:])
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
    for offset, text in sorted(plan.insertions, reverse=True):
        source = source[:offset] + text + source[offset:]
    return source


def prepare_file_transfer(source):
    """Return hardened curl/wget source and its complete local write set.

    *source* must be one transfer command whose program word comes first;
    trailing redirects that open no file (``2>/dev/null``, ``2>&1``) stay.
    Assignments or a wrapper before the program, or a transfer inside another
    command's word, keep ordinary admission: hardening there would not reach
    the transfer. Writes are ('file'|'tree', path) relative to the command's
    cwd; an empty set means the transfer writes no local file. Target identity
    and FILE versus TREE admission remain with the command writer owner.
    """
    if not source or len(source) > _MAX_SOURCE:
        return None
    from .command_analysis import command_segment_discardable_redirect_split
    split = command_segment_discardable_redirect_split(source)
    core = source if split is None else split[0]
    if not source.startswith(core):
        return None
    plan = _ProbePlan(transfer=True)
    try:
        tokens = _tokens(core, 0)
        head, dynamic = _word(tokens[0][0]) if tokens else ('', True)
        if dynamic or head not in _CURL_HEADS | _WGET_HEADS:
            return None
        plan.simple(tokens, 0)
    except ValueError:
        return None
    if plan.probes != 1:
        return None
    for offset, text in sorted(plan.insertions, reverse=True):
        source = source[:offset] + text + source[offset:]
    return source, tuple(plan.outputs)


def _command_word(raw):
    """A literal program word, else None (computed, or not exactly one word)."""
    if '$' in raw or '`' in raw:
        return None
    try:
        words = shlex.split(raw)
    except ValueError:
        return None
    return words[0] if len(words) == 1 else None


def transfer_environment_stable(source):
    """Whether no command in *source* can change what a hardened transfer runs.

    Hardening fixes a transfer's own options, config files and debug sinks, not
    the shell around it. Assignments to non-ordinary names (PATH, LD_*, shell
    integer variables), state builtins, unproven printf formats, arithmetic,
    function definitions, loops over special names or computed lists, comments
    and detached jobs keep every transfer in *source* on ordinary admission, as
    the probe grammar does.
    """
    from .command_analysis import _mask_quoted_literals
    from .command_heredoc import command_admission_view
    from .command_structure import ShellStructureUnknown, shell_command_ranges
    if not source or len(source) > _MAX_SOURCE:
        return False
    # A runtime's literal stdin (`python3 - <<'EOF'`) is its program, not shell.
    view = command_admission_view(source)
    masked = _mask_quoted_literals(view)
    if (re.search(r'(?:^|\s)#|\(\(|(?<![&>|<])&(?![&>])', masked)
            or _FUNCTION_DEFINITION.search(masked)):
        return False
    for match in _LOOP_HEADER.finditer(masked):
        words = view[match.start(2):match.end(2)] if match[2] is not None else ''
        if (not _NAME.fullmatch(match[1]) or not _ordinary_name(match[1])
                or re.search(r'[$`*?\[{~]', words)):
            return False
    try:
        ranges = shell_command_ranges(source)
    except ShellStructureUnknown:
        return False
    # An unterminated compound command is a shell syntax error the range
    # parser tolerates; it never proves what would run.
    opened = dict.fromkeys(_COMPOUND_CLOSERS.values(), 0)
    for start, end in ranges:
        try:
            tokens = _tokens(source[start:end], start)
        except ValueError:
            return False
        for raw, _ in tokens:
            if raw in opened:
                opened[raw] += 1
            elif raw in _COMPOUND_CLOSERS:
                opened[_COMPOUND_CLOSERS[raw]] -= 1
                if opened[_COMPOUND_CLOSERS[raw]] < 0:
                    return False
            elif raw not in _COMPOUND_WORDS:
                break
        while tokens and tokens[0][0] in _COMPOUND_WORDS:
            tokens = tokens[1:]
        while tokens and _ASSIGNMENT.fullmatch(tokens[0][0]):
            if not _ordinary_name(_ASSIGNMENT.fullmatch(tokens[0][0])[1]):
                return False
            tokens = tokens[1:]
        if not tokens:
            continue
        head = _command_word(tokens[0][0])
        if head is None or head in _STATE_BUILTINS:
            return False
        if head == 'printf':
            try:
                _check_scalar_printf(tokens[1:])
            except ValueError:
                return False
    return not any(opened.values())


def prepare_file_transfers(source):
    """Harden every qualifying curl/wget command in place, preserving the program.

    The per-command env wrapper unsets inherited debug sinks for the transfer
    only; every other command keeps its environment. A whole source that is a
    network probe keeps the probe owner's plan, and an unstable shell keeps
    every transfer on ordinary admission.
    """
    if (not source or ('curl' not in source and 'wget' not in source)
            or prepare_network_probe(source) is not None
            or not transfer_environment_stable(source)):
        return source
    from .command_structure import ShellStructureUnknown, shell_command_ranges
    try:
        ranges = shell_command_ranges(source)
    except ShellStructureUnknown:
        return source
    replacements = []
    for start, end in ranges:
        try:
            tokens = _tokens(source[start:end], start)
        except ValueError:
            continue
        while tokens and tokens[0][0] in _COMPOUND_WORDS:
            tokens = tokens[1:]
        if not tokens:
            continue
        begin = tokens[0][1]
        segment = source[begin:end].rstrip()
        plan = prepare_file_transfer(segment)
        if plan is not None:
            replacements.append((begin, begin + len(segment), CURL_FILE_ENV_PREFIX + plan[0]))
    for start, end, planned in reversed(replacements):
        source = source[:start] + planned + source[end:]
    return source
