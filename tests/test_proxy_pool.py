"""tests/test_proxy_pool.py — 代理池（有序/分域/健康追踪）守卫套件（2026-08-07, epic pt_bb2389f3）。

Pins the owner-directed contracts:

  * sanitize：URL userinfo → vault 拆分（URL 永不带凭证落盘）、scope 校验、
    认证与 scope 正交、id 派生/去重、上限。
  * proxies_for：订阅主机吃订阅代理（显式 URL）、非订阅主机永不进订阅代理、
    bypass 主机永远 _NO_PROXY、global 行服务任意主机、空池 = legacy env 行为
    （逐字节不变）、disabled 行跳过。
  * 健康评分：连续失败 ×2 → 冷却 → 故障转移到下一行；冷却到期复活；成功清零；
    凭证不可解析的行内联失败并转移。
  * egress 集成：池探测按序、任一 ok → direct（无需 agent）、全灭 → agent 兜底；
    set_proxy_pool 立即失效探测缓存（_probe_cache 300s 陈旧 verdict bug 的回归针）。
  * 路由：POST /api/v1/server-config 持久化池（文件无 userinfo）、vault 凭证
    随行创建、删行清凭证、legacy proxy_config 退役；proxy-test 端点分类
    ok/geo_blocked/network_fail 且不回显凭证。

Failure-first：池特性前 sanitize_proxy_pool/set_proxy_pool 不存在。
"""

from __future__ import annotations

import json
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

import lib.proxy as proxy
from lib.desktop import egress
from lib.subscription_routes import ProbeResult, manager as route_manager


@pytest.fixture(autouse=True)
def _isolated_pool():
    """Snapshot/restore the pool module state (process-global) — sibling
    suites must never see this suite's entries/health/credential cache."""
    saved = (
        list(proxy._proxy_pool),
        dict(proxy._pool_health),
        dict(proxy._pool_choice),
        dict(proxy._cred_cache),
        dict(proxy._reach_probe_cache),
        proxy._proxy_topology_epoch,
    )
    proxy._proxy_pool = []
    proxy._pool_health = {}
    proxy._pool_choice = {}
    proxy._cred_cache = {}
    proxy._reach_probe_cache = {}
    saved_probe = dict(egress._probe_cache._data)
    saved_network_fail = dict(egress._probe_network_fail_cache._data)
    egress._probe_cache.clear()
    egress._probe_network_fail_cache.clear()
    route_manager.reset()
    yield proxy
    proxy._proxy_pool, proxy._pool_health = saved[0], saved[1]
    proxy._pool_choice, proxy._cred_cache = saved[2], saved[3]
    proxy._reach_probe_cache = saved[4]
    proxy._proxy_topology_epoch = saved[5]
    with egress._probe_cache._lock:
        egress._probe_cache._data.clear()
        egress._probe_cache._data.update(saved_probe)
    with egress._probe_network_fail_cache._lock:
        egress._probe_network_fail_cache._data.clear()
        egress._probe_network_fail_cache._data.update(saved_network_fail)
    route_manager.reset()


def _entry(eid='hk', scope='subscription', url='http://gw.example.com:8080',
           enabled=True, credential_vault=''):
    e = {'id': eid, 'name': eid, 'url': url, 'scope': scope,
         'enabled': enabled}
    if credential_vault:
        e['credential_vault'] = credential_vault
    return e


# ══════════════════════════════════════════════════════
#  1. sanitize
# ══════════════════════════════════════════════════════

