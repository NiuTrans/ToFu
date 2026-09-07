"""One-file zero-config controlled-end installer contract.

Pairing codes, copied credentials, sidecars, and the user-facing ZIP are
retired: the credential and routes ride inside one directly runnable .exe.

The measured failure this kills: an agent handed a browser-reachable proxy
URL (missing scheme, missing /proxy/<port> prefix, then SSO-401 for a
cookieless client) polled a wall forever — access.log showed ZERO agent
requests while the panel waited on a pairing code that could never be
redeemed through the dead address.

Pinned:

SERVER side (``GET /api/v1/desktop/agent-installer``):
  1. no agent artifact in the store ⇒ 404;
  2. artifact without the NSIS attachment slot ⇒ 409 + rebuild kick;
  3. happy path ⇒ one .exe whose internal trailer holds a fresh scoped token;
  4. candidate ordering: direct LAN first (when the bind allows), the
     browser's live base LAST (SSO-edge risk — measured 2026-08-03);
  5. a loopback bind omits the direct candidate (advertising it would be
     a lie — same guard as the LAN discovery responder);
  6. ``?base=`` is adopted only when host-pinned to the request;
  7. a token-mint failure still serves the installer only for an open bridge;
     a gated bridge refuses an unusable download without exposing auth UI.

AGENT side (``desktop.connect_ui.import_attach_bundle``):
  8. no file ⇒ nothing happens;
  9. probed-alive candidate wins ⇒ attachment saved WITH the token, the
     whole route set persists as attach_candidates, the file is deleted
     (one-shot — it carries a bearer token);
 10. probe order: candidates → discovery ladder → fallbacks;
 11. nothing answers ⇒ the FIRST candidate is saved optimistically (the
     server may simply be off; the poll loop retries) and the file is
     still deleted;
 12. a LIVE existing attachment is never overridden (file still deleted);
 12b. a DEAD existing attachment does NOT veto the bundle (2026-08-06
     incident: an old dead proxy URL silently blocked the fresh bundle's
     working direct-LAN candidate forever) — the bundle re-points the
     attachment, its fresh token wins, and the dead address is kept as a
     TRAILING attach candidate (a transient outage may recover);
 13. a malformed bundle is deleted and ignored.

DIAG side (``/api/v1/desktop/client-diag`` + ``_probe.probe_server``):
 16. a pasted diagnostics bundle lands as ONE JSONL line bound to the
     caller; empty / oversize pastes are 400; GET replays newest-first
     with a truncated preview; no file ⇒ an empty list;
 17. probe_server bypasses env/system proxies — the SAME transport the
     poll loop uses, or probe and poll measure different routes.

RESUME side (``lib.desktop_agent._pair.resume_attachment``):
 14. saved address dead ⇒ the persisted attach_candidates are walked
     BEFORE the discovery ladder, a hit re-points the attachment keeping
     the ORIGINAL token;
 15. candidates all dead ⇒ the ladder runs (and wins here).

Run:  pytest tests/test_desktop_agent_bundle.py -q -p no:napari -o addopts=
"""


from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import json
import os

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]

OWNER_USER_ID = 17


# ── server-side helpers ───────────────────────────────────────────────

def _bearer(label='bundle'):
    from lib.api_keys import create_key
    _row, token = create_key(
        owner_user_id=OWNER_USER_ID,
        name=f'test-{label}',
        scopes=['chat'],
    )
    return {'Authorization': f'Bearer {token}'}


def _clear_keys():
    from lib import api_keys
    for row in api_keys.list_keys(owner_user_id=OWNER_USER_ID):
        api_keys.revoke_key(row['id'], owner_user_id=OWNER_USER_ID)


