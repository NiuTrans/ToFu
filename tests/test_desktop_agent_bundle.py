"""tests/test_desktop_agent_bundle.py — the zero-config attach bundle
(owner decree 2026-08-05: pairing codes retired; the credential rides the
download, never the user's keyboard).

The measured failure this kills: an agent handed a browser-reachable proxy
URL (missing scheme, missing /proxy/<port> prefix, then SSO-401 for a
cookieless client) polled a wall forever — access.log showed ZERO agent
requests while the panel waited on a pairing code that could never be
redeemed through the dead address.

Pinned:

SERVER side (``GET /api/v1/desktop/agent-bundle``):
  1. no agent artifact in the store ⇒ 404;
  2. artifact git_sha ≠ HEAD ⇒ 409 AND a rebuild kick (a stale exe would
     silently ignore the attach file — serving it would be a lie);
  3. happy path ⇒ ZIP carries the exe + tofu-agent-attach.json, and the
     JSON holds a FRESH agents:bridge token bound to the caller;
  4. candidate ordering: direct LAN first (when the bind allows), the
     browser's live base LAST (SSO-edge risk — measured 2026-08-03);
  5. a loopback bind omits the direct candidate (advertising it would be
     a lie — same guard as the LAN discovery responder);
  6. ``?base=`` is adopted only when host-pinned to the request;
  7. a token-mint failure still serves the ZIP (open bridges poll fine
     without one — the extension zip's fail-open rule).

AGENT side (``desktop.connect_ui.import_attach_bundle``):
  8. no file ⇒ nothing happens;
  9. probed-alive candidate wins ⇒ attachment saved WITH the token, the
     whole route set persists as attach_candidates, the file is deleted
     (one-shot — it carries a bearer token);
 10. probe order: candidates → discovery ladder → fallbacks;
 11. nothing answers ⇒ the FIRST candidate is saved optimistically (the
     server may simply be off; the poll loop retries) and the file is
     still deleted;
 12. an existing attachment is NEVER overridden (file still deleted);
 13. a malformed bundle is deleted and ignored.

RESUME side (``lib.desktop_agent._pair.resume_attachment``):
 14. saved address dead ⇒ the persisted attach_candidates are walked
     BEFORE the discovery ladder, a hit re-points the attachment keeping
     the ORIGINAL token;
 15. candidates all dead ⇒ the ladder runs (and wins here).

Run:  pytest tests/test_desktop_agent_bundle.py -q -p no:napari -o addopts=
"""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]


# ── server-side helpers ───────────────────────────────────────────────

def _bearer(user_id='u-bundle'):
    from lib.api_keys import create_key
    row, token = create_key(name=f'test-{user_id}', scopes=['chat'],
                            user_id=user_id)
    return {'Authorization': f'Bearer {token}'}


def _clear_keys():
    from lib import api_keys
    api_keys._cache.clear()
    api_keys._cache_loaded = False


@pytest.fixture
def fake_artifact(tmp_path, monkeypatch):
    """Point the route's store seam at a one-file fake exe; returns a
    dict of knobs (entry, path, kicked)."""
    exe = tmp_path / 'TofuAgent-Setup-9.9.9-win64.exe'
    exe.write_bytes(b'fake-nsis-exe' * 64)
    entry = {'filename': exe.name, 'version': '9.9.9', 'git_sha': 'shaHEAD',
             'size': exe.stat().st_size, 'source': 'built', 'kind': 'agent'}
    import routes.api_v1.desktop as d
    monkeypatch.setattr(d._dist_store, 'find_for_platform',
                        lambda *a, **kw: [entry])
    monkeypatch.setattr(d._dist_store, 'resolve_file',
                        lambda name: str(exe) if name == exe.name else None)
    monkeypatch.setattr(d, '_attach_flow_sha', lambda: 'shaHEAD')
    monkeypatch.setattr(d, '_contains_fix',
                        lambda fix, sha: fix == sha)
    kicked = []
    import lib.desktop_dist.winbuilder as wb
    monkeypatch.setattr(wb, 'is_running', lambda: False)
    monkeypatch.setattr(wb, 'start_installer',
                        lambda **kw: kicked.append(kw) or {'state': 'running'})
    return {'entry': entry, 'path': str(exe), 'kicked': kicked}


# ── 1-7: the bundle route ─────────────────────────────────────────────