class TestSanitize(unittest.TestCase):

    def test_userinfo_split_to_creds_and_stripped_url(self):
        entries, creds, err = proxy.sanitize_proxy_pool([{
            'name': 'HK 网关',
            'url': 'http://user%40corp:p%3Ass@g-hk.example.com:8080',
            'scope': 'subscription',
        }])
        self.assertEqual(err, '')
        self.assertEqual(entries[0]['url'], 'http://g-hk.example.com:8080')
        self.assertEqual(entries[0]['credential_vault'], 'proxy_hk_auth')
        # URL-decoded credential rides the vault map, never the entry.
        self.assertEqual(creds, {'hk': 'user@corp:p:ss'})
        self.assertNotIn('@', entries[0]['url'])

    def test_password_with_colon_preserved(self):
        entries, creds, err = proxy.sanitize_proxy_pool([{
            'url': 'http://u:p:a@gw.example.com:8080', 'scope': 'subscription',
        }])
        self.assertEqual(err, '')
        self.assertEqual(creds[entries[0]['id']], 'u:p:a')

    def test_credentialed_global_accepted(self):
        entries, creds, err = proxy.sanitize_proxy_pool([{
            'url': 'http://u:p@gw.example.com:8080', 'scope': 'global',
        }])
        self.assertEqual(err, '')
        self.assertEqual(entries[0]['scope'], 'global')
        self.assertEqual(creds[entries[0]['id']], 'u:p')

    def test_existing_vault_ref_with_global_scope_accepted(self):
        entries, creds, err = proxy.sanitize_proxy_pool([{
            'url': 'http://gw.example.com:8080', 'scope': 'global',
            'credential_vault': 'proxy_x_auth',
        }])
        self.assertEqual(err, '')
        self.assertEqual(creds, {})
        self.assertEqual(entries[0]['credential_vault'], 'proxy_x_auth')

    def test_bad_scheme_rejected(self):
        for url in ('socks5://gw.example.com:1080', 'ftp://gw.example.com'):
            _e, _c, err = proxy.sanitize_proxy_pool(
                [{'url': url, 'scope': 'subscription'}])
            self.assertIn('scheme', err, url)

    def test_schemeless_url_defaults_to_http(self):
        entries, _c, err = proxy.sanitize_proxy_pool(
            [{'url': 'gw.example.com:8080', 'scope': 'global'}])
        self.assertEqual(err, '')
        self.assertEqual(entries[0]['url'], 'http://gw.example.com:8080')

    def test_bad_port_rejected(self):
        _e, _c, err = proxy.sanitize_proxy_pool(
            [{'url': 'http://gw.example.com:notaport', 'scope': 'global'}])
        self.assertIn('unparseable', err)

    def test_bad_scope_rejected(self):
        _e, _c, err = proxy.sanitize_proxy_pool(
            [{'url': 'http://gw.example.com:8080', 'scope': 'everything'}])
        self.assertIn('scope', err)

    def test_overlong_pool_rejected(self):
        raw = [{'url': 'http://gw.example.com:8080', 'scope': 'global'}
               for _ in range(17)]
        _e, _c, err = proxy.sanitize_proxy_pool(raw)
        self.assertIn('at most', err)

    def test_id_minted_from_name_and_deduped(self):
        entries, _c, err = proxy.sanitize_proxy_pool([
            {'name': 'HK GW', 'url': 'http://a.example.com:8080',
             'scope': 'global'},
            {'name': 'HK GW', 'url': 'http://b.example.com:8080',
             'scope': 'global'},
        ])
        self.assertEqual(err, '')
        self.assertEqual(entries[0]['id'], 'hk-gw')
        self.assertEqual(entries[1]['id'], 'hk-gw-2')

    def test_existing_vault_ref_roundtrips_without_new_credential(self):
        entries, creds, err = proxy.sanitize_proxy_pool([{
            'id': 'hk', 'url': 'http://gw.example.com:8080',
            'scope': 'subscription', 'credential_vault': 'proxy_hk_auth',
        }])
        self.assertEqual(err, '')
        self.assertEqual(creds, {})
        self.assertEqual(entries[0]['credential_vault'], 'proxy_hk_auth')

    def test_enabled_defaults_true_and_false_honoured(self):
        entries, _c, err = proxy.sanitize_proxy_pool([
            {'url': 'http://a.example.com:8080', 'scope': 'global'},
            {'url': 'http://b.example.com:8080', 'scope': 'global',
             'enabled': False},
        ])
        self.assertEqual(err, '')
        self.assertTrue(entries[0]['enabled'])
        self.assertFalse(entries[1]['enabled'])


# ══════════════════════════════════════════════════════
#  2. proxies_for 路由 + scope 隔离
# ══════════════════════════════════════════════════════

