"""HTTP probes and transfers run the complete prepared plan, never a replay.

Execution-side preparation only: tool calls take no workspace lease
(docs/runbooks/GIT_WORKSPACE_GUARD.md), so these plans no longer decide
admission. Hidden curl/wget output configuration stays suppressed.
"""
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lib.project_mod import run_command as commands, tools
from lib.project_mod.command_network_probe import CURL_FILE_ENV_PREFIX, prepare_network_probe
from lib.project_mod.tool_admission import RecordedCommandSource, tool_admission_scope
from lib.project_mod.workspace_lease import workspace_lease_fds
from tests.project_mod.test_command_execution_preparation import (
    _internal_writer, repo as repo,
)

pytestmark = pytest.mark.unit

PROBE = '''for p in agent/terminal_bench_pipeline_agent.py pipeline/terminal_bench_pipeline_agent.py agent/tb_pipeline_agent.py agent/terminal_bench_agent.py; do
  code=$(curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $DEV_SANKUAI_TOKEN" "https://dev.internal.example.com/rest/api/latest/projects/nlp/repos/llm-eval-premium/raw/$p?at=2e0b702f508460948eef8b58d455449b7d35d335")
  echo "$p -> $code"
done'''

SAFE = [
    PROBE,
    'curl https://example.invalid/api',
    'curl -q -sS https://example.invalid/api',
    'curl --disable -I https://example.invalid/api',
    'curl -sSo/dev/null -w%{http_code} https://example.invalid/api',
    'curl --output=- --request=HEAD --url=https://example.invalid/api',
    'curl -I -H "Authorization: Bearer $TOKEN" "https://example.invalid/$PATH_ITEM"',
    'code=$(curl -sS https://example.invalid); printf "%s\\n" "$code"',
    'code="$(curl -sS https://example.invalid)"; echo "$code"',
    'curl https://example.invalid | printf "%s" body',
    'for p in a b; do curl -I "https://example.invalid/$p"; done',
    "value='literal'; curl -H \"X-Value: $value\" https://example.invalid",
]

UNSAFE = [
    'curl -o target https://example.invalid',
    'curl --output=target https://example.invalid',
    'curl -O https://example.invalid',
    'curl -sSO https://example.invalid',
    'curl --remote-name-all https://example.invalid',
    'curl -J https://example.invalid',
    'curl -D target https://example.invalid',
    'curl --dump-header=target https://example.invalid',
    'curl -c target https://example.invalid',
    'curl --cookie-jar target https://example.invalid',
    'curl --trace target https://example.invalid',
    'curl --trace-ascii target https://example.invalid',
    'curl --libcurl target https://example.invalid',
    'curl --alt-svc target https://example.invalid',
    'curl --hsts target https://example.invalid',
    'curl --etag-save target https://example.invalid',
    'curl --output-dir target https://example.invalid',
    'curl -K config https://example.invalid',
    'curl --config=config https://example.invalid',
    'curl --variable name=value https://example.invalid',
    'curl --expand-output target https://example.invalid',
    'curl -w "%output{target}%{http_code}" https://example.invalid',
    'curl -w @config https://example.invalid',
    'curl -w "$FORMAT" https://example.invalid',
    'curl -o "$OUTPUT" https://example.invalid',
    'curl $OPTIONS https://example.invalid',
    'curl "${OPTIONS}" https://example.invalid',
    'curl -X "$METHOD" https://example.invalid',
    'curl https://example.invalid > target',
    'curl https://example.invalid; touch target',
    'curl https://example.invalid | tee target',
    'curl -H "X-Value: $(touch target)" https://example.invalid',
    'curl "https://example.invalid/$(touch target)"',
    'code=$(curl https://example.invalid; touch target); echo "$code"',
    'code=$(echo "$(touch target)"; curl https://example.invalid)',
    'code=`curl https://example.invalid`; echo "$code"',
    'for p in $(touch target); do curl https://example.invalid; done',
    'for p in *; do curl https://example.invalid; done',
    'for p in a; do curl https://example.invalid; touch target; done',
    'for p in a; do curl https://example.invalid',
    'for PATH in a; do curl https://example.invalid; done',
    'PATH=other; curl https://example.invalid',
    'BASH_ENV=source; curl https://example.invalid',
    'LD_PRELOAD=source curl https://example.invalid',

    'SSLKEYLOGFILE=target curl https://example.invalid',
    'QLOGDIR=target curl https://example.invalid',
    'CURL_MEMDEBUG=target curl https://example.invalid',
    'curl https://example.invalid; printf -v PATH other',

    "OPTIND='a[$(touch target)]'; curl https://example.invalid",
    "RANDOM='a[$(touch target)]'; curl https://example.invalid",
    "SECONDS='a[$(touch target)]'; curl https://example.invalid",
    "FMT=-v; printf \"$FMT\" 'a[$(touch target)]' 0; curl https://example.invalid",
    "fmt=-v; printf \"$fmt\" 'a[$(touch target)]' 0; curl https://example.invalid",
    "printf '%n' 'a[$(touch target)]'; curl https://example.invalid",
    "for OPTIND in 'a[$(touch target)]'; do curl https://example.invalid; done",
    'sh -c "curl https://example.invalid"',
    'env curl https://example.invalid',
    'curl https://example.invalid &',
    'curl https://example.invalid # comment',
    'curl "https://example.invalid',
    'wget -O target https://example.invalid',
]


