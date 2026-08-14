"""tests/test_oauth_egress_status.py — S4 守卫：出口状态计算 / 选择器端点 / 探测接线。

Covers:
  * egress_status 五态（direct / agent / agent_no_capability / unavailable /
    unknown）——页面加载路径绝不发起探测（只读缓存 + 异步触发）
  * 选择器端点 GET/POST oauth_egress_agents.json（多 agent 时 pin 生效）
  * provider_probe claude 路按路由走 direct/egress
  * provider_probe codex 路从 SKIPPED 升级为流式真探测（open_stream 分类）
  * 前端卡片出口行 harness（状态行渲染 + capability-off 指引）

Failure-first：S4 实现前 egress_status / 选择器端点不存在。
"""

from __future__ import annotations

import json
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import egress


def _agent(agent_id='a1', user_id='', cap=True, name='box'):
    return {'agent_id': agent_id, 'name': name, 'platform': 'win32',
            'capabilities': {'egress': cap}, 'user_id': user_id,
            'last_seen': time.time()}


class TestEgressStatus(unittest.TestCase):

    def setUp(self):
        # Snapshot/restore the process-global probe cache — leaking a
        # 'geo_blocked' verdict for api.anthropic.com into sibling suites
        # (provider_probe_oauth) reroutes THEIR probes through the egress
        # branch and breaks them. Pollution hygiene, not production logic.
        self._saved = dict(egress._probe_cache._data)
        self._saved_network_fail = dict(
            egress._probe_network_fail_cache._data)
        egress._probe_cache.clear()
        egress._probe_network_fail_cache.clear()

    def tearDown(self):
        with egress._probe_cache._lock:
            egress._probe_cache._data.clear()
            egress._probe_cache._data.update(self._saved)
        with egress._probe_network_fail_cache._lock:
            egress._probe_network_fail_cache._data.clear()
            egress._probe_network_fail_cache._data.update(
                self._saved_network_fail)

    def _status(self, host='api.anthropic.com', verdict=None, agents=(),
                user_id=''):
        if verdict:
            egress._probe_cache.set(host, verdict)
        with mock.patch('lib.desktop.online_agents', return_value=list(agents)):
            return egress.egress_status(host, user_id=user_id)

    def test_direct_when_cached_ok(self):
        st = self._status(verdict='ok')
        self.assertEqual(st['state'], 'direct')
        self.assertIn('server_routes', st)
        self.assertIn('preferred_server_route', st)

    def test_agent_when_blocked_and_capable_agent_online(self):
        st = self._status(verdict='geo_blocked', agents=[_agent()])
        self.assertEqual(st['state'], 'agent')
        self.assertEqual(st['agents'][0]['agent_id'], 'a1')

    def test_agent_no_capability_distinct(self):
        # agent 在线但 capabilities.egress=false —— 默认态，必须单独可辨。
        st = self._status(verdict='geo_blocked', agents=[_agent(cap=False)])
        self.assertEqual(st['state'], 'agent_no_capability')

    def test_unavailable_when_blocked_and_zero_agents(self):
        st = self._status(verdict='network_fail', agents=[])
        self.assertEqual(st['state'], 'unavailable')

    def test_unknown_without_cache_never_probes_inline(self):
        # 页面加载路径：无缓存 → unknown，且绝不 inline 发起探测
        #（后台 warm-up 是独立线程，归另一条测试管）。
        with mock.patch.object(egress, '_probe_host') as probe, \
             mock.patch.object(egress, '_spawn_background_probe',
                               return_value=None):
            st = self._status(verdict=None)
        self.assertEqual(st['state'], 'unknown')
        probe.assert_not_called()

    def test_unknown_triggers_background_probe(self):
        fired = []
        with mock.patch.object(egress, '_probe_host') as probe, \
             mock.patch.object(egress, '_spawn_background_probe',
                               side_effect=lambda h: fired.append(h)):
            st = self._status(verdict=None)
        self.assertEqual(st['state'], 'unknown')
        self.assertEqual(fired, ['api.anthropic.com'])

    def test_tenant_scoping(self):
        st = self._status(verdict='geo_blocked',
                          agents=[_agent(user_id='u2')], user_id='u1')
        self.assertEqual(st['state'], 'unavailable')

    def test_bg_probe_hits_real_api_endpoint_not_web_root(self):
        # 2026-08-10 事故回归：chatgpt.com/ 根路径被 Cloudflare 拦 403
        #（健康代理也一样），误判 geo_blocked 后按 host 键缓存 300s，
        # 连真实路由一起毒死。后台探测必须打真实 API 端点。
        seen = []
        with mock.patch.object(egress, '_probe_host',
                               side_effect=lambda url: seen.append(url) or 'ok'):
            egress._spawn_background_probe('chatgpt.com')
            deadline = time.time() + 5
            while 'chatgpt.com' in egress._probe_bg_fired \
                    and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(seen,
                         ['https://chatgpt.com/backend-api/codex/responses'])

    def test_bg_probe_default_stays_web_root(self):
        # 未登记的主机维持根路径语义（api.anthropic.com 根 POST 不返 403）。
        seen = []
        with mock.patch.object(egress, '_probe_host',
                               side_effect=lambda url: seen.append(url) or 'ok'):
            egress._spawn_background_probe('api.anthropic.com')
            deadline = time.time() + 5
            while 'api.anthropic.com' in egress._probe_bg_fired \
                    and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(seen, ['https://api.anthropic.com/'])

    def test_bg_probe_url_overrides_stay_whitelisted(self):
        # 覆盖表里的每个 URL 必须仍落在订阅白名单主机上——否则探测本身
        # 会把缓存暖到 route_candidates 永不接管的域外。
        for url in egress._BG_PROBE_URL.values():
            self.assertTrue(egress.host_allowed(url), url)