class TestAgentBundleRoute:
    def setup_method(self):
        _clear_keys()

    def test_1_no_artifact_is_404(self, flask_client, monkeypatch):
        import routes.api_v1.desktop as d
        monkeypatch.setattr(d._dist_store, 'find_for_platform',
                            lambda *a, **kw: [])
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer())
        assert r.status_code == 404

    def test_2_stale_artifact_is_409_and_kicks_rebuild(self, flask_client,
                                                       fake_artifact,
                                                       monkeypatch):
        import routes.api_v1.desktop as d
        monkeypatch.setattr(d, '_contains_fix', lambda fix, sha: False)
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer())
        assert r.status_code == 409
        assert fake_artifact['kicked'], 'a stale artifact must kick a rebuild'
        assert fake_artifact['kicked'][0].get('target') == 'agent'

    def test_3_zip_carries_exe_and_attach_with_scoped_token(
            self, flask_client, fake_artifact, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '0.0.0.0')
        monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
        from lib.desktop import pairing as pairing_mod
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '10.9.8.7')
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer('u-carol'))
        assert r.status_code == 200, r.get_data(as_text=True)
        cd = r.headers.get('Content-Disposition', '')
        assert 'TofuAgent-Setup-9.9.9-win64.zip' in cd, cd
        zf = zipfile.ZipFile(io.BytesIO(r.get_data()))
        names = set(zf.namelist())
        assert 'TofuAgent-Setup-9.9.9-win64.exe' in names
        assert 'tofu-agent-attach.json' in names
        attach = json.loads(zf.read('tofu-agent-attach.json'))
        assert attach['kind'] == 'tofu-agent-attach'
        token = attach['token']
        assert token.startswith('tofu_live_')
        # The token is agents:bridge-scoped AND bound to the downloading
        # user (RWA P4a tenant isolation).
        from lib.api_keys import list_keys
        row = next(k for k in list_keys() if k.get('scopes')
                   and 'agents:bridge' in k['scopes']
                   and k.get('name', '').startswith('agent-attach-'))
        assert (row.get('user_id') or '') == 'u-carol'
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
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer())
        attach = json.loads(zipfile.ZipFile(io.BytesIO(r.get_data()))
                            .read('tofu-agent-attach.json'))
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
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=_bearer())
        attach = json.loads(zipfile.ZipFile(io.BytesIO(r.get_data()))
                            .read('tofu-agent-attach.json'))
        assert attach['candidates'] == []

    def test_6_base_param_is_host_pinned(self, flask_client, fake_artifact,
                                         monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        monkeypatch.delenv('VSCODE_PROXY_URI', raising=False)
        # A base naming a FOREIGN host must never be adopted into a bundle
        # carrying a fresh credential.
        r = flask_client.get(
            '/api/v1/desktop/agent-bundle?base=https://evil.example.com',
            headers=_bearer())
        attach = json.loads(zipfile.ZipFile(io.BytesIO(r.get_data()))
                            .read('tofu-agent-attach.json'))
        assert not any('evil.example.com' in u
                       for u in attach['fallback_candidates'])

    def test_7b_descendant_artifact_is_ready_ancestor_is_not(self):
        """The readiness gate is ANCESTRY, not equality (shared tree: sibling
        commits keep landing on top of the attach-flow commit — equality
        would flap the panel's readiness note on every unrelated one)."""
        import routes.api_v1.desktop as d
        fix = d._attach_flow_sha()
        if not fix:
            pytest.skip('repo unreadable here')
        # A descendant of the fix (HEAD) is ready; the fix's PARENT is not.
        import subprocess
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=d._REPO_ROOT,
                              capture_output=True, timeout=15)
        parent = subprocess.run(['git', 'rev-parse', fix + '^'],
                                cwd=d._REPO_ROOT, capture_output=True,
                                timeout=15)
        head_sha = head.stdout.decode().strip()
        parent_sha = parent.stdout.decode().strip()
        assert d._contains_fix(fix, head_sha) is True
        assert d._contains_fix(fix, parent_sha) is False
        assert d._agent_bundle_ready({'git_sha': head_sha}) is True
        assert d._agent_bundle_ready({'git_sha': parent_sha}) is False

    def test_7_mint_failure_still_serves_zip(self, flask_client,
                                             fake_artifact, monkeypatch):
        monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
        import lib.api_keys as ak
        auth = _bearer()  # mint BEFORE the keystore goes down

        def _boom(*a, **kw):
            raise RuntimeError('keystore down')
        monkeypatch.setattr(ak, 'create_key', _boom)
        r = flask_client.get('/api/v1/desktop/agent-bundle',
                             headers=auth)
        assert r.status_code == 200
        attach = json.loads(zipfile.ZipFile(io.BytesIO(r.get_data()))
                            .read('tofu-agent-attach.json'))
        assert attach['token'] == ''


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

    def test_12_existing_attachment_never_overridden(self, env):
        env['cfg'].save_remote_server('http://mine:15000', 'tofu_live_MINE')
        path = self._write_bundle(env)
        from desktop.connect_ui import import_attach_bundle
        assert import_attach_bundle(env['exe_dir']) is False
        assert env['cfg'].remote_server() == ('http://mine:15000',
                                              'tofu_live_MINE')
        assert not os.path.exists(path), 'even a refused bundle is one-shot'

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