# Not probes (a request body, a non-GET method, or another program consuming
# the response), yet hardened transfers that write no local file.
LOCAL_NO_WRITE = [
    'curl -s https://example.invalid | jq .',
    "curl -sS -H 'A: b' https://example.invalid | python3 -c 'import sys; print(sys.stdin.read())'",
    'curl -X POST -d @source https://example.invalid',
    'curl -X POST https://example.invalid',
    'curl --data body https://example.invalid',
    'curl -T source https://example.invalid',
    'curl -F data=@source https://example.invalid',
    'curl --data-binary @source -b source --cacert source -k https://example.invalid | cat',
    'curl -s https://example.invalid 2>/dev/null | jq .',
    'curl -sS -D - -o /dev/null https://example.invalid | cat',
    'for id in a b; do curl -s "https://example.invalid/$id" | jq .; done',
    'code=$(curl -s -X POST https://example.invalid); echo "$code"',
    'wget -qO- https://example.invalid | jq .',
    'wget -q -O - https://example.invalid | cat',
]


@pytest.mark.parametrize('source', LOCAL_NO_WRITE)
def test_transfers_writing_no_local_file_are_hardened_once(source):
    assert prepare_network_probe(source) is None
    executed = commands.command_execution_source(source)
    assert CURL_FILE_ENV_PREFIX in executed
    assert commands.command_execution_source(executed) == executed


def test_transfer_inside_substitution_is_hardened_once():
    source = 'x=$(curl -o out.json https://example.invalid); echo "$x"'
    assert commands.command_execution_source(source) == (
        'x=$(' + CURL_FILE_ENV_PREFIX + 'curl -q -o out.json https://example.invalid); echo "$x"')


def test_composed_transfers_run_hardened_beside_an_internal_writer(repo, monkeypatch, tmp_path):
    from lib.project_mod import network_command
    monkeypatch.setattr(network_command, 'prepare_network_command', lambda *a, **kw: None)
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name, first in (('curl', '-q'), ('wget', '--no-config')):
        program = binary / name
        program.write_text(f'#!{sys.executable}\nimport os, sys\n'
                           f'assert sys.argv[1] == {first!r}, sys.argv\n'
                           'assert "SSLKEYLOGFILE" not in os.environ\n'
                           'print("body")\n')
        program.chmod(0o700)
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       SSLKEYLOGFILE=str(repo / 'keys'))
    monkeypatch.setattr(commands, '_get_cmd_env', lambda *a, **kw: dict(environment))
    source = ('curl -s -X POST -d @source https://example.invalid | cat; '
              'wget -qO- https://example.invalid | cat')
    with _internal_writer(repo):
        result = commands.tool_run_command(str(repo), source)
    assert '[exit code: 0]' in result, result
    assert result.count('body') == 2
    assert not (repo / 'keys').exists()


@pytest.mark.parametrize('source', SAFE)
def test_complete_probes_get_a_stable_plan(source):
    planned = prepare_network_probe(source)
    assert planned is not None
    assert prepare_network_probe(planned) == planned


