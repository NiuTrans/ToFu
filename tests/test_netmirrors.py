"""tests/test_netmirrors.py — package-mirror registry (lib/netmirrors.py).

Covers: builtin seeds, upsert/get/remove/set_enabled + persistence,
health (EWMA, failure cooldown), best() selection (preferred wins,
latency order, cooling skipped), env_overlay shapes per ecosystem,
TOFU_NETMIRRORS_JSON, and the master switch.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_netmirrors.py -v
"""
from __future__ import annotations

import json

import pytest

import lib.netmirrors as netmirrors

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_NETMIRRORS', 'on')
    monkeypatch.delenv('TOFU_NETMIRRORS_JSON', raising=False)
    monkeypatch.setattr(netmirrors, '_STORE_PATH',
                        str(tmp_path / 'netpath_mirrors.json'))
    netmirrors.reset_for_test()
    yield
    netmirrors.reset_for_test()


class TestRegistry:
    def test_builtins_seeded(self):
        entries = netmirrors.entries()
        assert len(entries) == len(netmirrors._BUILTINS)
        pypi = netmirrors.entries('pypi')
        assert {e['id'] for e in pypi} == {'pypi-tuna', 'pypi-ustc',
                                           'pypi-aliyun'}
        # No corporate infrastructure may ship in the repo seeds.
        for e in entries:
            assert 'sankuai' not in e['url']
            assert 'meituan' not in e['url']

    def test_upsert_get_remove(self):
        e = netmirrors.upsert({'ecosystem': 'pypi',
                               'url': 'https://mirror.example.com/simple',
                               'label': 'Corp', 'preferred': True})
        assert e is not None and e['id']
        got = netmirrors.get(e['id'])
        assert got['preferred'] is True
        assert got['label'] == 'Corp'
        assert netmirrors.remove(e['id']) is True
        assert netmirrors.get(e['id']) is None

    def test_upsert_rejects_malformed(self):
        assert netmirrors.upsert({'ecosystem': 'rubygems',
                                  'url': 'https://x.com'}) is None
        assert netmirrors.upsert({'ecosystem': 'pypi',
                                  'url': 'ftp://x.com'}) is None
        assert netmirrors.upsert({'ecosystem': 'pypi', 'url': ''}) is None

    def test_config_persists_across_reload(self, tmp_path, monkeypatch):
        netmirrors.upsert({'id': 'pypi-corp', 'ecosystem': 'pypi',
                           'url': 'https://corp.example.com/simple'})
        netmirrors.report_outcome('pypi-corp', True, 88.0)
        netmirrors._save()
        # Simulate a restart: wipe in-memory state, reload from disk.
        monkeypatch.setattr(netmirrors, '_loaded', False)
        with netmirrors._lock:
            netmirrors._entries.clear()
        e = netmirrors.get('pypi-corp')
        assert e is not None
        assert e['ewma_ms'] == 88.0       # health survived the restart

    def test_set_enabled(self):
        assert netmirrors.set_enabled('pypi-tuna', False) is True
        assert netmirrors.get('pypi-tuna')['enabled'] is False
        assert netmirrors.set_enabled('nope', True) is False

    def test_env_json_entries(self, monkeypatch):
        monkeypatch.setattr(netmirrors, '_loaded', False)
        with netmirrors._lock:
            netmirrors._entries.clear()
        monkeypatch.setenv('TOFU_NETMIRRORS_JSON', json.dumps([
            {'id': 'pypi-env', 'ecosystem': 'pypi',
             'url': 'https://env-mirror.example.com/simple'}]))
        assert netmirrors.get('pypi-env') is not None


class TestHealthAndBest:
    def test_report_outcome_ewma_and_cooldown(self):
        netmirrors.report_outcome('pypi-tuna', True, 100.0)
        netmirrors.report_outcome('pypi-tuna', True, 50.0)
        assert netmirrors.get('pypi-tuna')['ewma_ms'] == pytest.approx(85.0)
        netmirrors.report_outcome('pypi-tuna', False)
        assert netmirrors.get('pypi-tuna')['fails'] == 1
        netmirrors.report_outcome('pypi-tuna', False)
        e = netmirrors.get('pypi-tuna')
        assert e['fails'] == 2
        entries = {x['id']: x for x in netmirrors.entries('pypi')}
        assert entries['pypi-tuna']['health']['cooling'] is True
        # Cooling entries are skipped by best().
        assert netmirrors.best('pypi')['id'] != 'pypi-tuna'

    def test_best_prefers_measured_fast(self):
        netmirrors.report_outcome('pypi-aliyun', True, 20.0)
        netmirrors.report_outcome('pypi-ustc', True, 300.0)
        assert netmirrors.best('pypi')['id'] == 'pypi-aliyun'

    def test_preferred_wins_over_latency(self):
        netmirrors.report_outcome('pypi-aliyun', True, 20.0)
        netmirrors.upsert({'id': 'pypi-corp', 'ecosystem': 'pypi',
                           'url': 'https://corp.example.com/simple',
                           'preferred': True})
        assert netmirrors.best('pypi')['id'] == 'pypi-corp'

    def test_best_none_when_all_cooling(self):
        for e in netmirrors.entries('npm'):
            netmirrors.report_outcome(e['id'], False)
            netmirrors.report_outcome(e['id'], False)
        assert netmirrors.best('npm') is None

    def test_disabled_switch(self, monkeypatch):
        monkeypatch.setenv('TOFU_NETMIRRORS', 'off')
        assert netmirrors.best('pypi') is None


class TestEnvOverlay:
    def test_pypi_https(self):
        entry = netmirrors.get('pypi-tuna')
        assert netmirrors.env_overlay('pypi', entry) == {
            'PIP_INDEX_URL': 'https://pypi.tuna.tsinghua.edu.cn/simple'}

    def test_pypi_http_adds_trusted_host(self):
        e = netmirrors.upsert({'id': 'pypi-http', 'ecosystem': 'pypi',
                               'url': 'http://mirror.internal:8081/simple'})
        assert netmirrors.env_overlay('pypi', e) == {
            'PIP_INDEX_URL': 'http://mirror.internal:8081/simple',
            'PIP_TRUSTED_HOST': 'mirror.internal'}

    def test_npm_and_conda(self):
        npm = netmirrors.get('npm-npmmirror')
        assert netmirrors.env_overlay('npm', npm) == {
            'npm_config_registry': 'https://registry.npmmirror.com'}
        conda = netmirrors.get('conda-tuna')
        overlay = netmirrors.env_overlay('conda', conda)
        assert overlay['CONDA_CHANNELS'].endswith('/conda-forge')

    def test_github_has_no_overlay(self):
        assert netmirrors.env_overlay('github', {'url': 'https://x.com'}) \
            == {}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