class TestProxiesFor(unittest.TestCase):

    def test_empty_pool_is_legacy_byte_for_byte(self):
        self.assertEqual(proxy.proxies_for('https://auth.openai.com/oauth/token'), {})
        self.assertEqual(proxy.proxies_for('https://example.com/x'), {})

    def test_subscription_host_gets_explicit_pool_url(self):
        proxy.set_proxy_pool([_entry('hk')])
        out = proxy.proxies_for('https://auth.openai.com/oauth/token')
        self.assertEqual(out, {'http': 'http://gw.example.com:8080',
                               'https': 'http://gw.example.com:8080'})

    def test_non_subscription_host_never_sees_subscription_proxy(self):
        proxy.set_proxy_pool([_entry('hk')])
        self.assertEqual(proxy.proxies_for('https://example.com/x'), {})

    def test_bypass_host_always_direct_even_with_pool(self):
        proxy.set_proxy_pool([_entry('hk'), _entry('g1', scope='global')])
        self.assertEqual(proxy.proxies_for('http://localhost:8317/v1'),
                         {'no_proxy': '*'})

    def test_global_entry_serves_arbitrary_host(self):
        proxy.set_proxy_pool([_entry('g1', scope='global')])
        out = proxy.proxies_for('https://example.com/x')
        self.assertEqual(out['https'], 'http://gw.example.com:8080')

    def test_subscription_host_prefers_subscription_then_global(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global', url='http://global.example.com:3128'),
            _entry('hk'),
        ])
        out = proxy.proxies_for('https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(out['https'], 'http://gw.example.com:8080')

    def test_disabled_entry_skipped(self):
        proxy.set_proxy_pool([
            _entry('hk', enabled=False),
            _entry('hk2', url='http://gw2.example.com:8080'),
        ])
        out = proxy.proxies_for('https://auth.openai.com/oauth/token')
        self.assertEqual(out['https'], 'http://gw2.example.com:8080')

    def test_choice_attributed_for_outcome_reporting(self):
        proxy.set_proxy_pool([_entry('hk')])
        proxy.proxies_for('https://auth.openai.com/oauth/token')
        self.assertEqual(proxy._pool_choice.get('auth.openai.com'), 'hk')


# ══════════════════════════════════════════════════════
#  3. 健康评分与故障转移
# ══════════════════════════════════════════════════════

class TestHealthFailover(unittest.TestCase):

    def _two(self):
        proxy.set_proxy_pool([
            _entry('hk'),
            _entry('backup', url='http://gw2.example.com:8080'),
        ])

    def test_two_consecutive_failures_fail_over(self):
        self._two()
        url = 'https://auth.openai.com/oauth/token'
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw.example.com:8080')
        proxy.report_outcome(url, False)
        # 第一次失败不转移（抗抖动）
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw.example.com:8080')
        proxy.report_outcome(url, False)
        # 连续第二次失败 → 冷却 → 下一行
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw2.example.com:8080')

    def test_cooldown_expiry_revives_first_entry(self):
        self._two()
        proxy.pool_note_outcome('hk', False)
        proxy.pool_note_outcome('hk', False)
        url = 'https://auth.openai.com/oauth/token'
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw2.example.com:8080')
        # 冷却到期（直接拨健康状态时钟，不 sleep）
        proxy._pool_health['hk']['cooldown_until'] = time.monotonic() - 1
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw.example.com:8080')

    def test_success_resets_failures(self):
        self._two()
        proxy.pool_note_outcome('hk', False)
        proxy.pool_note_outcome('hk', True, 120.0)
        proxy.pool_note_outcome('hk', False)
        url = 'https://auth.openai.com/oauth/token'
        # 一次失败后仍用首行（fail count 被成功清零过，未达阈值）
        self.assertEqual(proxy.proxies_for(url)['https'],
                         'http://gw.example.com:8080')
        self.assertEqual(proxy._pool_health['hk']['ewma_ms'], 120.0)

    def test_route_description_is_safe_and_names_concrete_pool(self):
        proxy.set_proxy_pool([_entry(
            'g1', scope='global',
            url='http://user:secret@gw.example.com:8080')])
        url = 'https://models.example.net/v1/chat/completions'
        decision = proxy.proxies_for(url)
        metadata = proxy.describe_route(url, proxies=decision)

        self.assertEqual(metadata['routeMode'], 'proxy')
        self.assertEqual(metadata['routeId'], 'pool:g1')
        self.assertEqual(metadata['poolId'], 'g1')
        self.assertNotIn('secret', repr(metadata))
        self.assertNotIn('gw.example.com', repr(metadata))

    def test_explicit_pool_outcome_ignores_racy_host_last_choice(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global',
                   url='http://gw2.example.com:8080'),
        ])
        url = 'https://models.example.net/v1/chat/completions'
        proxy.proxies_for(url)
        proxy._pool_choice['models.example.net'] = 'g2'  # concurrent overwrite

        proxy.report_outcome(url, False, pool_id='g1')

        self.assertEqual(proxy._pool_health['g1']['fails'], 1)
        self.assertNotIn('g2', proxy._pool_health)

    def test_unresolvable_credential_fails_over_inline(self):
        proxy.set_proxy_pool([
            _entry('hk', credential_vault='proxy_hk_auth'),
            _entry('backup', url='http://gw2.example.com:8080'),
        ])
        with mock.patch('lib.credentials_vault.get_entry', return_value=None):
            url = 'https://auth.openai.com/oauth/token'
            out = proxy.proxies_for(url)
        self.assertEqual(out['https'], 'http://gw2.example.com:8080')
        self.assertEqual(proxy._pool_health['hk']['fails'], 1)

    def test_credentialed_entry_resolves_with_vault_secret(self):
        proxy.set_proxy_pool([
            _entry('hk', credential_vault='proxy_hk_auth'),
        ])
        with mock.patch('lib.credentials_vault.get_entry',
                        return_value='user:p%ss') as ge:
            out = proxy.proxies_for('https://auth.openai.com/oauth/token')
        self.assertTrue(ge.called)
        self.assertEqual(
            out['https'], 'http://user:p%25ss@gw.example.com:8080')

    def test_all_unhealthy_falls_back_to_legacy_env(self):
        proxy.set_proxy_pool([_entry('hk')])
        proxy.pool_note_outcome('hk', False)
        proxy.pool_note_outcome('hk', False)
        self.assertEqual(
            proxy.proxies_for('https://auth.openai.com/oauth/token'), {})


