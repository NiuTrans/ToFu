"""tests/test_run_net.py — run_command network layer (lib/project_mod/run_net.py).

Covers: command-shape detection (tools/subcommands/URLs/exemptions),
pre-exec route injection (direct pin → no_proxy, pool pin → proxy vars,
mirror injection when upstream is bad or preferred), post-exec outcome
feed (success + network failure), the [network diagnosis] block
(category, route health, repin hint, mirror hint, credential safety),
non-network failures staying untouched, and both kill switches.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_run_net.py -v
"""
from __future__ import annotations

import pytest

import lib.netpath as netpath
import lib.netmirrors as netmirrors
import lib.proxy as lib_proxy
from lib.project_mod import run_net
from lib.project_mod.run_command import tool_run_command, _get_cmd_env

pytestmark = pytest.mark.unit

PROXY_ENV_VARS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')
_POOL_ENTRY = {
    'id': 'hk-gw', 'name': 'HK GW',
    'url': 'http://hk-gw.invalid:8080', 'scope': 'global', 'enabled': True,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_NETCMD', 'on')
    monkeypatch.delenv('TOFU_NETCMD_INJECT', raising=False)
    monkeypatch.setenv('TOFU_NETPATH', 'on')
    monkeypatch.setenv('TOFU_NETMIRRORS', 'on')
    for var in PROXY_ENV_VARS:
        monkeypatch.setenv(var, 'http://netpath-test-proxy.invalid:3128')
    monkeypatch.setattr(netpath, '_STORE_PATH',
                        str(tmp_path / 'netpath.json'))
    monkeypatch.setattr(netmirrors, '_STORE_PATH',
                        str(tmp_path / 'netpath_mirrors.json'))
    netpath.reset_for_test()
    netmirrors.reset_for_test()
    lib_proxy.set_proxy_pool([])
    yield
    netpath.reset_for_test()
    netmirrors.reset_for_test()
    lib_proxy.set_proxy_pool([])
    lib_proxy.set_bypass_domains([])


def _feed(host, path, ok=True, lat=100.0, n=1):
    url = 'https://%s/' % host
    netpath.note_url(url)      # outcomes drop for untracked hosts
    for _ in range(n):
        netpath.report_outcome(url, ok, lat if ok else None, path=path)


# ═════════════════════════════════════════════════════════════
#  Detection
# ═════════════════════════════════════════════════════════════

class TestDetection:
    @pytest.mark.parametrize('command', [
        'pytest -q',
        'ls -la',
        'grep -rn x lib/',
        'python3 -c "print(1)"',
        'git status',
        'git log --oneline',
        'pip list',
        'npm --version',
        'curl',                          # no target, no plan
    ])
    def test_non_network_commands_ignored(self, command):
        assert run_net.plan(command) is None

    @pytest.mark.parametrize('command,hosts', [
        ('curl -sS https://api.example.com/v1', ['api.example.com']),
        ('wget https://dl.example.com/x.tgz -O /tmp/x',
         ['dl.example.com']),
        ('git clone https://github.com/a/b.git', ['github.com']),
        ('pip install requests', ['pypi.org', 'files.pythonhosted.org']),
        ('pip3 install -r requirements.txt',
         ['pypi.org', 'files.pythonhosted.org']),
        ('npm install', ['registry.npmjs.org']),
        ('conda install numpy', ['conda.anaconda.org',
                                 'repo.anaconda.com']),
        ('FOO=1 sudo pip install x', ['pypi.org', 'files.pythonhosted.org']),
        ('echo ok && pip install x', ['pypi.org', 'files.pythonhosted.org']),
        ('pip install x | tee log.txt', ['pypi.org', 'files.pythonhosted.org']),
    ])
    def test_network_commands_planned(self, command, hosts):
        p = run_net.plan(command)
        assert p is not None, command
        for host in hosts:
            assert host in p.hosts, (command, host)

    def test_exempt_hosts_never_planned(self):
        assert run_net.plan('curl http://127.0.0.1:8080/x') is None
        assert run_net.plan('curl http://localhost:11434/v1') is None
        p = run_net.plan('curl https://a.example.com http://10.0.0.1/y')
        assert list(p.hosts) == ['a.example.com']


# ═════════════════════════════════════════════════════════════
#  Pre-exec injection
# ═════════════════════════════════════════════════════════════

class TestInjection:
    def test_no_opinion_no_injection(self):
        p = run_net.plan('curl https://fresh.example.com/x')
        assert p is not None
        assert p.hosts == {'fresh.example.com': None}
        assert run_net.env_overlay(p) is None

    def test_direct_pin_adds_no_proxy(self):
        _feed('pin-direct.example.com', 'direct', lat=100, n=2)
        _feed('pin-direct.example.com', 'env', lat=300, n=2)
        p = run_net.plan('curl https://pin-direct.example.com/x')
        overlay = run_net.env_overlay(p)
        assert 'pin-direct.example.com' in overlay['no_proxy']
        assert overlay['NO_PROXY'] == overlay['no_proxy']
        # The env proxy stays in place for the command's OTHER hosts.
        assert 'http_proxy' not in overlay

    def test_pool_pin_sets_proxy_vars(self):
        lib_proxy.set_proxy_pool([dict(_POOL_ENTRY)])
        _feed('pin-pool.example.com', 'pool:hk-gw', lat=50, n=2)
        _feed('pin-pool.example.com', 'env', lat=300, n=2)
        _feed('pin-pool.example.com', 'direct', lat=400, n=2)
        p = run_net.plan('curl https://pin-pool.example.com/x')
        overlay = run_net.env_overlay(p)
        assert overlay['http_proxy'] == 'http://hk-gw.invalid:8080'
        assert overlay['HTTPS_PROXY'] == 'http://hk-gw.invalid:8080'

    def test_inject_kill_switch(self, monkeypatch):
        monkeypatch.setenv('TOFU_NETCMD_INJECT', '0')
        _feed('no-inj.example.com', 'direct', lat=100, n=2)
        _feed('no-inj.example.com', 'env', lat=300, n=2)
        p = run_net.plan('curl https://no-inj.example.com/x')
        assert p is not None and p.hosts['no-inj.example.com'] == 'direct'
        assert run_net.env_overlay(p) is None   # learning stays on

    def test_mirror_injected_when_upstream_bad(self):
        # Every route to pypi.org fails twice → upstream bad.
        _feed('pypi.org', 'direct', ok=False, n=2)
        _feed('pypi.org', 'env', ok=False, n=2)
        p = run_net.plan('pip install requests')
        assert p is not None
        assert p.mirror_uses.get('pypi')
        overlay = run_net.env_overlay(p)
        assert overlay['PIP_INDEX_URL'].startswith('https://')

    def test_mirror_preferred_injected_even_when_healthy(self):
        netmirrors.upsert({'id': 'pypi-corp', 'ecosystem': 'pypi',
                           'url': 'https://corp-mirror.example.com/simple',
                           'preferred': True})
        p = run_net.plan('pip install requests')
        assert p.mirror_uses['pypi'] == 'pypi-corp'
        overlay = run_net.env_overlay(p)
        assert overlay['PIP_INDEX_URL'] == (
            'https://corp-mirror.example.com/simple')

    def test_mirror_not_injected_while_upstream_healthy(self):
        _feed('pypi.org', 'direct', lat=100, n=2)
        p = run_net.plan('pip install requests')
        assert not p.mirror_uses


# ═════════════════════════════════════════════════════════════
#  Outcome feed + diagnosis
# ═════════════════════════════════════════════════════════════

def _result(command, body, code):
    return '$ %s\n%s\n[exit code: %d]' % (command, body, code)


class TestFinalize:
    def test_success_feeds_outcome_and_stays_clean(self):
        p = run_net.plan('curl https://ok-feed.example.com/x')
        out = run_net.finalize(
            p, 'curl https://ok-feed.example.com/x',
            _result('curl …', 'hello', 0), 55.0)
        assert '[network diagnosis]' not in out
        st = netpath.host_status('ok-feed.example.com')
        assert st['routes']['env']['ms'] == 55.0

    def test_network_failure_feeds_and_diagnoses(self):
        p = run_net.plan('curl https://blocked.example.com/x')
        body = 'curl: (6) Could not resolve host: blocked.example.com'
        out = run_net.finalize(
            p, 'curl https://blocked.example.com/x',
            _result('curl …', body, 6), 40.0)
        assert '[network diagnosis]' in out
        assert 'DNS resolution failed' in out
        assert 'blocked.example.com' in out
        # The failure was attributed to the effective route (env default).
        st = netpath.host_status('blocked.example.com')
        assert st['routes']['env']['fails'] == 1
        # A mirror-less ecosystem (generic curl) shows no PIP hint.
        assert 'PIP_INDEX_URL' not in out

    def test_403_diagnosis_offers_mirror_for_pip(self):
        p = run_net.plan('pip install requests')
        body = 'ERROR: HTTP/1.1 403 Forbidden'
        out = run_net.finalize(p, 'pip install requests',
                               _result('pip …', body, 1), 30.0)
        assert '[network diagnosis]' in out
        assert 'HTTP 403' in out
        assert 'PIP_INDEX_URL=' in out      # concrete mirror escape hatch

    def test_repin_hint_after_failover(self):
        # env is pinned but dies on this command: after two bad marks the
        # scorer must repin, and the block must SAY so.
        _feed('repin.example.com', 'env', lat=100, n=2)
        _feed('repin.example.com', 'direct', lat=200, n=2)
        _feed('repin.example.com', 'env', ok=False, n=1)
        p = run_net.plan('curl https://repin.example.com/x')
        assert p.hosts['repin.example.com'] == 'env'
        body = 'curl: (7) Failed to connect (via proxy)'
        out = run_net.finalize(p, 'curl https://repin.example.com/x',
                               _result('curl …', body, 7), 30.0)
        assert 'repinned' in out
        assert 'direct' in out

    def test_non_network_failure_untouched(self):
        p = run_net.plan('pip install requests')
        body = 'ERROR: No matching distribution found for requests'
        out = run_net.finalize(p, 'pip install requests',
                               _result('pip …', body, 1), 30.0)
        assert '[network diagnosis]' not in out
        # …and nothing was fed to the scorer either.
        st = netpath.host_status('pypi.org')
        assert st is None or all(not v['fails']
                                 for v in st['routes'].values())

    def test_abort_and_interrupt_untouched(self):
        p = run_net.plan('curl https://abort.example.com/x')
        for marker in ('[Command aborted by user]',
                       '[Command interrupted by user]'):
            out = run_net.finalize(
                p, 'curl https://abort.example.com/x',
                '$ curl …\npartial\n%s\n[exit code: -1]' % marker, 10.0)
            assert '[network diagnosis]' not in out

    def test_407_diagnosis_points_at_credentials(self):
        p = run_net.plan('curl https://auth-fail.example.com/x')
        body = 'curl: (56) Received HTTP code 407 from proxy'
        out = run_net.finalize(p, 'curl https://auth-fail.example.com/x',
                               _result('curl …', body, 56), 20.0)
        assert '407' in out
        assert 'credential' in out

    def test_diagnosis_never_leaks_proxy_urls(self):
        lib_proxy.set_proxy_pool([dict(_POOL_ENTRY)])
        _feed('leak.example.com', 'pool:hk-gw', lat=50, n=2)
        _feed('leak.example.com', 'env', lat=300, n=2)
        p = run_net.plan('curl https://leak.example.com/x')
        out = run_net.finalize(p, 'curl https://leak.example.com/x',
                               _result('curl …', '403 Forbidden', 7), 20.0)
        assert '[network diagnosis]' in out
        assert 'hk-gw.invalid' not in out       # no proxy URL…
        assert 'proxy HK GW' in out             # …only its label

    def test_master_switch(self, monkeypatch):
        monkeypatch.setenv('TOFU_NETCMD', '0')
        assert run_net.plan('curl https://off.example.com/x') is None


# ═════════════════════════════════════════════════════════════
#  Wiring: _get_cmd_env overlay + tool_run_command end to end
# ═════════════════════════════════════════════════════════════

class TestWiring:
    def test_get_cmd_env_applies_overlay(self, monkeypatch):
        monkeypatch.setenv('RUNNET_KEEP', 'x')
        env = _get_cmd_env(net_overlay={'RUNNET_ADD': '1',
                                        'RUNNET_KEEP': None})
        assert env['RUNNET_ADD'] == '1'
        assert 'RUNNET_KEEP' not in env

    def test_get_cmd_env_default_no_overlay(self):
        env = _get_cmd_env()
        assert 'PIP_INDEX_URL' not in env

    def test_tool_run_command_appends_diagnosis_on_network_failure(
            self, tmp_path):
        # The child inherits the (bogus) pinned env proxy; .invalid DNS
        # fails fast — a genuine network-class failure end to end.
        result = tool_run_command(
            str(tmp_path),
            'curl -sS --max-time 8 http://runnet-e2e.invalid/path')
        assert '[network diagnosis]' in result
        assert 'runnet-e2e.invalid' in result

    def test_tool_run_command_success_untouched(self, tmp_path):
        result = tool_run_command(str(tmp_path), 'echo hello-net')
        assert 'hello-net' in result
        assert '[network diagnosis]' not in result

    def test_tool_run_command_injects_no_proxy_for_direct_pin(
            self, tmp_path):
        _feed('runnet-direct.invalid', 'direct', lat=100, n=2)
        _feed('runnet-direct.invalid', 'env', lat=300, n=2)
        result = tool_run_command(
            str(tmp_path),
            "curl -sS --max-time 8 http://runnet-direct.invalid/ ; "
            "python3 -c \"import os; print('NP=' + os.environ.get("
            "'no_proxy', ''))\"")
        # The pin forced the host into the child's no_proxy.
        assert 'runnet-direct.invalid' in result.split('NP=')[-1]
        # Overall exit is python's 0 (curl's failure is masked by `;`),
        # so no diagnosis block — the route still did its job.
        assert '[network diagnosis]' not in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
