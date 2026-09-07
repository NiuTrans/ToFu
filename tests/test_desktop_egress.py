"""tests/test_desktop_egress.py — desktop egress 路由层 + agent 执行器 + 刷新 singleflight 守卫（S2）。

Covers:
  * 域名白名单（精确 host 匹配，防 evil.com 后缀）
  * route_request 三态探测（ok/geo_blocked/network_fail）+ agent 选择
    （egress capability 过滤、user_id 所有者隔离、缺失所有者默认拒绝、
    无 agent → EgressUnavailable）
  * egress_http 结果适配（status/json 与 requests.Response 同形）
  * agent 侧 cmd_egress_http（白名单再校验、proxy_mode 解析、结果形状）
  * OS 代理发现（winreg / scutil 两条路径）
  * bridge 按命令 TTL（cmd['ttl'] 覆盖全局 90s）
  * claude/codex 刷新 singleflight（同 token 并发刷新合并为一次上游调用）
  * claude_exchange_code 在直连 403 时经 egress 落库

Failure-first：全部在 S2 实现前红（lib/desktop/egress.py 不存在）。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import egress
from lib.desktop.egress import EgressUnavailable
from lib.subscription_routes import ProbeResult, Route, manager as route_manager


def _agent(agent_id='a1', user_id='1', egress_cap=True, name='box'):
    return {
        'agent_id': agent_id, 'name': name, 'platform': 'win32',
        'capabilities': {'egress': egress_cap},
        'user_id': user_id, 'last_seen': time.time(),
    }


class TestWhitelist(unittest.TestCase):

    def test_allowed_hosts(self):
        for u in ('https://api.anthropic.com/v1/messages',
                  'https://console.anthropic.com/v1/oauth/token',
                  'https://platform.claude.com/v1/oauth/token',
                  'https://auth.openai.com/oauth/token',
                  'https://chatgpt.com/backend-api/codex/responses'):
            self.assertTrue(egress.host_allowed(u), u)

    def test_suffix_attack_rejected(self):
        for u in ('https://api.anthropic.com.evil.com/x',
                  'https://chatgpt.com.attacker.io/',
                  'https://notanthropic.com/v1/messages'):
            self.assertFalse(egress.host_allowed(u), u)


class TestRouteRequest(unittest.TestCase):

    def _agents(self, agents):
        # Patch the bridge-level source so the REAL _online_egress_agents
        # filtering (egress capability + tenant scope) actually runs.
        return mock.patch('lib.desktop.online_agents', return_value=agents)

    def test_probe_ok_routes_direct(self):
        with mock.patch.object(egress, '_probe_host', return_value='ok'):
            self.assertEqual(egress.route_request(
                'https://api.anthropic.com/v1/messages', user_id='1'), 'direct')

    def test_geo_block_without_agent_raises(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/messages',
                                     user_id='1')

    def test_geo_block_with_agent_returns_egress_target(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='1')]):
            target = egress.route_request('https://api.anthropic.com/v1/messages',
                                          user_id='1')
        self.assertEqual(target, 'a1')

    def test_network_fail_also_routes_to_agent(self):
        with mock.patch.object(egress, '_probe_host', return_value='network_fail'), \
             self._agents([_agent(user_id='1')]):
            self.assertEqual(egress.route_request('https://api.anthropic.com/v1/x', user_id='1'), 'a1')

    def test_non_egress_agent_not_selected(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(egress_cap=False)]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/x', user_id='1')

    def test_tenant_isolation(self):
        # Owner 2's agent must not serve owner 1's egress.
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='2')]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/x', user_id='1')

    def test_missing_owner_never_selects_deployment_global_agent(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='1')]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request(
                    'https://api.anthropic.com/v1/x', user_id='')

    def test_probe_result_cached_per_host(self):
        # The manager, rather than the UI verdict mirror, owns reachability.
        # A second path on the same host reuses the healthy route without a
        # second probe.
        route = Route('direct', 'direct', 'direct')
        route_manager.reset()
        with mock.patch('lib.proxy.subscription_route_specs',
                        return_value=[route]), \
             mock.patch.object(route_manager, '_probe',
                               return_value=ProbeResult('ok', 10, 401)) as probe:
            egress.route_request('https://api.anthropic.com/a', user_id='')
            egress.route_request('https://api.anthropic.com/b', user_id='')
        self.assertEqual(probe.call_count, 1)
        route_manager.reset()

    def test_steady_healthy_probe_logs_are_debug_only(self):
        infos = []
        debugs = []
        with mock.patch.object(egress, '_probe_host_paths', return_value='ok'), \
             mock.patch.object(
                 egress.logger, 'info',
                 side_effect=lambda *args, **_kwargs: infos.append(args)), \
             mock.patch.object(
                 egress.logger, 'debug',
                 side_effect=lambda *args, **_kwargs: debugs.append(args)):
            verdict = egress._probe_host(
                'https://chatgpt.com/backend-api/codex/responses')

        self.assertEqual(verdict, 'ok')
        self.assertEqual(infos, [])
        self.assertEqual(len(debugs), 1)

    def test_network_failure_cache_expires_before_proxy_cooldown_retry(self):
        """Transient network failure must not poison routing for 300s."""
        host = 'chatgpt.com'
        egress._probe_cache.invalidate(host)
        egress._probe_network_fail_cache.invalidate(host)
        with mock.patch.object(egress, '_probe_host_paths',
                               side_effect=['network_fail', 'ok']) as probe, \
             mock.patch.object(egress._probe_network_fail_cache,
                               'ttl', 0.001):
            self.assertEqual(
                egress._probe_host('https://chatgpt.com/backend-api/codex/responses'),
                'network_fail')
            time.sleep(0.005)
            self.assertEqual(
                egress._probe_host('https://chatgpt.com/backend-api/codex/responses'),
                'ok')
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(egress._probe_cache.get(host), 'ok')

    def test_multi_agent_requires_pinned_choice(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent('a1', '1'), _agent('a2', '1', name='box2')]), \
             mock.patch.object(egress, '_pinned_agent', return_value='a2'):
            self.assertEqual(egress.route_request('https://api.anthropic.com/v1/x', user_id='1'), 'a2')

    def test_multi_agent_unpinned_orders_by_recency(self):
        # G1: an unpinned multi-agent deployment no longer raises — the chain
        # is ordered (pinned → last egress success → last_seen).
        a1 = _agent('a1', '1'); a1['last_seen'] = 100.0
        a2 = _agent('a2', '1', name='box2'); a2['last_seen'] = 200.0
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([a1, a2]), \
             mock.patch.object(egress, '_pinned_agent', return_value=''):
            self.assertEqual(egress.route_request(
                'https://api.anthropic.com/v1/x', user_id='1'), 'a2')
            # A recent transport-level success beats last_seen.
            egress._note_success('a1')
            try:
                self.assertEqual(egress.route_request(
                    'https://api.anthropic.com/v1/x', user_id='1'), 'a1')
            finally:
                with egress._success_lock:
                    egress._last_success.pop('a1', None)

    def test_pinned_agent_offline_falls_back(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent('a1', '1')]), \
             mock.patch.object(egress, '_pinned_agent', return_value='gone'):
            self.assertEqual(egress.route_request(
                'https://api.anthropic.com/v1/x', user_id='1'), 'a1')


class TestEgressHttp(unittest.TestCase):

    def test_whitelist_enforced(self):
        with self.assertRaises(EgressUnavailable):
            egress.egress_http('https://evil.com/x', user_id='1')

    def test_happy_path_result_shape(self):
        payload = json.dumps({'access_token': 'tok-1', 'expires_in': 3600}).encode()
        agent_result = {
            'status': 200,
            'headers': {'content-type': 'application/json'},
            'body_b64': base64.b64encode(payload).decode(),
            'elapsed_ms': 42,
        }
        with mock.patch.object(egress, 'route_candidates', return_value=['a1']), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=(agent_result, None)) as send:
            resp = egress.egress_http(
                'https://api.anthropic.com/v1/oauth/token',
                method='POST', headers={'Content-Type': 'application/json'},
                body=b'{"x":1}', timeout=30, user_id='1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['access_token'], 'tok-1')
        # 命令按设计带 TTL + 目标 agent + user 作用域
        _args, kwargs = send.call_args
        self.assertEqual(kwargs.get('target_agent_id'), 'a1')
        self.assertEqual(kwargs.get('user_id'), '1')
        self.assertEqual(kwargs.get('ttl'), 120)
        # body 走 base64
        params = send.call_args[0][1]
        self.assertEqual(base64.b64decode(params['body_b64']), b'{"x":1}')

    def test_agent_network_error_raises_unavailable(self):
        with mock.patch.object(egress, 'route_candidates', return_value=['a1']), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=({'status': 0, 'error': 'DNS fail'}, None)):
            with self.assertRaises(EgressUnavailable):
                egress.egress_http('https://api.anthropic.com/x', user_id='1')

    def test_bridge_error_raises_unavailable(self):
        with mock.patch.object(egress, 'route_candidates', return_value=['a1']), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=(None, 'Desktop agent timeout')):
            with self.assertRaises(EgressUnavailable):
                egress.egress_http('https://api.anthropic.com/x', user_id='1')

    def test_explicit_agent_still_requires_owner_scope(self):
        with mock.patch('lib.desktop.send_desktop_command') as send:
            with self.assertRaises(EgressUnavailable):
                egress.egress_http(
                    'https://api.anthropic.com/x',
                    user_id='', agent_id='a1')
        send.assert_not_called()


class TestAgentFallback(unittest.TestCase):
    """G1 候选链 failover（SUBSCRIPTION_RELAY_SCENARIOS §4.2）。"""

    def tearDown(self):
        with egress._success_lock:
            egress._last_success.clear()

    def test_http_bridge_failure_fails_over(self):
        good = {'status': 200, 'headers': {}, 'body_b64': '', 'elapsed_ms': 5}
        calls = []

        def _send(_type, _params, **kw):
            calls.append(kw.get('target_agent_id'))
            if calls[-1] == 'a1':
                return None, 'agent offline'
            return good, None
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['a1', 'a2']), \
             mock.patch('lib.desktop.send_desktop_command', side_effect=_send):
            resp = egress.egress_http('https://api.anthropic.com/x', user_id='1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, ['a1', 'a2'])

    def test_http_agent_network_failure_fails_over(self):
        def _send(_t, _p, **kw):
            if kw.get('target_agent_id') == 'a1':
                return {'status': 0, 'error': 'connect timeout'}, None
            return {'status': 200, 'headers': {}, 'body_b64': ''}, None
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['a1', 'a2']), \
             mock.patch('lib.desktop.send_desktop_command', side_effect=_send):
            resp = egress.egress_http('https://api.anthropic.com/x', user_id='1')
        self.assertEqual(resp.status_code, 200)

    def test_http_delivered_error_status_never_fails_over(self):
        # A real HTTP answer (even 500) is a DEFINITIVE upstream verdict —
        # retrying it on another machine would double-bill the subscription.
        calls = []

        def _send(_t, _p, **kw):
            calls.append(kw.get('target_agent_id'))
            return {'status': 500, 'headers': {}, 'body_b64': ''}, None
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['a1', 'a2']), \
             mock.patch('lib.desktop.send_desktop_command', side_effect=_send):
            resp = egress.egress_http('https://api.anthropic.com/x', user_id='1')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(calls, ['a1'])

    def test_stream_pre_meta_failure_fails_over(self):
        enqueued = []

        def _enq(_type, _params, **kw):
            enqueued.append(kw.get('target_agent_id'))
            if enqueued[-1] == 'a1':
                return None, 'agent offline'
            return enqueued[-1], None
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['a1', 'a2']), \
             mock.patch('lib.desktop.enqueue_desktop_command', side_effect=_enq), \
             mock.patch.object(egress.EgressStreamReader, 'wait_headers',
                               new=lambda self: setattr(self, 'status_code', 200)):
            reader = egress.open_stream('https://api.anthropic.com/v1/messages',
                                        user_id='1')
        self.assertEqual(enqueued, ['a1', 'a2'])
        self.assertEqual(reader._agent_id, 'a2')

    def test_stream_frames_arrived_never_fails_over(self):
        def _wh(self):
            self._seq = 3  # frames arrived but no meta — ambiguous, DON'T retry
            raise EgressUnavailable('no meta within window')
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['a1', 'a2']), \
             mock.patch('lib.desktop.enqueue_desktop_command',
                        side_effect=lambda _t, _p, **kw: (kw.get('cmd_id'), None)), \
             mock.patch.object(egress.EgressStreamReader, 'wait_headers', new=_wh), \
             mock.patch.object(egress, 'cancel_stream'):
            with self.assertRaises(EgressUnavailable):
                egress.open_stream('https://api.anthropic.com/v1/messages',
                                   user_id='1')


class TestAgentExecutor(unittest.TestCase):
    """agent 侧 cmd_egress_http（lib/desktop_agent/_egress.py）。"""

    def test_agent_side_whitelist(self):
        from lib.desktop_agent._egress import cmd_egress_http
        out = cmd_egress_http({'url': 'https://evil.com/x', 'method': 'GET'})
        self.assertIn('error', out)

    def test_executor_result_shape(self):
        from lib.desktop_agent import _egress as ag
        fake = mock.Mock(status_code=200,
                         headers={'content-type': 'application/json',
                                  'set-cookie': 'secret=1'},
                         content=b'{"ok":true}')
        fake.elapsed = mock.Mock(total_seconds=lambda: 0.05)
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        return_value=fake):
            out = ag.cmd_egress_http({
                'url': 'https://api.anthropic.com/v1/messages',
                'method': 'POST', 'headers': {'x': 'y'},
                'body_b64': base64.b64encode(b'{}').decode(),
                'timeout_ms': 5000, 'proxy_mode': 'env'})
        self.assertEqual(out['status'], 200)
        self.assertEqual(base64.b64decode(out['body_b64']), b'{"ok":true}')
        self.assertNotIn('set-cookie', out['headers'])  # cookie 剥离

    def test_executor_network_error_status0(self):
        from lib.desktop_agent import _egress as ag
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        side_effect=ConnectionError('refused')):
            out = ag.cmd_egress_http({
                'url': 'https://api.anthropic.com/x', 'method': 'GET',
                'proxy_mode': 'env'})
        self.assertEqual(out['status'], 0)
        self.assertIn('error', out)

    def test_direct_mode_bypasses_proxy(self):
        from lib.desktop_agent import _egress as ag
        captured = {}
        def _fake(method, url, **kw):
            captured.update(kw)
            m = mock.Mock(status_code=200, headers={}, content=b'')
            m.elapsed = mock.Mock(total_seconds=lambda: 0.01)
            return m
        with mock.patch('lib.desktop_agent._egress.requests.request', _fake):
            ag.cmd_egress_http({'url': 'https://api.anthropic.com/x',
                                'method': 'GET', 'proxy_mode': 'direct'})
        self.assertEqual(captured.get('proxies'), {'no_proxy': '*'})


class TestOSProxyDiscovery(unittest.TestCase):

    def test_windows_registry_proxy(self):
        from lib.desktop_agent import _egress as ag
        # 真实 winreg.QueryValueEx 返回 (value, type) 二元组。
        fake_reg = {
            ('ProxyEnable',): (1, 4),
            ('ProxyServer',): ('127.0.0.1:7890', 1),
        }
        fake_winreg = mock.Mock()
        fake_winreg.HKEY_CURRENT_USER = object()
        fake_winreg.OpenKey.return_value = object()
        fake_winreg.QueryValueEx.side_effect = (
            lambda _k, name: fake_reg[(name,)])
        with mock.patch.dict('sys.modules', {'winreg': fake_winreg}), \
             mock.patch('lib.desktop_agent._egress._IS_WINDOWS', True), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', False):
            self.assertEqual(ag._os_proxy_url(), 'http://127.0.0.1:7890')

    def test_macos_scutil_proxy(self):
        from lib.desktop_agent import _egress as ag
        scutil_out = ('<dictionary> {\n  HTTPEnable : 1\n'
                      '  HTTPProxy : 127.0.0.1\n  HTTPPort : 7897\n'
                      '  HTTPSEnable : 1\n  HTTPSProxy : 127.0.0.1\n'
                      '  HTTPSPort : 7897\n}\n')
        with mock.patch('lib.desktop_agent._egress._IS_WINDOWS', False), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', True), \
             mock.patch('lib.desktop_agent._egress.subprocess.run') as run:
            run.return_value = mock.Mock(returncode=0, stdout=scutil_out)
            self.assertEqual(ag._os_proxy_url(), 'http://127.0.0.1:7897')

    def test_no_system_proxy_returns_empty(self):
        from lib.desktop_agent import _egress as ag
        with mock.patch('lib.desktop_agent._egress._IS_WINDOWS', False), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', False):
            self.assertEqual(ag._os_proxy_url(), '')


class TestBridgeTTL(unittest.TestCase):
    """Bridge expiration follows each command's declared TTL."""

    def setUp(self):
        from lib.desktop import bridge
        self._bridge = bridge
        with bridge.command_queue_lock:
            self._saved_agents = dict(bridge._agents)
            bridge._agents.clear()
        bridge.register_agent('ttl-agent', {'name': 'ttl'}, user_id='owner')

    def tearDown(self):
        bridge = self._bridge
        with bridge.command_queue_lock:
            bridge._agents.clear()
            bridge._agents.update(self._saved_agents)

    def test_per_command_ttl_override(self):
        from lib.desktop import bridge
        now = time.time()
        cmd = {'id': 'c1', 'type': 'egress_http', 'params': {},
               'created_at': now - 100,  # 超过全局 90s
               'event': threading.Event(), 'result': None, 'error': None,
               'ttl': 120, 'user_id': 'owner'}
        with bridge.command_queue_lock:
            bridge.command_queue['c1'] = cmd
        try:
            pending = bridge.take_pending_commands(
                agent_id='ttl-agent', user_id='owner')
            self.assertEqual([c['id'] for c in pending], ['c1'])
        finally:
            with bridge.command_queue_lock:
                bridge.command_queue.pop('c1', None)

    def test_default_ttl_still_90(self):
        from lib.desktop import bridge
        now = time.time()
        cmd = {'id': 'c2', 'type': 'desktop_list_files', 'params': {},
               'created_at': now - 100,
               'event': threading.Event(), 'result': None, 'error': None,
               'user_id': 'owner'}
        with bridge.command_queue_lock:
            bridge.command_queue['c2'] = cmd
        try:
            pending = bridge.take_pending_commands(
                agent_id='ttl-agent', user_id='owner')
            self.assertEqual(pending, [])
            self.assertEqual(cmd['error'], 'Command expired (stale cleanup)')
        finally:
            with bridge.command_queue_lock:
                bridge.command_queue.pop('c2', None)