# ══════════════════════════════════════════════════════
#  4. async 通道 parity
# ══════════════════════════════════════════════════════

class TestAsyncParity(unittest.TestCase):

    def test_async_returns_pool_url_for_subscription_host(self):
        proxy.set_proxy_pool([_entry('hk')])
        self.assertEqual(proxy.async_proxy_for('https://auth.openai.com/x'),
                         'http://gw.example.com:8080')

    def test_async_bypass_host_is_direct(self):
        proxy.set_proxy_pool([_entry('hk')])
        self.assertIsNone(proxy.async_proxy_for('http://localhost:8317/v1'))

    def test_async_empty_pool_falls_to_env(self):
        with mock.patch.dict('os.environ',
                             {'https_proxy': 'http://env.example.com:3128'},
                             clear=False):
            self.assertEqual(proxy.async_proxy_for('https://auth.openai.com/x'),
                             'http://env.example.com:3128')


# ══════════════════════════════════════════════════════
#  5. egress 集成（池探测 + 缓存失效回归针）
# ══════════════════════════════════════════════════════

class TestEgressIntegration(unittest.TestCase):

    def test_subscription_route_specs_cover_direct_all_pool_and_env(self):
        proxy.set_proxy_pool([
            _entry('global', scope='global',
                   url='http://global.example.com:3128'),
            _entry('hk', url='http://hk.example.com:8080'),
        ])
        env = {'http_proxy': 'http://env.example.com:8888',
               'https_proxy': 'http://env.example.com:8888',
               'HTTP_PROXY': '', 'HTTPS_PROXY': ''}
        with mock.patch.dict('os.environ', env, clear=False), \
             mock.patch('requests.utils.should_bypass_proxies',
                        return_value=False):
            routes = proxy.subscription_route_specs(
                'https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual([route.route_id for route in routes], [
            'direct', 'pool:hk', 'pool:global', 'env'])

    def test_duplicate_env_proxy_is_probed_once(self):
        proxy.set_proxy_pool([_entry('hk')])
        env = {'http_proxy': 'http://gw.example.com:8080',
               'https_proxy': 'http://gw.example.com:8080',
               'HTTP_PROXY': '', 'HTTPS_PROXY': ''}
        with mock.patch.dict('os.environ', env, clear=False):
            routes = proxy.subscription_route_specs(
                'https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual([route.route_id for route in routes],
                         ['direct', 'pool:hk'])

    def test_set_proxy_pool_invalidates_egress_probe_cache(self):
        """The 300s-stale-verdict bug pin: a pool change MUST clear cached
        geo_blocked verdicts immediately."""
        egress._probe_cache.set('auth.openai.com', 'geo_blocked')
        egress._probe_network_fail_cache.set('chatgpt.com', 'network_fail')
        proxy.set_proxy_pool([_entry('hk')])
        self.assertIsNone(egress._probe_cache.get('auth.openai.com'))
        self.assertIsNone(
            egress._probe_network_fail_cache.get('chatgpt.com'))

    def test_pool_probe_order_includes_global_fallback(self):
        proxy.set_proxy_pool([
            _entry('global', scope='global',
                   url='http://global.example.com:3128'),
            _entry('hk', scope='subscription',
                   url='http://hk.example.com:8080'),
        ])
        self.assertEqual(proxy.pool_probe_entries(), [
            ('hk', 'http://hk.example.com:8080'),
            ('global', 'http://global.example.com:3128'),
        ])

    def test_pool_probe_ok_routes_direct_without_agents(self):
        proxy.set_proxy_pool([_entry('hk')])
        seen = []

        def _probe(_url, route):
            seen.append(route.route_id)
            if route.pool_id == 'hk':
                return ProbeResult('ok', 10, 401)
            return ProbeResult('network_fail')

        with mock.patch.object(route_manager, '_probe', side_effect=_probe), \
             mock.patch('lib.desktop.online_agents', return_value=[]):
            out = egress.route_candidates('https://auth.openai.com/oauth/token',
                                          user_id='u1')
        self.assertEqual(out, ['direct'])
        self.assertIn('direct', seen)
        self.assertIn('pool:hk', seen)
        selected = proxy.subscription_route_candidates(
            'https://auth.openai.com/oauth/token')[0]
        self.assertEqual(selected.route_id, 'pool:hk')
        self.assertEqual(selected.requests_proxies(),
                         {'http': 'http://gw.example.com:8080',
                          'https': 'http://gw.example.com:8080'})

    def test_pool_probe_order_then_agent_fallback(self):
        proxy.set_proxy_pool([
            _entry('hk'),
            _entry('backup', url='http://gw2.example.com:8080'),
        ])
        calls = []

        def _probe(_url, route):
            calls.append(route.route_id)
            return ProbeResult('policy_blocked', 10, 403)

        agent = {'agent_id': 'a1', 'name': 'box', 'platform': 'win32',
                 'capabilities': {'egress': True}, 'user_id': '1',
                 'last_seen': time.time()}
        with mock.patch.object(route_manager, '_probe', side_effect=_probe), \
             mock.patch('lib.desktop.online_agents', return_value=[agent]):
            out = egress.route_candidates('https://auth.openai.com/oauth/token',
                                          user_id='1')
        # Direct + every pool row race concurrently; all fail before agent.
        self.assertIn('direct', calls)
        self.assertIn('pool:hk', calls)
        self.assertIn('pool:backup', calls)
        self.assertEqual(out, ['a1'])
        # Target-specific 403 lives only in per-host route health and cannot
        # globally poison these proxies for unrelated traffic.
        health = route_manager.status()['routes']['auth.openai.com']
        self.assertEqual(health['pool:hk']['failure_kind'], 'policy_blocked')
        self.assertEqual(health['pool:backup']['failure_kind'],
                         'policy_blocked')
        self.assertNotIn('hk', proxy._pool_health)
        self.assertNotIn('backup', proxy._pool_health)

    def test_probe_failure_then_success_marks_recovery(self):
        proxy.set_proxy_pool([_entry('hk')])
        proxy.pool_note_outcome('hk', False)
        proxy.pool_note_outcome('hk', False)
        self.assertIn('hk', proxy._pool_health)

        def _probe(_url, route):
            if route.pool_id == 'hk':
                return ProbeResult('ok', 10, 400)
            return ProbeResult('network_fail')

        with mock.patch.object(route_manager, '_probe', side_effect=_probe), \
             mock.patch('lib.desktop.online_agents', return_value=[]):
            out = egress.route_request('https://auth.openai.com/oauth/token',
                                       user_id='')
        self.assertEqual(out, 'direct')
        self.assertTrue(route_manager.status()['routes']
                        ['auth.openai.com']['pool:hk']['healthy'])


