"""Browser-pushed zero-config attach — the agent-side policy contract.

WHY THIS SUITE
--------------
The personalized installer is a Windows-only artifact (NSIS trailer), so
macOS/Linux agents attach by receiving their routes + a fresh credential
from the signed-in Local Control page over the loopback broker. The broker
(``_browser_relay.py``) owns the transport gate; THIS module
(``_push_attach.handle_pushed_attach``) owns the policy:

  * a page may only attach the agent to THE PAGE'S OWN server — the
    unforgeable Origin must own one of the bundle's routes (the bootstrap
    anti-drive-by rule; there is no configured origin to gate on yet);
  * a LIVE saved attachment refuses the push; a DEAD one is re-pointed and
    kept as a trailing candidate (the 2026-08-06 repair rule, shared with
    ``import_attach_bundle``);
  * a probed-alive route wins and reports transport 'direct'; when nothing
    answers a cookieless probe (an SSO edge) the page's own origin-matched
    route is saved optimistically and reports 'browser' — the page then
    keeps relaying polls;
  * shape caps: http(s) only, ≤8 URLs per list, ≤300 chars each, the token
    bounded — the payload is attested by no one, so it stays small.

Run:  pytest tests/test_agent_push_attach.py -q
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

ORIGIN = 'https://tofu.example'
LAN = 'http://10.9.8.7:15000'
PAGE = 'https://tofu.example'


def _bundle(**over):
    bundle = {'v': 1, 'kind': 'tofu-agent-attach', 'token': 'tofu_live_NEW',
              'candidates': [LAN], 'fallback_candidates': [PAGE]}
    bundle.update(over)
    return bundle


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated config + a probe seam whose alive set is test-driven."""
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    import lib.desktop_agent._probe as probe_mod
    probes = {'alive': set(), 'calls': []}

    def _probe(url, timeout=4.0):
        probes['calls'].append(url)
        alive = url in probes['alive']
        return alive, '' if alive else 'dead'

    monkeypatch.setattr(probe_mod, 'probe_server', _probe)
    from lib.desktop_agent import config as cfg_mod
    return {'probes': probes, 'cfg': cfg_mod}


def _handle(payload, origin=ORIGIN):
    from lib.desktop_agent._push_attach import handle_pushed_attach
    return handle_pushed_attach(payload, origin)


# ── shape caps ────────────────────────────────────────────────────────

def test_a_non_dict_payload_is_bad_shape(env):
    ok, reason, url, transport = _handle(['not', 'a', 'dict'])
    assert (ok, reason) == (False, 'bad_shape')
    assert env['cfg'].remote_server() == ('', '')


def test_a_bundle_with_no_usable_routes_is_refused(env):
    for payload in ({}, {'candidates': []},
                    {'candidates': ['javascript:alert(1)', 'ftp://x'],
                     'fallback_candidates': ['not-a-url']},
                    {'candidates': ['http://' + 'x' * 400]}):
        ok, reason, _url, _t = _handle(payload)
        assert (ok, reason) == (False, 'no_routes'), payload
    assert env['cfg'].remote_server() == ('', '')


def test_route_lists_are_capped(env, monkeypatch):
    env['probes']['alive'].add(LAN)
    many = ['http://10.0.0.%d:15000' % i for i in range(20)]
    ok, reason, url, transport = _handle(_bundle(candidates=[LAN] + many))
    assert ok and transport == 'direct'
    saved = env['cfg'].load_config().get('attach_candidates') or []
    assert len(saved) <= 8 + 1, saved  # 8 cap + the fallback entry


# ── the origin-owns-a-route gate ──────────────────────────────────────

def test_a_foreign_page_cannot_point_the_agent_anywhere(env):
    ok, reason, _url, _t = _handle(_bundle(), origin='https://evil.example')
    assert (ok, reason) == (False, 'origin_mismatch')
    assert env['cfg'].remote_server() == ('', ''), (
        'a refused push must persist nothing')


def test_a_missing_origin_is_refused(env):
    ok, reason, _url, _t = _handle(_bundle(), origin='')
    assert (ok, reason) == (False, 'origin_mismatch')


# ── happy paths ───────────────────────────────────────────────────────

def test_a_probed_candidate_attaches_directly(env):
    env['probes']['alive'].add(LAN)
    ok, reason, url, transport = _handle(_bundle())
    assert (ok, reason, transport) == (True, 'attached', 'direct')
    assert url == LAN
    assert env['cfg'].remote_server() == (LAN, 'tofu_live_NEW')
    assert env['cfg'].load_config().get('attach_candidates') == [LAN, PAGE]


def test_nothing_alive_saves_the_pages_own_route_for_the_relay(env):
    """The SSO case: no route answers a cookieless probe, so the saved
    route must be the one the PAGE RELAY can carry (the page's own
    origin), never a blindly optimistic LAN guess — and the page is told
    to keep relaying ('browser')."""
    ok, reason, url, transport = _handle(_bundle())
    assert (ok, reason, transport) == (True, 'attached_optimistic',
                                       'browser')
    assert url == PAGE, 'the origin-matched route is the relayable one'
    assert env['cfg'].remote_server() == (PAGE, 'tofu_live_NEW')


# ── the live/dead existing-attachment rules ───────────────────────────

def test_a_live_attachment_refuses_the_push(env):
    env['cfg'].save_remote_server('http://mine:15000', 'tofu_live_MINE')
    env['probes']['alive'].add('http://mine:15000')
    ok, reason, _url, _t = _handle(_bundle())
    assert (ok, reason) == (False, 'already_attached')
    assert env['cfg'].remote_server() == ('http://mine:15000',
                                          'tofu_live_MINE')


def test_a_dead_attachment_is_repointed_and_kept_trailing(env):
    env['cfg'].save_remote_server('http://dead-proxy:15000', 'tofu_live_OLD')
    env['probes']['alive'].add(LAN)
    ok, reason, url, transport = _handle(_bundle())
    assert (ok, transport) == (True, 'direct')
    assert env['cfg'].remote_server() == (LAN, 'tofu_live_NEW'), (
        'the push refreshes BOTH the route and the token')
    assert env['cfg'].load_config().get('attach_candidates') == [
        LAN, PAGE, 'http://dead-proxy:15000'], (
        'the dead address survives only as a trailing candidate')


def test_a_tokenless_push_keeps_the_existing_secret(env):
    """Open-bridge servers mint no token; a repair push must not blank the
    secret the dead attachment still holds."""
    env['cfg'].save_remote_server('http://dead-proxy:15000', 'tofu_live_OLD')
    env['probes']['alive'].add(LAN)
    ok, _reason, _url, _t = _handle(_bundle(token=''))
    assert ok
    assert env['cfg'].remote_server() == (LAN, 'tofu_live_OLD')


def test_a_new_token_is_capped_but_kept(env):
    env['probes']['alive'].add(LAN)
    ok, _r, _u, _t = _handle(_bundle(token='t' * 900))
    assert ok
    assert len(env['cfg'].remote_server()[1]) == 512