class TestPinnedSelector(unittest.TestCase):

    def test_pin_write_and_read_roundtrip(self):
        from lib.desktop import egress as eg
        with mock.patch('lib.desktop.egress._pinned_agent', return_value='a2'):
            self.assertEqual(eg._pinned_agent('u1'), 'a2')

    def test_route_request_uses_pin_among_many(self):
        with mock.patch('lib.desktop.online_agents',
                        return_value=[_agent('a1'), _agent('a2', name='b2')]), \
             mock.patch.object(egress, '_probe_host',
                               return_value='geo_blocked'), \
             mock.patch.object(egress, '_pinned_agent', return_value='a2'):
            self.assertEqual(
                egress.route_request('https://api.anthropic.com/v1/x',
                                     user_id=''), 'a2')


class TestProviderProbeRouting(unittest.TestCase):
    """provider_probe 的 oauth 分支按路由走 direct / egress。"""

    def _probe(self, oauth='claude'):
        from lib.provider_probe import probe_one_cell
        return probe_one_cell

    def test_claude_probe_direct_path(self):
        resp = mock.Mock(
            status_code=200,
            text='{"content":[{"type":"text","text":"OK"}]}')
        with mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
             mock.patch('lib.http_client.http_post', return_value=resp) as hp, \
             mock.patch('lib.oauth.outbound.resolve_oauth_request',
                        return_value=('tok', {}, {'messages': []})):
            status, _detail = self._probe()(
                'https://api.anthropic.com/v1', 'oauth-managed',
                'claude-opus-4-5-20251101', None, 5, oauth='claude')
        self.assertEqual(status, 'ok')
        self.assertTrue(hp.called)

    def test_claude_probe_egress_path(self):
        er = mock.Mock(
            status_code=200,
            text='{"content":[{"type":"text","text":"OK"}]}')
        with mock.patch('lib.desktop.egress.route_request', return_value='a1'), \
             mock.patch('lib.desktop.egress.egress_http', return_value=er) as eh, \
             mock.patch('lib.oauth.outbound.resolve_oauth_request',
                        return_value=('tok', {}, {'messages': []})):
            status, _detail = self._probe()(
                'https://api.anthropic.com/v1', 'oauth-managed',
                'claude-opus-4-5-20251101', None, 5, oauth='claude')
        self.assertEqual(status, 'ok')
        self.assertTrue(eh.called)

    def test_codex_probe_is_streaming_now(self):
        # S4：codex 探测从 SKIPPED 升级为流式真探测。
        reader = mock.Mock(status_code=200)
        reader.read_all_text.return_value = (
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n')
        with mock.patch('lib.oauth.codex.codex_get_valid_token',
                        return_value='tok'), \
             mock.patch('lib.oauth.token_store.load_token',
                        return_value={'account_id': 'acc'}), \
             mock.patch('lib.desktop.egress.route_request', return_value='a1'), \
             mock.patch('lib.desktop.egress.open_stream', return_value=reader) as os_:
            status, _detail = self._probe()(
                'https://chatgpt.com/backend-api/codex', 'oauth-managed',
                'gpt-5.4', None, 5, oauth='codex')
        self.assertEqual(status, 'ok')
        self.assertTrue(os_.called)

    def test_codex_probe_not_logged_in_neutral(self):
        with mock.patch('lib.oauth.codex.codex_get_valid_token',
                        return_value=None):
            status, _detail = self._probe()(
                'https://chatgpt.com/backend-api/codex', 'oauth-managed',
                'gpt-5.4', None, 5, oauth='codex')
        self.assertEqual(status, 'not_logged_in')

    def test_codex_probe_403_classified_unauthorized(self):
        reader = mock.Mock(status_code=403)
        reader.read_all_text.return_value = 'Cloudflare block'
        with mock.patch('lib.oauth.codex.codex_get_valid_token',
                        return_value='tok'), \
             mock.patch('lib.oauth.token_store.load_token',
                        return_value={'account_id': 'acc'}), \
             mock.patch('lib.desktop.egress.route_request', return_value='a1'), \
             mock.patch('lib.desktop.egress.open_stream', return_value=reader):
            status, _detail = self._probe()(
                'https://chatgpt.com/backend-api/codex', 'oauth-managed',
                'gpt-5.4', None, 5, oauth='codex')
        self.assertEqual(status, 'unauthorized')


if __name__ == '__main__':
    unittest.main()