# ══════════════════════════════════════════════════════
#  6. 持久化 + 全局代理解析
# ══════════════════════════════════════════════════════

class TestGlobalProxyUrl(unittest.TestCase):

    def test_first_global_url_and_search_bridge_prefers_it(self):
        proxy.set_proxy_pool([_entry('g1', scope='global')])
        self.assertEqual(proxy.first_global_proxy_url(),
                         'http://gw.example.com:8080')
        from lib.search_bridge import _resolve_proxy_url
        self.assertEqual(_resolve_proxy_url(), 'http://gw.example.com:8080')

    def test_cooling_global_entry_skipped(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        proxy.pool_note_outcome('g1', False)
        proxy.pool_note_outcome('g1', False)
        self.assertEqual(proxy.first_global_proxy_url(),
                         'http://gw2.example.com:8080')

    def test_no_global_entry_falls_back_to_legacy(self):
        from lib.search_bridge import _resolve_proxy_url
        legacy = {'https_proxy': 'http://legacy.example.com:3128',
                  'http_proxy': ''}
        with mock.patch('lib.proxy.get_proxy_config', return_value=legacy):
            self.assertEqual(_resolve_proxy_url(),
                             'http://legacy.example.com:3128')


class TestReachableGlobalProxyUrl(unittest.TestCase):
    """first_reachable_global_proxy_url: pool order + TCP + HTTP-tunnel gates.

    Consumers that inject the URL into a subprocess env (MCP launchers)
    cannot fail over inside the child, so a first-entry-but-useless proxy
    must be skipped up front. Two measured failure shapes, one per gate:
    process/port down (TCP refuse) and allowlist gateway that accepts TCP
    then refuses the CONNECT tunnel (hk-gw vs pypi.org, 2026-08-20).
    """

    def _patched(self, http_ok):
        """Patch TCP alive for everything; HTTP tunnel per *http_ok*."""
        tcp = mock.patch.object(proxy, '_tcp_reachable', return_value=True)
        http = mock.patch.object(
            proxy, '_http_probe_ok',
            side_effect=lambda url, probe, t: http_ok(url))
        return tcp, http

    def test_first_alive_entry_wins(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        tcp, http = self._patched(lambda url: True)
        with tcp, http:
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw.example.com:8080')

    def test_tunnel_refused_first_entry_fails_over(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        tcp, http = self._patched(lambda url: 'gw2' in url)
        with tcp, http:
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw2.example.com:8080')

    def test_all_dead_returns_empty(self):
        proxy.set_proxy_pool([_entry('g1', scope='global')])
        tcp, http = self._patched(lambda url: False)
        with tcp, http:
            self.assertEqual(proxy.first_reachable_global_proxy_url(), '')

    def test_tcp_dead_skipped_without_http_probe(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        http_probed = []
        with mock.patch.object(proxy, '_tcp_reachable',
                               side_effect=lambda url, t: 'gw2' in url), \
             mock.patch.object(proxy, '_http_probe_ok',
                               side_effect=lambda url, probe, t:
                               http_probed.append(url) or True):
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw2.example.com:8080')
        self.assertEqual(http_probed, ['http://gw2.example.com:8080'])

    def test_cooled_entry_skipped_without_probe(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global'),
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        proxy.pool_note_outcome('g1', False)
        proxy.pool_note_outcome('g1', False)
        probed = []
        with mock.patch.object(proxy, '_tcp_reachable',
                               side_effect=lambda url, t: probed.append(url) or True), \
             mock.patch.object(proxy, '_http_probe_ok', return_value=True):
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw2.example.com:8080')
        self.assertEqual(probed, ['http://gw2.example.com:8080'])

    def test_unresolvable_credential_skipped_without_probe(self):
        proxy.set_proxy_pool([
            _entry('g1', scope='global',
                   credential_vault='proxy_g1_auth'),  # never stored → None
            _entry('g2', scope='global', url='http://gw2.example.com:8080'),
        ])
        probed = []
        with mock.patch.object(proxy, '_tcp_reachable',
                               side_effect=lambda url, t: probed.append(url) or True), \
             mock.patch.object(proxy, '_http_probe_ok', return_value=True):
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw2.example.com:8080')
        self.assertEqual(probed, ['http://gw2.example.com:8080'])

    def test_pass_cached_for_ttl_window(self):
        proxy.set_proxy_pool([_entry('g1', scope='global')])
        calls = {'http': 0}
        def _http(url, probe, t):
            calls['http'] += 1
            return True
        with mock.patch.object(proxy, '_tcp_reachable', return_value=True), \
             mock.patch.object(proxy, '_http_probe_ok', side_effect=_http):
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw.example.com:8080')
            self.assertEqual(proxy.first_reachable_global_proxy_url(),
                             'http://gw.example.com:8080')
        self.assertEqual(calls['http'], 1)

    def test_tcp_reachable_real_sockets(self):
        import socket as _socket
        listener = _socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertTrue(
                proxy._tcp_reachable(f'http://127.0.0.1:{port}', 0.5))
        finally:
            listener.close()
        self.assertFalse(
            proxy._tcp_reachable(f'http://127.0.0.1:{port}', 0.5))


# ══════════════════════════════════════════════════════
#  7. 路由：保存 + proxy-test 端点
# ══════════════════════════════════════════════════════

@pytest.fixture()
def _server_config_snapshot():
    """Snapshot/restore the sandboxed server_config.json around route
    tests (the test env's TOFU_DATA_DIR is a per-worker tmp dir; the file
    may legitimately NOT exist yet)."""
    import os
    import routes.config as rc
    path = rc._SERVER_CONFIG_PATH
    saved = None
    if os.path.isfile(path):
        with open(path) as f:
            saved = f.read()
    yield rc
    if saved is None:
        if os.path.isfile(path):
            os.remove(path)
    else:
        with open(path, 'w') as f:
            f.write(saved)
    proxy.set_proxy_pool([])
    try:
        import lib as _lib
        _lib.reload_config()
    except Exception:
        pass


def test_save_pool_persists_sanitized_and_splits_credential(
        flask_client, _server_config_snapshot):
    from lib.credentials_vault import get_entry
    resp = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{
            'name': 'HK', 'url': 'http://user:p%40ss@g-hk.example.com:8080',
            'scope': 'subscription', 'enabled': True,
        }],
    })
    body = resp.get_json()
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert body['ok'] is True
    cfg = json.loads(_server_config_snapshot._SERVER_CONFIG_PATH and
                     open(_server_config_snapshot._SERVER_CONFIG_PATH).read())
    pool = cfg.get('proxy_pool')
    assert pool and len(pool) == 1
    entry = pool[0]
    assert entry['url'] == 'http://g-hk.example.com:8080', entry
    assert '@' not in entry['url'], '凭证绝不允许随 URL 落盘'
    assert entry['credential_vault'] == 'proxy_hk_auth'
    assert entry['scope'] == 'subscription'
    assert 'user:p@ss' not in json.dumps(cfg), '明文凭证泄漏进配置文件'
    # 凭证进了 vault（urldecode 后）
    assert get_entry('proxy_hk_auth') == 'user:p@ss'
    # 运行态已应用
    assert proxy._proxy_pool[0]['id'] == 'hk'
    from lib.credentials_vault import delete_entry
    delete_entry('proxy_hk_auth')


def test_save_pool_retires_legacy_proxy_config(
        flask_client, _server_config_snapshot):
    r1 = flask_client.post('/api/v1/server-config', json={
        'proxy_config': {'http_proxy': 'http://legacy.example.com:3128',
                         'https_proxy': ''},
    })
    assert r1.get_json()['ok'] is True
    r2 = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{'url': 'http://gw.example.com:8080',
                        'scope': 'global'}],
    })
    assert r2.get_json()['ok'] is True
    cfg = json.loads(open(_server_config_snapshot._SERVER_CONFIG_PATH).read())
    assert cfg.get('proxy_pool'), 'pool must persist'
    assert not (cfg.get('proxy_config') or {}).get('http_proxy'), (
        'legacy proxy_config must retire once the pool editor owns proxying')