@pytest.fixture
def fake_artifact(tmp_path, monkeypatch):
    """Point the route's store seam at a one-file fake exe; returns a
    dict of knobs (entry, path, kicked)."""
    exe = tmp_path / 'TofuAgent-Setup-9.9.9-win64.exe'
    from lib.desktop_dist.agent_installer import EMPTY_TRAILER, FORMAT
    exe.write_bytes((b'fake-nsis-exe' * 64) + EMPTY_TRAILER)
    entry = {'filename': exe.name, 'version': '9.9.9', 'git_sha': 'shaHEAD',
             'size': exe.stat().st_size, 'source': 'built', 'kind': 'agent',
             'attach_format': FORMAT}
    import routes.api_v1.desktop as d
    monkeypatch.setattr(d._dist_store, 'find_for_platform',
                        lambda *a, **kw: [entry])
    monkeypatch.setattr(d._dist_store, 'resolve_file',
                        lambda name: str(exe) if name == exe.name else None)
    kicked = []
    import lib.desktop_dist.winbuilder as wb
    monkeypatch.setattr(wb, 'is_running', lambda: False)
    monkeypatch.setattr(wb, 'start_installer',
                        lambda **kw: kicked.append(kw) or {'state': 'running'})
    return {'entry': entry, 'path': str(exe), 'kicked': kicked}


# ── 1-7: the one-file installer route ─────────────────────────────────


def _embedded_attach(response):
    from lib.desktop_dist.agent_installer import MAGIC, TRAILER_SIZE
    raw = response.get_data()
    assert len(raw) >= TRAILER_SIZE
    trailer = raw[-TRAILER_SIZE:]
    assert trailer.startswith(MAGIC)
    return json.loads(trailer[len(MAGIC):].decode('ascii'))

