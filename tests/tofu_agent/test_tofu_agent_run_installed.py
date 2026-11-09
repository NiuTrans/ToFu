"""Installed-wheel `tofu-agent run`: one real tool round without checkout imports.

A checkout hides wheel-boundary regressions: PYTHONPATH, the editable-install
finder and the working directory all make packages the wheel excludes, such as
`lib.storage`, importable again. This test builds the wheel from the working
tree with an isolated setuptools ``build_base``, extracts it as the only
application path beside this interpreter's installed dependencies, and runs the
wheel's declared ``tofu-agent`` console entry point under ``-S`` from a
directory outside the checkout against a scripted loopback provider. The build
is offline: the pinned ``build-system.requires`` come from the uv cache, never
the network (landing verification runs without proxy routes).
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import Distribution
import json
import os
from pathlib import Path
import site
import subprocess
import sys
import sysconfig
import threading
import zipfile

import pytest

from tests.support.standalone_model_routing import standalone_model_routing_envelope

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]
MARKER = 'installed-wheel-marker.txt'
FINAL_ANSWER = 'The installed wheel wrote its marker.'
# The one declared optional storage capability: lib/tasks_pkg/handlers/__init__.py
# drops the large tool-result continuation handler when lib.storage is absent.
OPTIONAL_STORAGE_IMPORTERS = frozenset({'lib.tool_results.tool_result_artifacts'})

# Mirrors the console script an installer generates from entry_points.txt. It
# also reports where every loaded module came from and which imports under the
# wheel's own top-level packages nothing could satisfy, even swallowed ones.
_CONSOLE_SCRIPT = '''
import importlib, json, sys
report, module_name, attribute, top_level = sys.argv[1:5]
sys.argv = ['tofu-agent', *sys.argv[5:]]
top_level, missing = frozenset(json.loads(top_level)), []

class UnsatisfiedWheelImports:
    def find_spec(self, name, path=None, target=None):
        if name.partition('.')[0] in top_level:
            frame = sys._getframe(1)
            while frame is not None and frame.f_globals.get('__name__', '').startswith('importlib'):
                frame = frame.f_back
            missing.append([name, frame and frame.f_globals.get('__name__')])
        return None

sys.meta_path.append(UnsatisfiedWheelImports())
try:
    status = getattr(importlib.import_module(module_name), attribute)()
finally:
    with open(report, 'w', encoding='utf-8') as stream:
        json.dump({'missing': missing, 'modules': {
            name: getattr(module, '__file__', None)
            for name, module in list(sys.modules.items())}}, stream)
sys.exit(status)
'''


def _build_wheel(tmp_path: Path) -> Path:
    from uv import find_uv_bin

    # setuptools otherwise writes build/ and *.egg-info into the source tree,
    # and a stale build/lib can leak packages the current exclusions drop.
    isolation = tmp_path / 'setuptools-isolation.cfg'
    egg_base = tmp_path / 'egg-info'
    egg_base.mkdir()
    isolation.write_text(
        f'[build]\nbuild_base = {tmp_path / "build"}\n'
        f'[egg_info]\negg_base = {egg_base}\n', encoding='utf-8')
    dist = tmp_path / 'dist'
    environment = {**os.environ, 'DIST_EXTRA_CONFIG': str(isolation)}
    environment.pop('PYTHONPATH', None)
    try:
        built = subprocess.run(
            [find_uv_bin(), 'build', '--wheel', '--offline', '--python', sys.executable,
             '--out-dir', str(dist), str(ROOT)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as expired:
        pytest.fail(f'wheel build exceeded {expired.timeout}s: {str(expired.stderr)[-4000:]}')
    if built.returncode and 'was not found in the cache' in built.stderr:
        # Landing verification points UV_CACHE_DIR at an isolated root. The
        # pinned build backend is not a test oracle: a cold cache is an
        # unexercisable precondition, not a wheel defect. When it is warm the
        # build is offline and exercises the installed wheel end to end.
        pytest.skip('uv cache has no pinned build backend for an offline wheel build')
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-4000:]
    (wheel,) = dist.glob('tofu_agent-*.whl')
    return wheel


def _scripted_provider(requests_seen: list[dict]) -> ThreadingHTTPServer:
    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests_seen.append(request)
            if len(requests_seen) == 1:
                delta = {'tool_calls': [{
                    'index': 0, 'id': 'call-installed-marker', 'type': 'function',
                    'function': {'name': 'run_command', 'arguments': json.dumps({
                        'command': f'printf installed-wheel > {MARKER}'})},
                }]}
                finish = 'tool_calls'
            else:
                delta, finish = {'content': FINAL_ANSWER}, 'stop'
            frames = [
                {'choices': [{'index': 0, 'delta': delta}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                 'usage': {'prompt_tokens': 200, 'completion_tokens': 10}},
            ]
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write((wire + 'data: [DONE]\n\n').encode())
            self.wfile.flush()
            self.close_connection = True

    return ThreadingHTTPServer(('127.0.0.1', 0), Provider)


@pytest.mark.timeout(600)
def test_installed_wheel_run_completes_a_tool_round_without_checkout_imports(tmp_path):
    wheel = _build_wheel(tmp_path)
    installed = tmp_path / 'installed'
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    # The published contract keeps durable storage authorities out of the wheel.
    assert not (installed / 'lib' / 'storage').exists()
    (distribution,) = (Distribution.at(path) for path in installed.glob('tofu_agent-*.dist-info'))
    (console,) = distribution.entry_points.select(group='console_scripts', name='tofu-agent')
    top_level = distribution.read_text('top_level.txt').split()
    # A venv overlay adds its base environment's site-packages through a .pth
    # file, which site.getsitepackages() does not list and -S never processes.
    dependencies = list(dict.fromkeys(
        path for path in [*site.getsitepackages(), *sys.path]
        if Path(path).name in {'site-packages', 'dist-packages'} and Path(path).is_dir()))

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    requests_seen: list[dict] = []
    server = _scripted_provider(requests_seen)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id='installed-wheel-test')
    for model in envelope['model_routing']['models']:
        model['capabilities'].append('tools')
    for offering in envelope['model_routing']['offerings']:
        offering['capabilities'].append('tools')
    environment = {**os.environ,
                   'PYTHONPATH': os.pathsep.join([str(installed), *dependencies]),
                   'TOFU_AGENT_MODEL_ROUTING': json.dumps(envelope),
                   'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                   'TOFU_DATA_DIR': str(tmp_path / 'data')}
    result_path = tmp_path / 'run.json'
    report_path = tmp_path / 'imports.json'
    try:
        completed = subprocess.run([
            sys.executable, '-S', '-c', _CONSOLE_SCRIPT,
            str(report_path), console.module, console.attr, json.dumps(top_level),
            '--env-file', str(tmp_path / 'absent.env'),
            'run', '--task', f'Create {MARKER} in the project root.',
            '--cwd', str(workspace), '--output', str(result_path), '--timeout-s', '90',
        ], cwd=workspace, env=environment, capture_output=True, text=True, timeout=150)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)

    assert 'ModuleNotFoundError' not in completed.stderr, completed.stderr[-4000:]
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-4000:]
    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert result['ok'] is True, result.get('error')
    assert result['status'] == 'done' and result['content'] == FINAL_ANSWER
    assert (workspace / MARKER).read_text(encoding='utf-8') == 'installed-wheel'
    assert len(requests_seen) == 2
    assert any(message.get('role') == 'tool'
               and message.get('tool_call_id') == 'call-installed-marker'
               for message in requests_seen[1]['messages'])

    report = json.loads(report_path.read_text(encoding='utf-8'))
    paths = sysconfig.get_paths()
    allowed = [root.resolve() for root in (
        installed, *map(Path, dependencies), Path(paths['stdlib']), Path(paths['platstdlib']))]
    foreign = {name: path for name, path in report['modules'].items() if path and not any(
        Path(path).resolve().is_relative_to(root) for root in allowed)}
    assert not foreign
    assert 'tofu_agent.cli' in report['modules']
    unexpected = [[name, importer] for name, importer in report['missing'] if not (
        importer in OPTIONAL_STORAGE_IMPORTERS
        and (name == 'lib.storage' or name.startswith('lib.storage.')))]
    assert not unexpected