class TestRefreshSingleflight(unittest.TestCase):

    def test_concurrent_refresh_merges_to_one_upstream_call(self):
        import lib.oauth.token_store as token_store
        calls = {'n': 0}
        stored = {'expire': 0, 'refresh_token': 'r0', 'access_token': 'old'}

        def fake_refresh(rt):
            calls['n'] += 1
            time.sleep(0.05)  # 放大竞态窗口
            stored.update({'access_token': 'new', 'refresh_token': 'r1',
                           'expire': time.time() + 3600})
            return dict(stored)

        results = []
        def worker():
            results.append(token_store.refresh_singleflight(
                'codex', 'r0', fake_refresh,
                load=lambda: dict(stored)))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls['n'], 1)
        self.assertTrue(all(r and r['access_token'] == 'new' for r in results))

    def test_singleflight_passes_through_failure(self):
        import lib.oauth.token_store as token_store
        with mock.patch('lib.oauth.token_store.load_token', return_value=None):
            out = token_store.refresh_singleflight(
                'codex', 'r0', lambda rt: None, load=lambda: None)
        self.assertIsNone(out)


class TestExchangeViaEgress(unittest.TestCase):

    def test_claude_exchange_falls_back_to_egress_on_geo_block(self):
        import lib.oauth.claude as claude
        geo = mock.Mock(status_code=403,
                        text='{"error":{"type":"forbidden","message":"Request not allowed"}}')
        geo.json.return_value = {'error': {'type': 'forbidden'}}
        egress_resp = mock.Mock(status_code=200)
        egress_resp.json.return_value = {
            'access_token': 'sk-ant-oat01-NEW', 'refresh_token': 'r1',
            'expires_in': 28800,
        }
        egress_resp.text = json.dumps(egress_resp.json.return_value)
        with mock.patch('lib.oauth.claude.http_post', return_value=geo), \
             mock.patch('lib.desktop.egress.egress_http', return_value=egress_resp) as eg, \
             mock.patch('lib.oauth.claude.save_token', return_value=True) as save, \
             mock.patch('lib.desktop.egress.route_request', return_value='a1'):
            out = claude.claude_exchange_code('code-1', 'verifier-1', state='s')
        self.assertEqual(out['access_token'], 'sk-ant-oat01-NEW')
        self.assertTrue(save.called)
        self.assertTrue(eg.called)


class TestLlmOwnerPropagation(unittest.TestCase):

    def test_non_stream_transport_reuses_selected_agent_and_owner(self):
        from lib.llm.chat import chat

        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {
            'choices': [{
                'message': {'role': 'assistant', 'content': 'ok'},
                'finish_reason': 'stop',
            }],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
        }
        with mock.patch(
                'lib.desktop.egress.route_request',
                return_value='agent-41') as route, \
             mock.patch(
                 'lib.desktop.egress.egress_http',
                 return_value=response) as relay:
            content, _usage = chat(
                [{'role': 'user', 'content': 'hi'}],
                model='gpt-x', api_key='k',
                base_url='https://api.anthropic.com/v1',
                owner_user_id=41, max_retries=0,
            )

        self.assertEqual(content, 'ok')
        self.assertEqual(route.call_args.kwargs['user_id'], '41')
        self.assertEqual(relay.call_args.kwargs['user_id'], '41')
        self.assertEqual(relay.call_args.kwargs['agent_id'], 'agent-41')


if __name__ == '__main__':
    unittest.main()