@pytest.mark.parametrize('source', UNSAFE)
def test_unknown_or_writing_commands_get_no_probe_plan(source):
    assert prepare_network_probe(source) is None


@pytest.mark.parametrize('route', ['runner', 'dispatch', 'recorded'])
@pytest.mark.parametrize('source', [PROBE, SAFE[1], SAFE[7]])
def test_probe_runs_beside_an_internal_writer_without_lease_or_source_rewrite(
        repo, monkeypatch, route, source):
    from lib.project_mod import network_command
    monkeypatch.setattr(network_command, 'prepare_network_command', lambda *a, **kw: None)

    for name in ('SSLKEYLOGFILE', 'QLOGDIR', 'CURL_MEMDEBUG'):
        monkeypatch.setenv(name, str(repo / 'hidden-output'))
    observed = []

    def process(command, executed, *args, **kwargs):
        assert not workspace_lease_fds()

        assert not any(name in kwargs['child_env']
                       for name in ('SSLKEYLOGFILE', 'QLOGDIR', 'CURL_MEMDEBUG'))
        assert 'curl -q ' in executed
        observed.append((command, executed))
        return '$ original\n[exit code: 0]'

    monkeypatch.setattr(commands, '_run_command_simple', process)
    with _internal_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), source)
        elif route == 'dispatch':
            result = tools.execute_tool('run_command', {'command': source}, str(repo))
        else:
            wrapped = '( ' + source + '\n) > /dev/null'
            binding = RecordedCommandSource(source, wrapped)
            with tool_admission_scope(command_source=binding):
                result = commands.tool_run_command(str(repo), wrapped)
    assert '[exit code: 0]' in result
    assert len(observed) == 1
    assert observed[0][0] == (wrapped if route == 'recorded' else source)


def test_shell_prefix_and_recorded_wrapper_drift_get_no_probe_plan(repo, monkeypatch):
    source = SAFE[1]
    monkeypatch.setattr(commands, 'SHELL_PREFIX', 'touch target;')
    assert commands._network_probe_execution_source(source) is None
    monkeypatch.setattr(commands, 'SHELL_PREFIX', '')
    with tool_admission_scope(command_source=RecordedCommandSource(source, source)):
        with pytest.raises(ValueError, match='wrapper changed'):
            commands.tool_run_command(str(repo), source + '; touch target')
    assert (repo / 'target').read_text() == 'old\n'
    wrapped = source + '; ' + source
    with tool_admission_scope(command_source=RecordedCommandSource(source, wrapped)):
        assert commands._network_probe_execution_source(wrapped) is None


def test_parser_bounds_give_no_probe_plan():
    source = SAFE[1] + ' ' * 16_384
    assert prepare_network_probe(source) is None
    assert prepare_network_probe('curl ' + ' '.join(['-s'] * 256) + ' https://example.invalid') is None
    nested = SAFE[1]
    for _ in range(10):
        nested = 'code=$(' + nested + ')'
    assert prepare_network_probe(nested) is None


def test_real_curl_does_not_load_hidden_output_config_or_replay_probe(repo, tmp_path):
    curl = shutil.which('curl')
    if not curl:
        pytest.skip('curl not installed')
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(204)
            self.end_headers()
        def log_message(self, *args):
            pass  # Test-owned server has no external logging sink.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    home = tmp_path / 'home'
    home.mkdir()
    hidden = repo / 'hidden-output'
    (home / '.curlrc').write_text('output = ' + str(hidden) + '\n')
    source = ('for p in a b; do code=$(curl -sS --noproxy "*" -o /dev/null '
              '-w "%{http_code}" "http://127.0.0.1:' + str(server.server_port) + '/$p"); '
              'echo "$p -> $code"; done')
    try:
        planned = prepare_network_probe(source)
        assert planned is not None
        environment = {'PATH': os.path.dirname(curl) + os.pathsep + '/usr/bin:/bin',
                       'HOME': str(home), 'CURL_HOME': str(home)}
        with _internal_writer(repo):
            result = subprocess.run(planned, shell=True, cwd=repo, env=environment,
                                    capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ['a -> 204', 'b -> 204']
        assert requests == ['/a', '/b']
        assert not hidden.exists()
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
        assert not worker.is_alive()