def test_save_pool_removal_sweeps_vault_credential(
        flask_client, _server_config_snapshot):
    from lib.credentials_vault import delete_entry, get_entry
    r1 = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{'name': 'HK',
                        'url': 'http://u:p@g-hk.example.com:8080',
                        'scope': 'subscription'}],
    })
    assert r1.get_json()['ok'] is True
    assert get_entry('proxy_hk_auth') == 'u:p'
    r2 = flask_client.post('/api/v1/server-config', json={'proxy_pool': []})
    assert r2.get_json()['ok'] is True
    assert get_entry('proxy_hk_auth') is None, '删行必须连 vault 凭证一起清'
    cfg = json.loads(open(_server_config_snapshot._SERVER_CONFIG_PATH).read())
    assert cfg.get('proxy_pool') == []


def test_save_pool_auth_removal_sweeps_vault_without_deleting_row(
        flask_client, _server_config_snapshot):
    from lib.credentials_vault import get_entry
    first = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{
            'id': 'office', 'url': 'http://proxy.example.com:8080',
            'scope': 'global', 'username': 'alice', 'password': 'secret',
        }],
    })
    assert first.status_code == 200
    assert get_entry('proxy_office_auth') == 'alice:secret'

    second = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{
            'id': 'office', 'url': 'http://proxy.example.com:8080',
            'scope': 'global', 'credential_vault': 'proxy_office_auth',
            'clear_credential': True,
        }],
    })
    assert second.status_code == 200
    assert get_entry('proxy_office_auth') is None
    cfg = json.loads(open(_server_config_snapshot._SERVER_CONFIG_PATH).read())
    assert cfg['proxy_pool'][0]['id'] == 'office'
    assert 'credential_vault' not in cfg['proxy_pool'][0]