class TestAgentInstallerRoute:
    def setup_method(self):
        _clear_keys()

    def test_1_no_artifact_is_404(self, flask_client, monkeypatch):
        import routes.api_v1.desktop as d
        monkeypatch.setattr(d._dist_store, 'find_for_platform',
                            lambda *a, **kw: [])
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=_bearer())
        assert r.status_code == 404

    def test_2_stale_artifact_is_409_and_kicks_rebuild(self, flask_client,
                                                       fake_artifact,
                                                       monkeypatch):
        import routes.api_v1.desktop as d
        monkeypatch.setattr(d, '_agent_installer_ready', lambda entry: False)
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=_bearer())
        assert r.status_code == 409
        assert fake_artifact['kicked'], 'a stale artifact must kick a rebuild'
        assert fake_artifact['kicked'][0].get('target') == 'agent'

    def test_3_one_exe_embeds_attach_with_scoped_token(
            self, flask_client, fake_artifact, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '0.0.0.0')
        monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
        from lib.desktop import pairing as pairing_mod
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '10.9.8.7')
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=_bearer('carol'))
        assert r.status_code == 200, r.get_data(as_text=True)
        cd = r.headers.get('Content-Disposition', '')
        assert 'TofuAgent-Setup-9.9.9-win64.exe' in cd, cd
        assert '.zip' not in cd.lower()
        assert r.headers['Content-Type'].startswith(
            'application/vnd.microsoft.portable-executable')
        attach = _embedded_attach(r)
        assert attach['kind'] == 'tofu-agent-attach'
        token = attach['token']
        assert token.startswith('tofu_live_')
        # The token is agents:bridge-scoped AND bound to the downloading
        # user (RWA P4a tenant isolation).
        from lib.api_keys import list_keys
        row = next(k for k in list_keys(owner_user_id=OWNER_USER_ID)
                   if k.get('scopes')
                   and 'agents:bridge' in k['scopes']
                   and k.get('name', '').startswith('agent-attach-'))
        assert row['owner_user_id'] == OWNER_USER_ID
        # …and the token actually authorizes a poll as that user.
        poll = flask_client.post(
            '/api/desktop/poll',
            json={'results': [], 'streams': [],
                  'agent': {'agent_id': 'bundle-test', 'name': 'pc',
                            'platform': 'windows', 'capabilities': {},
                            'share_roots': []}},
            headers={'X-Bridge-Secret': token})
        assert poll.status_code == 200

    def test_4_candidate_order_direct_first_live_base_last(
            self, flask_client, fake_artifact, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '0.0.0.0')
        monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
        from lib.desktop import pairing as pairing_mod
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '10.9.8.7')
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=_bearer())
        attach = _embedded_attach(r)
        assert attach['candidates'] == ['http://10.9.8.7:15000']
        # The browser-reachable base (host_url here) is a FALLBACK, never
        # the primary — an SSO edge 401s a cookieless agent.
        assert attach['fallback_candidates'], 'live base must be present'
        assert all('10.9.8.7' not in u
                   for u in attach['fallback_candidates'])

    def test_5_loopback_bind_omits_direct_candidate(self, flask_client,
                                                    fake_artifact,
                                                    monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=_bearer())
        attach = _embedded_attach(r)
        assert attach['candidates'] == []

    def test_6_base_param_is_host_pinned(self, flask_client, fake_artifact,
                                         monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        monkeypatch.delenv('VSCODE_PROXY_URI', raising=False)
        # A base naming a FOREIGN host must never be adopted into a bundle
        # carrying a fresh credential.
        r = flask_client.get(
            '/api/v1/desktop/agent-installer?base=https://evil.example.com',
            headers=_bearer())
        attach = _embedded_attach(r)
        assert not any('evil.example.com' in u
                       for u in attach['fallback_candidates'])

    def test_7b_readiness_is_proved_by_the_artifact_not_git_history(
            self, fake_artifact):
        import routes.api_v1.desktop as d
        assert d._agent_installer_ready(fake_artifact['entry']) is True
        with open(fake_artifact['path'], 'r+b') as f:
            f.seek(-1000, os.SEEK_END)
            f.write(b'NOPE')
        assert d._agent_installer_ready(fake_artifact['entry']) is False

    def test_7_mint_failure_never_serves_an_unusable_installer(
            self, flask_client, fake_artifact, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        import lib.api_keys as ak
        auth = _bearer()  # mint BEFORE the keystore goes down

        def _boom(*a, **kw):
            raise RuntimeError('keystore down')
        monkeypatch.setattr(ak, 'create_key', _boom)
        r = flask_client.get('/api/v1/desktop/agent-installer',
                             headers=auth)
        assert r.status_code == 503
        assert '.exe' not in r.headers.get('Content-Disposition', '')

    def test_7c_legacy_bundle_url_also_returns_exe_not_zip(
            self, flask_client, fake_artifact):
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer())
        assert r.status_code == 200
        assert r.headers['Content-Type'].startswith(
            'application/vnd.microsoft.portable-executable')
        assert '.exe' in r.headers.get('Content-Disposition', '')
        assert '.zip' not in r.headers.get('Content-Disposition', '').lower()


# ── 7d-7h: the browser-pushed attach bundle route ─────────────────────
# The macOS/Linux zero-config channel: same payload construction as the
# .exe trailer, served as JSON to the signed-in page, which relays it to
# the unattached agent's loopback broker.

class TestAgentAttachBundleRoute:
    def setup_method(self):
        _clear_keys()

    def test_7d_requires_auth(self, flask_client):
        r = flask_client.post('/api/v1/desktop/agent-attach-bundle')
        assert r.status_code == 401

    def test_7e_mints_the_same_bundle_shape_as_the_exe(
            self, flask_client, monkeypatch):
        """One construction, two channels: direct LAN first, the page's
        own base as the fallback, a fresh user-bound agents:bridge token
        that actually authorizes a poll."""
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '0.0.0.0')
        monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
        from lib.desktop import pairing as pairing_mod
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '10.9.8.7')
        r = flask_client.post('/api/v1/desktop/agent-attach-bundle',
                              headers=_bearer('mac'))
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body['kind'] == 'tofu-agent-attach'
        assert body['candidates'] == ['http://10.9.8.7:15000']
        assert body['fallback_candidates'], 'the page base must be present'
        token = body['token']
        assert token.startswith('tofu_live_')
        from lib.api_keys import list_keys
        row = next(k for k in list_keys(owner_user_id=OWNER_USER_ID)
                   if 'agents:bridge' in (k.get('scopes') or [])
                   and k.get('name', '').startswith('agent-attach-'))
        assert row['owner_user_id'] == OWNER_USER_ID
        poll = flask_client.post(
            '/api/desktop/poll',
            json={'results': [], 'streams': [],
                  'agent': {'agent_id': 'push-test', 'name': 'mac',
                            'platform': 'darwin', 'capabilities': {},
                            'share_roots': []}},
            headers={'X-Bridge-Secret': token})
        assert poll.status_code == 200

    def test_7f_base_param_is_host_pinned(self, flask_client, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        monkeypatch.delenv('VSCODE_PROXY_URI', raising=False)
        r = flask_client.post(
            '/api/v1/desktop/agent-attach-bundle?base=https://evil.example.com',
            headers=_bearer())
        assert r.status_code == 200
        assert not any('evil.example.com' in u
                       for u in r.get_json()['fallback_candidates'])

    def test_7g_mint_failure_is_a_503(self, flask_client, monkeypatch):
        import lib.api_keys as ak
        auth = _bearer()

        def _boom(*a, **kw):
            raise RuntimeError('keystore down')
        monkeypatch.setattr(ak, 'create_key', _boom)
        r = flask_client.post('/api/v1/desktop/agent-attach-bundle',
                              headers=auth)
        assert r.status_code == 503, (
            'a device package must never ship without a usable credential')


# ── 8-13: agent-side import ───────────────────────────────────────────

class TestImportAttachBundle:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                           str(tmp_path / 'desktop_agent.json'))
        exe_dir = tmp_path / 'installed'
        exe_dir.mkdir()
        calls = {'probe': [], 'discover': 0}
        import lib.desktop_agent._probe as probe_mod
        import lib.desktop_agent._pair as pair_mod
        monkeypatch.setattr(probe_mod, 'probe_server',
                            lambda url, timeout=4.0:
                            (calls['probe'].append(url) or (False, 'dead')))
        monkeypatch.setattr(pair_mod, 'discover',
                            lambda log=None: calls.__setitem__(
                                'discover', calls['discover'] + 1) or '')
        from lib.desktop_agent import config as cfg_mod
        return {'exe_dir': str(exe_dir), 'calls': calls, 'cfg': cfg_mod}

    def _write_bundle(self, env, **over):
        import os.path
        bundle = {'v': 1, 'kind': 'tofu-agent-attach',
                  'token': 'tofu_live_TEST',
                  'candidates': ['http://10.1.1.1:15000'],
                  'fallback_candidates': ['https://proxy.example.com/proxy/15000']}
        bundle.update(over)
        path = os.path.join(env['exe_dir'], 'tofu-agent-attach.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(bundle, f)
        return path

    def test_8_no_file_is_a_noop(self, env):
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is False
        assert env['cfg'].remote_server() == ('', '')

    def test_9_probed_candidate_wins_token_saved_file_deleted(self, env):
        path = self._write_bundle(env)
        import lib.desktop_agent._probe as probe_mod
        probe_mod.probe_server = (
            lambda url, timeout=4.0: (True, '') if '10.1.1.1' in url
            else (False, 'dead'))
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is True
        assert env['cfg'].remote_server() == ('http://10.1.1.1:15000',
                                              'tofu_live_TEST')
        assert env['cfg'].load_config().get('attach_candidates') == [
            'http://10.1.1.1:15000', 'https://proxy.example.com/proxy/15000']
        assert not os.path.exists(path), 'the token file must be one-shot'

    def test_10_probe_order_candidates_then_ladder_then_fallbacks(self,
                                                                  env):
        self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        import lib.desktop_agent._pair as pair_mod
        hits = {'n': 0}

        def _discover(log=None):
            hits['n'] += 1
            return 'http://127.0.0.1:15000'
        pair_mod.discover = _discover
        assert import_attach_bundle(env['exe_dir']) is True
        # Candidates probed FIRST, the ladder consulted BEFORE the
        # SSO-risky fallback: the fallback URL must never have been probed.
        assert env['calls']['probe'] == ['http://10.1.1.1:15000']
        assert hits['n'] == 1
        assert env['cfg'].remote_server()[0] == 'http://127.0.0.1:15000'

    def test_11_nothing_answers_saves_first_candidate_optimistically(
            self, env):
        path = self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is True
        assert env['cfg'].remote_server() == ('http://10.1.1.1:15000',
                                              'tofu_live_TEST')
        assert not os.path.exists(path)

    def test_12_live_attachment_never_overridden(self, env):
        """A LIVE saved attachment is the user's own connect — the bundle
        must not touch it. (The env fixture's probe answers dead for
        everything; here the saved address answers ALIVE.)"""
        env['cfg'].save_remote_server('http://mine:15000', 'tofu_live_MINE')
        import lib.desktop_agent._probe as probe_mod
        probe_mod.probe_server = (
            lambda url, timeout=4.0: (True, '') if 'mine' in url
            else (False, 'dead'))
        path = self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is False
        assert env['cfg'].remote_server() == ('http://mine:15000',
                                              'tofu_live_MINE')
        assert not os.path.exists(path), 'even a refused bundle is one-shot'

    def test_12b_dead_attachment_is_repointed_by_the_bundle(self, env):
        """The 2026-08-06 incident pin: a DEAD saved route must NOT veto a
        freshly downloaded bundle — re-downloading IS the repair path.
        (env's probe answers dead for everything, so the optimistic first
        candidate wins and the dead address becomes a trailing backup.)"""
        env['cfg'].save_remote_server('http://dead-proxy:15000',
                                      'tofu_live_OLD')
        path = self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is True
        assert env['cfg'].remote_server() == ('http://10.1.1.1:15000',
                                              'tofu_live_TEST'), (
            'the bundle route + its FRESH token must replace the dead one')
        assert env['cfg'].load_config().get('attach_candidates') == [
            'http://10.1.1.1:15000',
            'https://proxy.example.com/proxy/15000',
            'http://dead-proxy:15000'], (
            'the dead address is kept as a TRAILING candidate only')
        assert not os.path.exists(path)

    def test_12c_dead_attachment_repoints_to_a_probed_live_candidate(
            self, env):
        """Repair with a reachable bundle candidate: the probed-alive
        route wins (no optimistic guess), token still refreshed."""
        env['cfg'].save_remote_server('http://dead-proxy:15000',
                                      'tofu_live_OLD')
        import lib.desktop_agent._probe as probe_mod
        probe_mod.probe_server = (
            lambda url, timeout=4.0: (True, '') if '10.1.1.1' in url
            else (False, 'dead'))
        self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is True
        assert env['cfg'].remote_server() == ('http://10.1.1.1:15000',
                                              'tofu_live_TEST')

    def test_12d_bundle_without_token_keeps_the_old_secret(self, env):
        """An open-bridge download bakes no token — the repair must keep
        the attachment's existing secret rather than blanking it."""
        env['cfg'].save_remote_server('http://dead-proxy:15000',
                                      'tofu_live_OLD')
        self._write_bundle(env, token='')
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is True
        assert env['cfg'].remote_server() == ('http://10.1.1.1:15000',
                                              'tofu_live_OLD')

    def test_13_malformed_bundle_deleted_and_ignored(self, env):
        path = os.path.join(env['exe_dir'], 'tofu-agent-attach.json')
        with open(path, 'w') as f:
            f.write('{not json')
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is False
        assert not os.path.exists(path)
        assert env['cfg'].remote_server() == ('', '')


# ── 14-15: resume walks the persisted candidates ──────────────────────

class TestResumeAttachCandidates:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                           str(tmp_path / 'desktop_agent.json'))
        import lib.desktop_agent._pair as pair_mod
        return pair_mod

    def test_14_dead_saved_url_repoints_via_candidates(self, env,
                                                       monkeypatch):
        pair_mod = env
        from lib.desktop_agent import config as cfg_mod
        cfg = cfg_mod.load_config()
        cfg['attach_candidates'] = ['http://10.2.2.2:15000']
        cfg_mod.save_config(cfg)
        monkeypatch.setattr(pair_mod, 'probe_server',
                            lambda url, timeout=4.0:
                            (True, '') if '10.2.2.2' in url
                            else (False, 'dead'))
        monkeypatch.setattr(pair_mod, 'discover',
                            lambda log=None: (_ for _ in ()).throw(
                                AssertionError('ladder must not run — a '
                                               'candidate already won')))
        url, secret = pair_mod.resume_attachment(
            'http://dead-proxy.example.com', 'tofu_live_KEPT')
        assert url == 'http://10.2.2.2:15000'
        assert secret == 'tofu_live_KEPT', 'the token rides along'
        assert cfg_mod.remote_server()[0] == 'http://10.2.2.2:15000'

    def test_15_candidates_dead_falls_to_ladder(self, env, monkeypatch):
        pair_mod = env
        from lib.desktop_agent import config as cfg_mod
        cfg = cfg_mod.load_config()
        cfg['attach_candidates'] = ['http://10.3.3.3:15000']
        cfg_mod.save_config(cfg)
        monkeypatch.setattr(pair_mod, 'probe_server',
                            lambda url, timeout=4.0: (False, 'dead'))
        monkeypatch.setattr(pair_mod, 'discover',
                            lambda log=None: 'http://127.0.0.1:15000')
        url, secret = pair_mod.resume_attachment('http://dead', 'tok')
        assert url == 'http://127.0.0.1:15000'
        assert secret == 'tok'


