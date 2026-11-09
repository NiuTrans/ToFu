"""New integrations must inherit the shared network authority automatically."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
# These are concrete probe transports, below route admission, not app callers.
WIRE_OWNERS = {
    'lib/network_routes.py', 'lib/proxy.py', 'lib/netpath.py',
    'lib/project_mod/network_command.py',
}
RAW_CALLS = {
    *(f'requests.{name}' for name in (
        'Session', 'session', 'request', 'get', 'post', 'put', 'delete', 'head', 'patch')),
    'httpx.Client', 'httpx.AsyncClient', 'httpx2.Client', 'httpx2.AsyncClient',
    'urllib.request.urlopen', 'urllib.request.build_opener',
}


def test_application_network_calls_use_managed_transports():
    failures = []
    for root in ('lib', 'routes', 'plugins/tofu-trading/tofu_trading'):
        for path in (ROOT / root).rglob('*.py'):
            relative = path.relative_to(ROOT).as_posix()
            if relative in WIRE_OWNERS or relative.startswith('lib/http_transport/'):
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            imports = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for entry in node.names:
                        imports[entry.asname or entry.name.split('.')[0]] = (
                            entry.name if entry.asname else entry.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for entry in node.names:
                        imports[entry.asname or entry.name] = node.module + '.' + entry.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                first, separator, rest = name.partition('.')
                resolved = imports.get(first, first) + separator + rest
                if resolved in RAW_CALLS:
                    failures.append(f'{relative}:{node.lineno}: {resolved}')
    assert not failures, 'Use managed network clients (docs/NETWORK.md):\n' + '\n'.join(failures)