def test_save_pool_invalid_payload_400(flask_client, _server_config_snapshot):
    resp = flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{'url': 'http://gw.example.com:8080',
                        'scope': 'global', 'password': 'missing-user'}],
    })
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False


def test_get_server_config_exposes_pool_without_credentials(
        flask_client, _server_config_snapshot):
    from lib.credentials_vault import delete_entry
    flask_client.post('/api/v1/server-config', json={
        'proxy_pool': [{'name': 'HK', 'url': 'http://u:p@g.example.com:8080',
                        'scope': 'subscription'}],
    })
    resp = flask_client.get('/api/v1/server-config')
    body = resp.get_json()
    assert body['ok'] is True
    pool = (body.get('network') or {}).get('proxy_pool') or []
    assert len(pool) == 1
    assert pool[0]['has_credential'] is True
    assert 'u:p' not in json.dumps(body), 'GET 绝不回显凭证'
    delete_entry('proxy_hk_auth')


def test_proxy_test_endpoint_classification(flask_client):
    seen = []

    def _fake(url, **kw):
        seen.append((url, kw.get('proxies')))
        return mock.Mock(status_code=400)

    with mock.patch('requests.post', side_effect=_fake):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://gw.example.com:8080', 'scope': 'subscription',
        })
    body = resp.get_json()
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert body['ok'] is True
    assert body['any_ok'] is True
    labels = [r['label'] for r in body['results']]
    assert labels == ['OpenAI Auth', 'Anthropic API']
    assert all(r['verdict'] == 'ok' for r in body['results'])
    # 探测走显式代理，不吃环境变量
    assert seen[0][1] == {'http': 'http://gw.example.com:8080',
                          'https': 'http://gw.example.com:8080'}


def test_proxy_test_endpoint_geo_block_and_failure(flask_client):
    with mock.patch('requests.post', return_value=mock.Mock(status_code=403)):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://gw.example.com:8080'})
    body = resp.get_json()
    assert body['any_ok'] is False
    assert body['results'][0]['verdict'] == 'geo_blocked'

    with mock.patch('requests.post',
                    side_effect=ConnectionError('refused')):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://gw.example.com:8080'})
    body = resp.get_json()
    assert body['results'][0]['verdict'] == 'network_fail'
    assert 'refused' in body['results'][0]['error']