# ── 16-17: the diagnostics inbox + probe/poll transport alignment ────

class TestClientDiagInbox:
    """The 2026-08-06 owner ask: a controlled machine that cannot reach the
    server cannot push its logs — the user pastes the agent's「复制诊断信
    息」bundle into the panel and it lands on the server's disk."""

    @pytest.fixture
    def diag_log(self, tmp_path, monkeypatch):
        import routes.api_v1.desktop as d
        path = tmp_path / 'diag.log'
        monkeypatch.setattr(d, '_DIAG_LOG', str(path))
        return path

    def setup_method(self):
        _clear_keys()

    def test_16_post_stores_a_jsonl_line_bound_to_the_caller(
            self, flask_client, diag_log):
        r = flask_client.post('/api/v1/desktop/client-diag',
                              json={'text': 'Tofu Agent diagnostics\nlink: proxy'},
                              headers=_bearer('diag'))
        assert r.status_code == 200, r.get_data(as_text=True)
        lines = diag_log.read_text(encoding='utf-8').strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry['owner_user_id'] == OWNER_USER_ID
        assert 'link: proxy' in entry['text']
        assert entry['ts'] > 0

    def test_16b_empty_paste_is_a_400(self, flask_client, diag_log):
        r = flask_client.post('/api/v1/desktop/client-diag',
                              json={'text': '   '}, headers=_bearer())
        assert r.status_code == 400
        assert not diag_log.exists()

    def test_16c_oversize_paste_is_refused(self, flask_client, diag_log):
        import routes.api_v1.desktop as d
        r = flask_client.post('/api/v1/desktop/client-diag',
                              json={'text': 'x' * (d._DIAG_MAX_CHARS + 1)},
                              headers=_bearer())
        assert r.status_code == 400

    def test_16d_get_returns_newest_first_with_preview(
            self, flask_client, diag_log):
        for i in range(3):
            flask_client.post('/api/v1/desktop/client-diag',
                              json={'text': 'report-%d %s' % (i, 'y' * 300)},
                              headers=_bearer())
        r = flask_client.get('/api/v1/desktop/client-diag',
                             headers=_bearer())
        assert r.status_code == 200
        entries = r.get_json()['entries']
        assert len(entries) == 3
        assert 'report-2' in entries[0]['preview'], 'newest first'
        assert entries[0]['chars'] > 200
        assert len(entries[0]['preview']) == 200, 'preview is truncated'

    def test_16e_get_without_any_submission_is_an_empty_list(
            self, flask_client, diag_log):
        r = flask_client.get('/api/v1/desktop/client-diag',
                             headers=_bearer())
        assert r.status_code == 200
        assert r.get_json()['entries'] == []


class TestProbeTransportAlignment:
    def test_17_probe_bypasses_env_proxies_like_the_poll_loop(
            self, monkeypatch):
        """requests honors system/env proxies by default; the poll loop
        bypasses them ALL (no_proxy='*'). A probe on a different transport
        measures a route the poll will never take — the two must agree."""
        import requests
        import lib.desktop_agent._probe as probe_mod
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {'bootId': 'x'}

        def _get(url, **kw):
            captured.update(kw)
            return _Resp()

        monkeypatch.setattr(requests, 'get', _get)
        ok, reason = probe_mod.probe_server('http://10.0.0.1:15000')
        assert ok and reason == ''
        assert captured.get('proxies') == {'no_proxy': '*'}, (
            'the probe must measure the SAME transport the poll loop uses')
