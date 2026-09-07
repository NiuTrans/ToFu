"""Guards for the two contracts that make this package safe to embed.

Both are enforced here rather than by convention because both fail SILENTLY and
far from their cause:

1. **stdout stays clean.** A host may run this library inside a process whose
   stdout carries a protocol stream (an MCP stdio server speaks JSON-RPC over
   stdout). One stray write corrupts the stream, and the host reports an
   unparseable message rather than "tofu_search printed something".

2. **One HTTP seam per layer.** Timeouts, the shared User-Agent, retry policy
   and the SSRF guard live in the seam modules. Code that reaches for
   ``requests.get`` directly gets none of them, and the omission is invisible
   until a request hangs forever or resolves an internal address.
"""

import ast
import io
import pathlib
from contextlib import redirect_stdout

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent / 'tofu_search'


# ── Contract 1: nothing in the package writes to stdout ──────────────────────

def test_importing_the_package_writes_nothing_to_stdout():
    """Import side effects must not touch stdout.

    Import is the dangerous moment: it runs module-level code in every
    submodule (pool construction, availability probes, logger setup) before the
    host has had any chance to intervene.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        import importlib

        import tofu_search
        importlib.reload(tofu_search)

    assert buf.getvalue() == '', (
        'importing tofu_search wrote to stdout, which corrupts a stdio '
        f'protocol stream: {buf.getvalue()[:200]!r}')


def test_library_logging_goes_to_stderr_not_stdout():
    """The package's logger must never be wired to stdout.

    tofu_search.log attaches a handler only when the root logger has none. That
    fallback handler has to target stderr — if it ever defaults to stdout, every
    log record becomes protocol corruption.
    """
    import logging

    from tofu_search.log import get_logger

    logger = get_logger('tofu_search.test_probe')
    buf = io.StringIO()
    with redirect_stdout(buf):
        logger.warning('probe message that must not reach stdout')
        for h in logging.getLogger().handlers:
            h.flush()

    assert buf.getvalue() == '', (
        'a log record reached stdout; the library must log to stderr only: '
        f'{buf.getvalue()[:200]!r}')


def test_no_print_calls_in_package_source():
    """Belt-and-braces for ruff's T20.

    Lint can be skipped with a `# noqa`; this cannot. Docstrings that *show* a
    print() to the reader are fine — this walks the AST, so only real call
    nodes count.
    """
    offenders = []
    for path in sorted(PKG_ROOT.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ('print', 'pprint')):
                offenders.append(f'{path.relative_to(PKG_ROOT.parent)}:{node.lineno}')

    assert not offenders, (
        'print() in library code writes to stdout and corrupts a stdio '
        f'protocol stream; use tofu_search.log instead: {offenders}')


# ── Contract 2: HTTP goes through a seam ─────────────────────────────────────

#: Modules allowed to construct raw HTTP calls, and why.
_TRANSPORT_OWNERS = {
    # The general-purpose seam: applies the shared UA and a default timeout.
    'http_client.py',
    # The fetch stack's own pooled session (retry adapter + SSRF guard). A
    # deliberate second stack, not a bypass: page fetching needs streaming and
    # per-host pooling that the simple seam does not offer.
    'fetch/http.py',
    'fetch/core.py',
    'fetch/utils.py',
    # The search-engine seam (proxy-mode learning + engine throttle).
    'search/_common.py',
    # Talks to an OpenAI-compatible endpoint, not to the open web: none of the
    # web-fetch policy (SSRF guard, bot detection, circuit breaker) applies.
    'llm_adapter.py',
}

_FORBIDDEN_ATTRS = {
    ('requests', 'get'), ('requests', 'post'), ('requests', 'put'),
    ('requests', 'delete'), ('requests', 'request'),
}
_FORBIDDEN_NAMES = {'urlopen'}
_FORBIDDEN_PREFIXES = ('httpx.', 'http.client', 'urllib.request.')


def _package_modules():
    for path in sorted(PKG_ROOT.rglob('*.py')):
        yield path, path.relative_to(PKG_ROOT).as_posix()


@pytest.mark.parametrize('path,rel', list(_package_modules()),
                         ids=lambda v: v if isinstance(v, str) else '')
def test_http_calls_go_through_a_transport_seam(path, rel):
    """No module outside the seam list may build its own HTTP call.

    Asserted on real AST call nodes, so a docstring or a comment mentioning
    ``requests.get`` does not trip it — only code does.
    """
    if rel in _TRANSPORT_OWNERS:
        pytest.skip(f'{rel} is a declared transport owner')

    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (func.value.id, func.attr) in _FORBIDDEN_ATTRS:
                offenders.append(f'{func.value.id}.{func.attr} @ line {node.lineno}')
        elif isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAMES:
            offenders.append(f'{func.id} @ line {node.lineno}')

    for prefix in _FORBIDDEN_PREFIXES:
        if prefix in source.replace(' ', ''):
            offenders.append(f'{prefix} referenced')

    assert not offenders, (
        f'{rel} builds its own HTTP call ({offenders}). Route it through '
        'tofu_search.http_client (or vertical/base.py for verticals) so it '
        'inherits the shared timeout, User-Agent and retry policy. If this '
        'module genuinely owns a transport, add it to _TRANSPORT_OWNERS with '
        'a comment saying why.')


def test_mcp_sdk_is_imported_from_exactly_one_place():
    """The MCP SDK may only be reached via `from mcp.server import MCPServer`.

    ★ This is what makes "migrating SDK major versions is an import change, not
    a rewrite" a fact rather than a hope.

    Confining the coupling to one import and two call sites keeps future SDK
    migrations mechanical. The moment a second
    module reaches into `mcp.server.lowlevel`, `mcp.types` or similar, the
    blast radius stops being knowable -- and it would happen gradually, one
    convenient import at a time, which is exactly the kind of drift a guard
    catches and a code review does not.
    """
    server_root = PKG_ROOT / 'mcp_server'
    if not server_root.exists():
        pytest.skip('mcp_server package not present')

    allowed = ('mcp.server', 'MCPServer')
    offenders = []
    for path in sorted(server_root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if module == 'mcp' or module.startswith('mcp.'):
                    names = [a.name for a in node.names]
                    if module != allowed[0] or names != [allowed[1]]:
                        offenders.append(
                            f'{path.name}:{node.lineno} from {module} import '
                            f'{", ".join(names)}')
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == 'mcp' or alias.name.startswith('mcp.'):
                        offenders.append(f'{path.name}:{node.lineno} import {alias.name}')

    assert not offenders, (
        'the MCP SDK must be reached only via '
        '`from mcp.server import MCPServer`, so a future SDK migration '
        f'stays an import change rather than a rewrite. Found: {offenders}')


def test_transport_owner_list_has_no_stale_entries():
    """A seam that no longer exists must not stay whitelisted.

    Without this, deleting or renaming a transport owner leaves a permanent
    hole in the guard that nothing points at.
    """
    missing = [rel for rel in _TRANSPORT_OWNERS if not (PKG_ROOT / rel).exists()]
    assert not missing, (
        f'_TRANSPORT_OWNERS lists modules that no longer exist: {missing}')