def test_proxy_test_endpoint_proxy_auth_classification(flask_client):
    """407 → proxy_auth：凭证失效绝不能报成“网络失败”。

    2026-08-21 事故锚：hk-gw 凭证缺了首字符反引号，Squid 对一切 CONNECT
    回 407，测试却笼统报 network_fail，排查被误导成“URL 是否过时”。
    """
    with mock.patch('requests.post', return_value=mock.Mock(status_code=407)):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://gw.example.com:8080'})
    body = resp.get_json()
    assert body['any_ok'] is False
    assert body['results'][0]['verdict'] == 'proxy_auth'
    assert body['results'][0]['status'] == 407

    # Squid 对 HTTPS CONNECT 的 407 在 requests 里是 ProxyError 异常而非响应
    import requests as _rq
    tunnel_err = _rq.exceptions.ProxyError(
        "HTTPSConnectionPool(host='auth.openai.com', port=443): "
        'Tunnel connection failed: 407 Proxy Authentication Required')
    with mock.patch('requests.post', side_effect=tunnel_err):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://gw.example.com:8080'})
    body = resp.get_json()
    assert body['any_ok'] is False
    assert body['results'][0]['verdict'] == 'proxy_auth'


def test_proxy_test_never_echoes_credential(flask_client):
    secret = 's3cr3t-pw'
    with mock.patch('requests.post',
                    side_effect=ConnectionError(
                        'Failed to connect to http://user:%s@gw.example.com'
                        % secret)):
        resp = flask_client.post('/api/v1/network/proxy-test', json={
            'url': 'http://user:%s@gw.example.com:8080' % secret,
            'scope': 'subscription'})
    raw = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert secret not in raw, '异常文本必须脱敏'


def test_proxy_test_rejects_bad_input_400(flask_client):
    resp = flask_client.post('/api/v1/network/proxy-test',
                             json={'url': 'socks5://gw.example.com:1080'})
    assert resp.status_code == 400


def test_global_egress_route_specs_keep_direct_pool_and_environment_order():
    proxy._proxy_pool = [
        _entry('subscription-only', scope='subscription'),
        _entry('global-one', scope='global',
               url='http://pool.example.com:8080'),
    ]
    with mock.patch.object(proxy, 'get_proxy_config', return_value={
            'http_proxy': 'http://env.example.com:8080',
            'https_proxy': 'http://env.example.com:8080'}):
        routes = proxy.global_egress_route_specs(
            'ssh://dev.example.test:3022/', include_bypassed=True)
    route_ids = [route.route_id for route in routes]
    assert route_ids[0] == 'direct'
    assert route_ids[1].startswith('pool:global-one:g')
    assert route_ids[2].startswith('proxy:environment:g')
    assert all('subscription-only' not in route.route_id for route in routes)


def test_global_egress_route_specs_require_explicit_bypass_challenge():
    proxy._proxy_pool = [
        _entry('global-one', scope='global',
               url='http://pool.example.com:8080'),
    ]
    with mock.patch.object(proxy, '_bypass_domains', ('.example.test',)), \
            mock.patch.object(proxy, 'get_proxy_config', return_value={
                'http_proxy': '', 'https_proxy': ''}):
        strict = proxy.global_egress_route_specs(
            'https://dev.example.test/repo.git')
        challenged = proxy.global_egress_route_specs(
            'https://dev.example.test/repo.git', include_bypassed=True)
    assert [route.route_id for route in strict] == ['direct']
    assert [route.route_id for route in challenged][0] == 'direct'
    assert challenged[1].route_id.startswith('pool:global-one:g')


def test_global_egress_route_specs_never_proxy_loopback():
    proxy._proxy_pool = [
        _entry('global-one', scope='global',
               url='http://pool.example.com:8080'),
    ]
    routes = proxy.global_egress_route_specs(
        'https://127.0.0.1:8443/', include_bypassed=True)
    assert [route.route_id for route in routes] == ['direct']


def test_global_egress_route_specs_do_not_grant_vault_proxy_to_shell():
    proxy._proxy_pool = [
        _entry('credentialed', scope='global',
               url='http://pool.example.com:8080',
               credential_vault='proxy_credentialed_auth'),
    ]
    with mock.patch.object(proxy, 'get_proxy_config', return_value={
            'http_proxy': '', 'https_proxy': ''}), \
            mock.patch.object(proxy, '_resolve_entry') as resolve:
        routes = proxy.global_egress_route_specs(
            'https://dev.example.test/repo.git', include_bypassed=True)
    assert [route.route_id for route in routes] == ['direct']
    resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
