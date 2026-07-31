"""tests/test_mcp_remote_transport.py — remote MCP transport + credential safety.

Two behaviours are pinned here, both asserted on RESULTS rather than on
implementation details (charter: "测试守卫必须断言「结果」而非「实现」"):

1. **A remote MCP server that requires an auth header can actually be
   reached.** Verified against a REAL streamable-http MCP server started on
   localhost which rejects any request without the right bearer token. The
   assertion is "the tool call returned the right answer", so the guard keeps
   biting if the transport is reimplemented.

2. **No credential ever leaves the process through the config surface.** The
   assertion is "the secret string does not appear anywhere in the serialized
   response", not "field X was popped" — so it also catches a *new*
   secret-bearing field being added later.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_mcp_remote_transport.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SECRET = 'sk-tofu-rollinggo-SUPERSECRET-9f3a'


# ── A real authenticated streamable-http MCP server ──────────────────

_SERVER_SRC = textwrap.dedent('''
    import sys
    from mcp.server.fastmcp import FastMCP
    from starlette.responses import JSONResponse

    PORT = int(sys.argv[1])
    SECRET = sys.argv[2]

    mcp = FastMCP('authcheck', host='127.0.0.1', port=PORT)

    @mcp.tool()
    def whoami() -> str:
        """Return a fixed string proving the authenticated call went through."""
        return 'authenticated-ok'

    app = mcp.streamable_http_app()

    class RequireBearer:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                await self.inner(scope, receive, send)
                return
            hdrs = {k.decode().lower(): v.decode()
                    for k, v in scope.get('headers') or []}
            from urllib.parse import parse_qs
            qs = parse_qs((scope.get('query_string') or b'').decode())
            # Accept EITHER auth style, mirroring the two real vendor shapes:
            # a Bearer header (RollingGo) or a ?key= query param (Amap).
            # Exact comparison, not a substring test: `key=<SECRET>-WRONG`
            # contains `key=<SECRET>` and would otherwise authenticate.
            ok = (hdrs.get('authorization') == f'Bearer {SECRET}'
                  or qs.get('key', [None])[0] == SECRET)
            if not ok:
                resp = JSONResponse({'error': 'unauthorized'}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.inner(scope, receive, send)

    import uvicorn
    uvicorn.run(RequireBearer(app), host='127.0.0.1', port=PORT,
                log_level='error')
''')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def auth_server(tmp_path_factory):
    """Start the bearer-protected MCP server; yield its /mcp URL."""
    port = _free_port()
    script = tmp_path_factory.mktemp('mcpsrv') / 'server.py'
    script.write_text(_SERVER_SRC)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port), SECRET],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    url = f'http://127.0.0.1:{port}/mcp'
    for _ in range(100):
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.skip(f'MCP test server died: {err.decode()[:400]}')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip('MCP test server did not come up')
    yield url
    proc.kill()
    proc.wait(timeout=10)


# ── 1. Reachability of an authenticated remote MCP server ────────────

def _remote_cfg(url: str) -> dict:
    return {
        'transport': 'streamable-http',
        'url': url,
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'env': {'ROLLINGGO_API_KEY': SECRET},
        'enabled': True,
    }


def test_authenticated_remote_server_is_reachable(auth_server):
    """The whole point: a Bearer-gated remote MCP server yields its tools.

    Without header pass-through the server answers 401 and this fails — which
    is exactly the NEUTER for the credential-plumbing half of the change.
    """
    from lib.mcp.client import MCPBridge

    bridge = MCPBridge()
    try:
        tools = bridge.connect_server('authcheck', _remote_cfg(auth_server))
        assert [t.name for t in tools] == ['whoami'], \
            f'expected the remote tool to be discovered, got {tools}'

        result = bridge.call_tool('mcp__authcheck__whoami', {})
        assert 'authenticated-ok' in str(result), \
            f'authenticated tool call did not return its answer: {result!r}'
    finally:
        bridge.disconnect_all()


def test_unresolved_header_credential_names_the_missing_key():
    """A blank credential fails with the key name, not an opaque upstream 401."""
    from lib.mcp.transport import resolve_headers

    cfg = {'transport': 'streamable-http', 'url': 'http://x/mcp',
           'headers': {'Authorization': 'Bearer ${NOPE_MISSING_KEY}'}, 'env': {}}
    with pytest.raises(ValueError) as ei:
        resolve_headers(cfg, server_name='rollinggo')
    assert 'NOPE_MISSING_KEY' in str(ei.value)


def test_env_supplies_header_value_not_a_second_secret_store():
    """Credentials resolve from the SAME env block the UI already redacts."""
    from lib.mcp.transport import resolve_headers

    cfg = {'headers': {'Authorization': 'Bearer ${K}', 'X-Trace': 'plain'},
           'env': {'K': SECRET}}
    assert resolve_headers(cfg) == {
        'Authorization': f'Bearer {SECRET}', 'X-Trace': 'plain'}


# ── 2. Credentials never leave through the config surface ────────────

def test_redacted_config_never_carries_a_secret():
    """Result-level assertion: the secret is absent from the whole payload.

    Deliberately not "assert 'headers' not in out" — a future field holding a
    token would slip past that, but not past a substring scan of the JSON.
    """
    from lib.mcp.transport import redact_config

    cfg = _remote_cfg('http://127.0.0.1:1/mcp')
    out = redact_config(cfg)
    assert SECRET not in json.dumps(out, ensure_ascii=False), \
        f'credential leaked through redact_config: {out}'
    # The *shape* stays visible so the UI can show that the server authenticates.
    assert 'Authorization' in out.get('headers', {})


def test_redaction_masks_a_hand_inlined_literal_token():
    """A hand-edited mcp_servers.json that inlines the token is masked too."""
    from lib.mcp.transport import redact_config

    cfg = {'transport': 'streamable-http', 'url': 'http://x/mcp',
           'headers': {'Authorization': f'Bearer {SECRET}'}}
    assert SECRET not in json.dumps(redact_config(cfg))


def test_list_servers_response_contains_no_credential(monkeypatch, tmp_path):
    """End-to-end on the real route: GET /api/v1/mcp/servers leaks nothing.

    The stored config deliberately carries the secret in BOTH shapes:
      - ``env`` (the normal, tool-written shape), and
      - a LITERAL token inlined in ``headers`` (what a hand-edited
        mcp_servers.json looks like).
    The literal is the one that matters here: a template-only config would
    pass this scan even with no redaction at all, so a guard built purely on
    ``${VAR}`` fixtures cannot tell "redaction works" from "there was nothing
    to redact". With the literal present, returning the raw config turns this
    red — which is what makes it a real NEUTER target.
    """
    import lib.mcp.config as mcfg

    stored = _remote_cfg('http://127.0.0.1:1/mcp')
    # Hand-edited shape: token pasted straight into the header block.
    stored['headers'] = {'Authorization': f'Bearer {SECRET}',
                         'X-Api-Key': SECRET}

    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    (cfg_dir / 'mcp_servers.json').write_text(
        json.dumps({'rollinggo': stored}), encoding='utf-8')
    monkeypatch.setattr(mcfg, '_CONFIG_DIR', str(cfg_dir))

    import routes.api_v1.mcp as mcp_routes

    class _FakeBridge:
        def list_servers(self):
            return []

        def get_breaker_state(self, name):
            return None

        def get_cred_health(self, name):
            return None

    captured = {}

    def _fake_ok(payload):
        captured['payload'] = payload
        return payload

    monkeypatch.setattr(mcp_routes, 'api_ok', _fake_ok)
    monkeypatch.setattr('lib.mcp.get_bridge', lambda: _FakeBridge())

    mcp_routes.list_servers_v1.__wrapped__() if hasattr(
        mcp_routes.list_servers_v1, '__wrapped__') else None
    # Call the undecorated body directly (auth decorator needs a request ctx).
    fn = mcp_routes.list_servers_v1
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    fn()

    blob = json.dumps(captured['payload'], ensure_ascii=False, default=str)
    assert SECRET not in blob, f'GET /servers leaked the credential: {blob}'
    assert 'rollinggo' in blob, 'server should still be listed'


# ── 3. Transport classification is explicit, not "not sse" ───────────

@pytest.mark.parametrize('transport,expect_stdio', [
    ('stdio', True),
    (None, True),
    ('sse', False),
    ('streamable-http', False),
    ('http', False),            # Claude CLI spelling
    ('streamable_http', False),  # Codex/docs spelling
])
def test_stdio_classification(transport, expect_stdio):
    """`stdio_command` must be empty for EVERY remote transport.

    The old `transport != 'sse'` idiom returned True for streamable-http,
    which would have sent a remote server down the subprocess-launcher path.
    """
    from lib.mcp.transport import is_stdio, stdio_command

    cfg = {'command': 'npx', 'url': 'http://x/mcp'}
    if transport is not None:
        cfg['transport'] = transport
    assert is_stdio(cfg) is expect_stdio
    assert bool(stdio_command(cfg)) is expect_stdio


def test_catalog_build_never_gives_a_remote_server_a_launcher(monkeypatch):
    """A remote catalog entry must build a URL config, not a subprocess one.

    `build_server_config` classified with `transport == 'sse'`, so a
    streamable-http entry fell into the stdio branch and would have been
    launched as a (nonexistent) local command. Asserted on the built config
    because that is what the bridge actually consumes.
    """
    import lib.mcp.registry as reg

    entry = {
        'id': 'rollinggo-hotel', 'name': 'RollingGo Hotel',
        'description': 'hotels', 'category': reg.CAT_OTHER,
        'transport': 'streamable-http',
        'endpoint': 'https://mcp.rollinggo.cn/mcp',
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'command': '', 'args': [],
        'env_specs': [{'key': 'ROLLINGGO_API_KEY', 'label': 'API Key',
                       'required': True, 'secret': True}],
    }
    monkeypatch.setattr(reg, 'get_catalog_entry', lambda sid: entry)

    cfg = reg.build_server_config('rollinggo-hotel',
                                  {'ROLLINGGO_API_KEY': SECRET})
    assert cfg['url'] == 'https://mcp.rollinggo.cn/mcp'
    assert 'command' not in cfg, \
        f'remote server was given a subprocess launcher: {cfg}'
    # The credential landed in env (the redacted store), and the header block
    # only references it.
    assert cfg['env']['ROLLINGGO_API_KEY'] == SECRET
    assert cfg['headers']['Authorization'] == 'Bearer ${ROLLINGGO_API_KEY}'

    from lib.mcp.transport import resolve_headers
    assert resolve_headers(cfg) == {'Authorization': f'Bearer {SECRET}'}


# ── 4. Query-param credentials (Amap) ride the same env store ────────
#
# Amap authenticates as `https://mcp.amap.com/mcp?key=<secret>` — the
# credential is in the URL, not a header. Without URL templating a user's only
# option is pasting the key literally into mcp_servers.json's url, which
# bypasses the "all credentials live in env" rule entirely.

_AMAP_URL_TMPL = 'https://mcp.amap.com/mcp?key=${AMAP_MAPS_API_KEY}'


def _amap_cfg() -> dict:
    return {
        'transport': 'streamable-http',
        'url': _AMAP_URL_TMPL,
        'env': {'AMAP_MAPS_API_KEY': SECRET},
        'enabled': True,
    }


def test_query_param_credential_resolves_from_env():
    """The URL is a template too, so the key still has exactly one home."""
    from lib.mcp.transport import resolve_url

    assert resolve_url(_amap_cfg(), server_name='amap') == \
        f'https://mcp.amap.com/mcp?key={SECRET}'


def test_query_param_credential_is_redacted_but_endpoint_stays_readable():
    """Mask the VALUE, keep scheme/host/path — a fully masked URL would make
    every remote server look identical in the settings UI and push users back
    to hand-editing the config file."""
    from lib.mcp.transport import redact_config

    out = redact_config({'transport': 'streamable-http',
                         'url': f'https://mcp.amap.com/mcp?key={SECRET}'})
    assert SECRET not in json.dumps(out), f'query credential leaked: {out}'
    assert out['url'] == 'https://mcp.amap.com/mcp?key=***'


def test_url_only_server_still_advertises_its_required_env_key():
    """A query-param vendor has NO headers, so header-only key discovery would
    report it as credential-free and the settings UI would never prompt."""
    from lib.mcp.transport import header_env_keys

    assert header_env_keys(_amap_cfg()) == ['AMAP_MAPS_API_KEY']


def test_missing_query_param_credential_names_the_key():
    from lib.mcp.transport import resolve_url

    with pytest.raises(ValueError) as ei:
        resolve_url({'url': _AMAP_URL_TMPL, 'env': {}}, server_name='amap')
    assert 'AMAP_MAPS_API_KEY' in str(ei.value)


def test_bridge_reaches_a_query_param_authenticated_server(auth_server):
    """END-TO-END: the BRIDGE must template the URL, not just the helper.

    Testing ``resolve_url`` alone leaves the wiring untested — deleting the
    bridge's call to it keeps a helper-only suite fully green while every
    query-param vendor (Amap) breaks in production. This drives a real
    handshake against a server that rejects an unresolved ``${VAR}``, so the
    assertion covers the call site rather than the function.
    """
    from lib.mcp.client import MCPBridge

    cfg = {
        'transport': 'streamable-http',
        'url': auth_server + '?key=${AMAP_MAPS_API_KEY}',
        'env': {'AMAP_MAPS_API_KEY': SECRET},
        'enabled': True,
    }
    bridge = MCPBridge()
    try:
        tools = bridge.connect_server('amap', cfg)
        assert [t.name for t in tools] == ['whoami'], (
            'query-param credential was not substituted before the request — '
            'the bridge sent the literal ${VAR} and got 401'
        )
        assert 'authenticated-ok' in str(bridge.call_tool('mcp__amap__whoami', {}))
    finally:
        bridge.disconnect_all()


# ── 5. Redaction is a fail-closed WHITELIST, not a blacklist ─────────

def test_unclassified_config_field_is_withheld_not_echoed():
    """The default for an unforeseen field must be DROP, not EXPOSE.

    Redaction started as a blacklist and three credential carriers were missed
    in a row (env → headers → url). This asserts the inverted default: a field
    nobody classified does not reach the caller, so the next carrier cannot
    leak just because someone forgot to add it to a deny list.
    """
    from lib.mcp.transport import redact_config

    out = redact_config({
        'transport': 'streamable-http', 'url': 'https://x/mcp',
        # A plausible future field that happens to carry a secret.
        'oauth_refresh_token': SECRET,
        'proxy_password': 'hunter2',
    })
    blob = json.dumps(out)
    assert SECRET not in blob, f'unclassified field leaked: {out}'
    assert 'hunter2' not in blob, f'unclassified field leaked: {out}'
    assert 'oauth_refresh_token' not in out
    assert out['transport'] == 'streamable-http'   # classified → still shown


def test_every_config_field_has_a_declared_exposure_level():
    """Ratchet: adding a field to MCPServerConfig without classifying it fails.

    Anchored on the TypedDict's annotations (a semantic unit), not on a
    hand-copied field list — so it tracks the real config shape.
    """
    from lib.mcp.transport import CLASSIFIED_CONFIG_FIELDS
    from lib.mcp.types import MCPServerConfig

    declared = set(MCPServerConfig.__annotations__)
    unclassified = declared - CLASSIFIED_CONFIG_FIELDS
    assert not unclassified, (
        f'MCPServerConfig field(s) {sorted(unclassified)} have no exposure '
        f'level. Classify them in lib/mcp/transport.py as PUBLIC_ / '
        f'TRANSFORMED_ / SECRET_CONFIG_FIELDS.'
    )


def test_secret_and_public_classifications_are_disjoint():
    """A field cannot be both echoed verbatim and withheld."""
    from lib.mcp.transport import (
        PUBLIC_CONFIG_FIELDS, SECRET_CONFIG_FIELDS, TRANSFORMED_CONFIG_FIELDS,
    )

    assert not (PUBLIC_CONFIG_FIELDS & SECRET_CONFIG_FIELDS)
    assert not (PUBLIC_CONFIG_FIELDS & TRANSFORMED_CONFIG_FIELDS)
    assert not (SECRET_CONFIG_FIELDS & TRANSFORMED_CONFIG_FIELDS)


# ── 6. The third exit: log sinks ────────────────────────────────────

def test_scrub_text_masks_a_credential_embedded_in_an_error_message():
    """httpx failures embed the RESOLVED request URL in their message.

    A remote connect failure therefore writes a live credential into
    app.log / error.log, where it persists far longer than an API response.
    This is the exit the two config endpoints do not cover.
    """
    from lib.mcp.transport import scrub_text

    raw = (f"Client error '401 Unauthorized' for url "
           f"'https://mcp.amap.com/mcp?key={SECRET}'\n"
           f'For more information check: https://example.com/401')
    out = scrub_text(raw)
    assert SECRET not in out, f'credential survived scrubbing: {out}'
    assert 'mcp.amap.com/mcp?key=***' in out
    # The non-credential URL keeps its shape so the message stays useful.
    assert 'https://example.com/401' in out


def test_connect_error_message_is_scrubbed(auth_server):
    """Result-level, on the REAL error type: a failed authenticated connect
    must not put the key in the string that gets logged."""
    from lib.mcp.client import MCPBridge, MCPConnectError

    # Authenticate with a WRONG key so the server answers 401 — a genuine
    # failure whose httpx message embeds the resolved URL (with the key in it).
    cfg = {
        'transport': 'streamable-http',
        'url': auth_server + '?key=${AMAP_MAPS_API_KEY}',
        'env': {'AMAP_MAPS_API_KEY': SECRET + '-WRONG'},
    }
    bridge = MCPBridge()
    try:
        with pytest.raises((MCPConnectError, Exception)) as ei:
            bridge.connect_server('amap-fail', cfg)
        assert SECRET not in str(ei.value), \
            f'connect error leaked the credential: {ei.value}'
    finally:
        bridge.disconnect_all()


# ── 7. Catalog: the China local-life entries are real, not decorative ──

_CN_LOCAL_IDS = ['amap-maps', 'rollinggo-hotel', 'rollinggo-flight',
                 '12306-train', 'tuniu-travel']


def test_china_local_entries_build_usable_configs():
    """Every new card must produce a config the bridge can actually consume.

    Asserted on the BUILT config (what the bridge consumes) rather than on the
    literal catalog dict, so a future entry that forgets `endpoint`, or lands a
    remote server in the stdio branch, fails here instead of becoming a dead
    Install button a user discovers.
    """
    import lib.mcp.registry as reg
    from lib.mcp.transport import is_stdio

    for sid in _CN_LOCAL_IDS:
        entry = reg.get_catalog_entry(sid)
        assert entry is not None, f'{sid} missing from catalog'
        assert entry['category'] == reg.CAT_LOCAL_CN

        env = {s['key']: SECRET for s in entry.get('env_specs', [])}
        cfg = reg.build_server_config(sid, env)
        assert cfg is not None, f'{sid} built no config'

        if is_stdio(cfg):
            assert cfg.get('command'), f'{sid}: stdio entry without a command'
        else:
            assert cfg.get('url'), f'{sid}: remote entry without an endpoint URL'
            assert 'command' not in cfg, \
                f'{sid}: remote entry was given a subprocess launcher'


def test_every_china_entry_credential_resolves_and_never_leaks():
    """Every credential-bearing entry delivers its key, and none leaks it.

    Split by CARRIER, because the three shapes are genuinely different and
    collapsing them would assert something false for two of them:
      - stdio (Tuniu): the key is handed to the subprocess as a real env var,
        so there is no ``${VAR}`` template to discover;
      - remote + header (RollingGo) and remote + query param (Amap): the key
        stays in ``env`` and the carrier only references it, so the template
        MUST advertise which env key it needs or the UI never prompts.
    """
    import lib.mcp.registry as reg
    from lib.mcp.transport import (
        header_env_keys, is_stdio, redact_config, resolve_headers, resolve_url,
    )

    for sid in _CN_LOCAL_IDS:
        entry = reg.get_catalog_entry(sid)
        specs = entry.get('env_specs', [])
        if not specs:
            continue                      # 12306 needs no credential
        keys = [s['key'] for s in specs]
        cfg = reg.build_server_config(sid, {k: SECRET for k in keys})

        if is_stdio(cfg):
            # The subprocess receives it as an environment variable.
            for k in keys:
                assert cfg['env'][k] == SECRET, \
                    f'{sid}: {k} never reached the subprocess env'
        else:
            # The UI must know which key to prompt for, whichever remote
            # carrier is used — a query-param vendor has no headers at all.
            assert header_env_keys(cfg), \
                f'{sid}: remote entry advertises no env key to prompt for'
            resolved = resolve_url(cfg, server_name=sid)
            resolved_hdrs = resolve_headers(cfg, server_name=sid)
            assert SECRET in resolved or SECRET in json.dumps(resolved_hdrs), \
                f'{sid}: credential never reached the request'

        assert SECRET not in json.dumps(redact_config(cfg)), \
            f'{sid}: credential leaked through redact_config'


def test_amap_key_rides_the_url_and_rollinggo_rides_a_header():
    """Pin the two DIFFERENT carriers explicitly — the reason URL templating
    exists at all. If Amap were header-only this whole mechanism would be
    unnecessary, so the asymmetry is the contract worth guarding."""
    import lib.mcp.registry as reg

    amap = reg.build_server_config('amap-maps', {'AMAP_MAPS_API_KEY': SECRET})
    assert '${AMAP_MAPS_API_KEY}' in amap['url']
    assert not amap.get('headers')

    hotel = reg.build_server_config('rollinggo-hotel',
                                    {'ROLLINGGO_API_KEY': SECRET})
    assert hotel['headers']['Authorization'] == 'Bearer ${ROLLINGGO_API_KEY}'
    assert '${' not in hotel['url']


def test_no_dead_card_for_a_business_gated_vendor():
    """Ctrip / Meituan must NOT get an entry — in EITHER catalog.

    Both were researched: Ctrip's MCP is corporate-only (its individual-facing
    'wendao' product is not MCP at all) and Meituan's open platform is
    merchant-side behind a business review. An entry would render an Install
    button that cannot succeed. Their status belongs in docs and a
    business-access ticket, not in a clickable card.

    Scans the SKILLS catalog too: vendors ship either shape (Fliggy is a skill,
    Tuniu is an MCP server), so guarding one surface would let the next dead
    card in through the other door.
    """
    import lib.mcp.registry as reg
    import lib.skills.catalog as skills

    banned = ('ctrip', 'xiecheng', '携程', 'meituan', '美团', 'dianping', '点评')
    for surface, entries in (('mcp', reg.get_catalog()),
                             ('skills', skills.get_catalog())):
        for entry in entries:
            haystack = f"{entry['id']} {entry.get('name', '')}".lower()
            # 'meituan' legitimately appears in internal_only research-cluster
            # entries (hope / xuecheng / llm) — those are corp-internal dev
            # tools, not consumer life-service cards.
            if entry.get('internal_only'):
                continue
            for token in banned:
                assert token not in haystack, (
                    f'{surface} catalog entry {entry["id"]!r} references '
                    f'{token!r} — these vendors require business onboarding, '
                    f'so a card is a dead Install button'
                )


def test_individually_accessible_travel_vendors_are_actually_shipped():
    """The complement of the ban list: vendors that DO open up must be present.

    Without this, "exclude the gated ones" degenerates into "ship nothing" and
    the ban guard alone would still pass. An earlier revision reasoned that
    Chinese OTAs keep inventory closed as a moat; measurement refuted it —
    Tuniu and Fliggy both hand out self-service credentials WITH a booking
    chain. Pinning them keeps that correction from silently regressing.
    """
    import lib.mcp.registry as reg
    import lib.skills.catalog as skills

    mcp_ids = {e['id'] for e in reg.get_catalog()}
    assert 'tuniu-travel' in mcp_ids, 'Tuniu MCP entry went missing'

    skill_ids = {e['id'] for e in skills.get_catalog()}
    assert 'flyai' in skill_ids, 'Fliggy FlyAI skill entry went missing'
