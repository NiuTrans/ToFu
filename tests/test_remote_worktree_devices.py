"""tests/test_remote_worktree_devices.py — RWA P4b-1:伪路径解析 + Devices 端点.

docs/REMOTE_WORKTREE_DESIGN.md §5 P4(拍板 5A Settings→Devices):
  * **伪路径**:conv.projectPath = ``remote:<agent_id>:<root>`` 复用全部既有
    持久化机制;resolve_conv_config 翻译为 ``cfg['project_remote']`` 并清掉
    projectPath(服务器侧无此路径);总闸 off → 逐字节不翻译;
  * **Devices 端点**:``GET /api/v1/desktop/devices``(agents+bridge tokens,
    按 caller user 过滤)、``POST /api/v1/desktop/token``(铸 scope
    agents:bridge 的 key,原文只回一次)、``DELETE …/token/<id>``(属主校验,
    不许借 admin 宽权)。

Run:  pytest tests/test_remote_worktree_devices.py -m unit -v
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.desktop import bridge as db


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_ADDRESSING', '1')
    monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
    monkeypatch.setattr(db, '_last_poll', [0.0])
    monkeypatch.setattr(db, '_v1_last_poll', 0.0)
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


# ═══════════════════════════════════════════════════════════
#  伪路径 → project_remote(resolver 翻译)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPseudoPathResolution:
    def _resolve(self, monkeypatch, project_path, master='1'):
        if master is None:
            monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        else:
            monkeypatch.setenv('TOFU_REMOTE_WORKTREE', master)
        from lib.conv_config import resolve_conv_config
        return resolve_conv_config(conv_settings={'projectPath': project_path},
                                   is_active=False)

    def test_remote_path_translates_to_binding(self, monkeypatch):
        out = self._resolve(monkeypatch, 'remote:agent-A:myapp')
        assert out['project_remote'] == {'agent_id': 'agent-A', 'root': 'myapp'}
        assert out['projectPath'] == ''  # 服务器侧无此路径

    def test_normal_path_untouched(self, monkeypatch):
        out = self._resolve(monkeypatch, '/srv/code/app')
        assert out['projectPath'] == '/srv/code/app'
        assert out.get('project_remote') is None

    def test_master_switch_off_byte_identical(self, monkeypatch):
        out = self._resolve(monkeypatch, 'remote:agent-A:myapp', master=None)
        assert out['projectPath'] == 'remote:agent-A:myapp'
        assert out.get('project_remote') is None

    def test_malformed_remote_path_not_translated(self, monkeypatch):
        out = self._resolve(monkeypatch, 'remote:onlyagent')
        assert out['projectPath'] == 'remote:onlyagent'
        assert out.get('project_remote') is None

    def test_settings_resolver_persists_raw_pseudo_path(self, monkeypatch):
        """settings 解析器原样持久化伪路径(下次发送再翻译)."""
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        from lib.conv_config import resolve_conv_settings
        out = resolve_conv_settings(
            conv_settings={'projectPath': 'remote:agent-A:myapp'})
        assert out['projectPath'] == 'remote:agent-A:myapp'

    def test_parse_helper_contract(self):
        from lib.desktop.remote import parse_remote_path
        assert parse_remote_path('remote:a:r') == ('a', 'r')
        assert parse_remote_path('remote:a:r:with:colons') == ('a', 'r:with:colons')
        assert parse_remote_path('/srv/x') is None
        assert parse_remote_path('remote:') is None
        assert parse_remote_path('remote:a:') is None


# ═══════════════════════════════════════════════════════════
#  Devices 端点
# ═══════════════════════════════════════════════════════════

def _register(agent_id, user_id='', name='box'):
    db.register_agent(agent_id, {'name': name, 'platform': 'linux',
                                 'share_roots': [{'name': 'app',
                                                  'path': '/code/app'}]},
                      user_id=user_id)


@pytest.mark.api
class TestDevicesEndpoints:
    def _token(self, user_id='u-alice', scopes=('chat',)):
        from lib.api_keys import create_key
        _row, token = create_key(name='devices-test', scopes=list(scopes),
                                 user_id=user_id)
        return token

    def test_devices_lists_agents_and_bridge_tokens(self, flask_client):
        token = self._token()
        _register('agent-A', user_id='u-alice', name='mac')
        _register('agent-B', user_id='u-bob', name='win')
        from lib.api_keys import create_key
        create_key(name='bridge-mac', scopes=['agents:bridge'],
                   user_id='u-alice')
        create_key(name='unrelated-chat-key', scopes=['chat'],
                   user_id='u-alice')
        r = flask_client.get('/api/v1/desktop/devices',
                             headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200
        body = r.get_json()
        assert [a['agent_id'] for a in body['agents']] == ['agent-A']
        # tokens 只列 bridge scope + 只列自己的,不泄原文(共享 key store
        # 里可能有别次测试留的 bridge key,不断言精确条数)
        names = [t['name'] for t in body['tokens']]
        assert 'bridge-mac' in names
        assert 'unrelated-chat-key' not in names
        for t in body['tokens']:
            assert 'agents:bridge' in t['scopes']
            assert 'token' not in t

    def test_mint_token_returns_secret_once(self, flask_client):
        token = self._token()
        r = flask_client.post('/api/v1/desktop/token',
                              json={'name': 'my-mac'},
                              headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 201
        body = r.get_json()
        assert body['token'] and body['id']
        # 铸出的 token 真能过 poll 认证(scope + user 绑定)
        import os
        os.environ['TOFU_BRIDGE_SECRET'] = 'global-x'
        try:
            db.register_agent('agent-Z', {'name': 'z'}, user_id='')
            with db.command_queue_lock:
                db._agents.clear()
            rp = flask_client.post('/api/desktop/poll',
                                   json={'results': [], 'agent': {
                                       'agent_id': 'agent-Z', 'name': 'z'}},
                                   headers={'X-Bridge-Secret': body['token']})
            assert rp.status_code == 200
            assert db.online_agents()[0]['user_id'] == 'u-alice'
        finally:
            del os.environ['TOFU_BRIDGE_SECRET']

    def test_revoke_own_token(self, flask_client):
        token = self._token()
        r = flask_client.post('/api/v1/desktop/token', json={'name': 'x'},
                              headers={'Authorization': f'Bearer {token}'})
        key_id = r.get_json()['id']
        rd = flask_client.delete(f'/api/v1/desktop/token/{key_id}',
                                 headers={'Authorization': f'Bearer {token}'})
        assert rd.status_code == 200
        # 撤销后 poll 认证不再接受它
        import os
        os.environ['TOFU_BRIDGE_SECRET'] = 'global-x'
        try:
            minted = r.get_json()['token']
            rp = flask_client.post('/api/desktop/poll',
                                   json={'results': []},
                                   headers={'X-Bridge-Secret': minted})
            assert rp.status_code == 401
        finally:
            del os.environ['TOFU_BRIDGE_SECRET']

    def test_revoke_foreign_token_refused(self, flask_client):
        alice = self._token(user_id='u-alice')
        bob = self._token(user_id='u-bob')
        r = flask_client.post('/api/v1/desktop/token', json={'name': 'a'},
                              headers={'Authorization': f'Bearer {alice}'})
        key_id = r.get_json()['id']
        rd = flask_client.delete(f'/api/v1/desktop/token/{key_id}',
                                 headers={'Authorization': f'Bearer {bob}'})
        assert rd.status_code in (403, 404)

    def test_revoke_non_bridge_key_refused(self, flask_client):
        token = self._token()
        from lib.api_keys import create_key
        row, _t = create_key(name='chat-key', scopes=['chat'],
                             user_id='u-alice')
        rd = flask_client.delete(f"/api/v1/desktop/token/{row['id']}",
                                 headers={'Authorization': f'Bearer {token}'})
        assert rd.status_code in (403, 404)

    def test_devices_endpoints_carry_require_auth(self):
        """静态钉:三端点必须挂 require_auth(open 部署 200 是正确语义,
        行为层 401 由 auth 套件覆盖——不在本环境断言)."""
        import inspect
        import routes.api_v1.desktop as dmod
        src = inspect.getsource(dmod)
        for route in ('/api/v1/desktop/devices', '/api/v1/desktop/token'):
            idx = src.index(f"route('{route}'")
            assert '@require_auth' in src[idx:idx + 120], (
                f'{route} 缺少 require_auth 装饰器')
