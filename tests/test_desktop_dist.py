"""tests/test_desktop_dist.py — server-hosted desktop downloads.

WHAT THIS GUARDS
----------------
The two halves of the "Download Desktop" rework (pt_a859c11e75d142d1):

1. **The settings-panel stall is gone at the root.** The desktop status
   endpoint used to probe ``api.github.com`` SYNCHRONOUSLY inside an async
   route (up to a 6 s timeout on the event loop every 900 s TTL expiry) —
   the measured reason the Local Control modal's desktop row always loaded
   last. The request path now performs ZERO network: downloads come from
   the local artifact store, and all GitHub traffic moved into the
   mirror's single-flight background thread. ``test_the_status_endpoint_
   serves_downloads_with_the_network_hard_down`` pins that property.

2. **The store/mirror behave.** Traversal-proof resolution, per-platform
   matching with built-beats-mirrored version preference, skip-unchanged
   (a 115 MB installer is not re-fetched every 6 h), prune-on-new-release
   that never strands a platform whose replacement failed to download,
   and stale-while-revalidate on probe failure.

Run:  pytest tests/test_desktop_dist.py -q
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from lib.desktop_dist import mirror, platforms, store

# ═══════════════════════════════════════════════════════════════════
#  Fixtures / fakes
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """An isolated artifact store (the env override is read at call time)."""
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path))
    yield tmp_path


def _entry(name, os_key='windows', arch='x86_64', label='Windows installer',
           size=100, source='mirrored', version='0.14.2', fetched_at=None):
    return {'os': os_key, 'arch': arch, 'label': label, 'filename': name,
            'size': size, 'sha256': 'ab' * 32, 'source': source,
            'version': version,
            'fetched_at': fetched_at if fetched_at is not None else time.time()}


def _seed(tmp_path, name, content=b'x' * 1024, **kw):
    (tmp_path / name).write_bytes(content)
    kw.setdefault('size', len(content))
    store.record_artifact(_entry(name, **kw))


def _fake_size(tag, name):
    """The byte count the fake downloader produces for this asset.

    Both seams derive from ONE formula because the skip-unchanged logic
    compares the release's advertised size against the recorded download
    size — two hand-kept numbers would be the drift the suite exists to
    catch, in miniature.
    """
    url = f'https://example.test/{tag}/{name}'
    return len((url + '|' + name).encode())


def _release(tag='v0.14.2', ver='0.14.2', sizes=None):
    """A probe payload whose asset names match the REAL platform globs."""
    sizes = sizes or {}
    names = [f'Tofu-{ver}-macos-arm64.dmg', f'Tofu-{ver}-macos-x86_64.dmg',
             f'Tofu-Setup-{ver}-win64.exe', f'Tofu-{ver}-linux-x86_64.tar.gz']
    return {'tag': tag,
            'assets': [{'name': n,
                        'url': f'https://example.test/{tag}/{n}',
                        'size': sizes.get(n, _fake_size(tag, n))}
                       for n in names]}


@pytest.fixture()
def fake_net(monkeypatch):
    """Mirror network faked at BOTH seams (probe + download), with recorders."""
    state = {'probe': None, 'downloads': [], 'fail_for': set()}

    def _probe(timeout=8.0):
        if isinstance(state['probe'], Exception):
            raise state['probe']
        return state['probe']

    import hashlib

    def _download(url, dest_part):
        name = os.path.basename(dest_part)[:-5]  # strip '.part'
        state['downloads'].append(name)
        if name in state['fail_for']:
            raise RuntimeError(f'boom for {name}')
        content = (url + '|' + name).encode()
        with open(dest_part, 'wb') as f:
            f.write(content)
        return len(content), hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(platforms, 'fetch_latest_release', _probe)
    monkeypatch.setattr(mirror, '_download', _download)
    return state


# ═══════════════════════════════════════════════════════════════════
#  store
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_resolve_file_is_exact_match_only(tmp_store):
    """The download route maps URL → file ONLY through a manifest key.

    Anything else — unknown names, dot segments, embedded slashes — must
    never reach the filesystem. This is the whole traversal defence.
    """
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')
    assert store.resolve_file('Tofu-Setup-0.14.2-win64.exe') is not None
    for bad in ('../server.py', '..', '.', 'a/b', 'a\\b',
                'Tofu-Setup-9.9.9-win64.exe', '', 'manifest.json'):
        assert store.resolve_file(bad) is None, bad


@pytest.mark.unit
def test_resolve_file_drops_entries_whose_file_vanished(tmp_store):
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')
    os.remove(tmp_store / 'Tofu-Setup-0.14.2-win64.exe')
    assert store.resolve_file('Tofu-Setup-0.14.2-win64.exe') is None


@pytest.mark.unit
def test_a_corrupt_manifest_is_an_empty_store_not_a_crash(tmp_store):
    (tmp_store / 'manifest.json').write_text('{not json', encoding='utf-8')
    assert store.artifacts() == {}
    assert store.find_for_platform('windows', 'x86_64') == []


@pytest.mark.unit
def test_find_for_platform_matches_the_shared_table(tmp_store):
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')
    _seed(tmp_store, 'Tofu-0.14.2-macos-arm64.dmg',
          os_key='macos', arch='arm64', label='macOS arm64 DMG')
    _seed(tmp_store, 'Tofu-0.14.2-macos-x86_64.dmg',
          os_key='macos', arch='x86_64', label='macOS x86_64 DMG')
    rows = store.find_for_platform('windows', 'x86_64')
    assert [r['filename'] for r in rows] == ['Tofu-Setup-0.14.2-win64.exe']
    # macOS with an UNKNOWN arch must offer BOTH DMGs — see the macOS trap
    # documented in tests/test_devices_panel_and_platform_download.py.
    both = store.find_for_platform('macos', '')
    assert sorted(r['arch'] for r in both) == ['arm64', 'x86_64']
    assert store.find_for_platform('plan9', '') == []


@pytest.mark.unit
def test_the_newer_version_wins_and_ties_go_to_the_build(tmp_store):
    """A stale local build must not pin the user below the mirrored release;
    a build of CURRENT code must beat the older mirrored one."""
    _seed(tmp_store, 'Tofu-0.13.0-linux-x86_64.tar.gz', os_key='linux',
          label='Linux archive', source='built', version='0.13.0')
    _seed(tmp_store, 'Tofu-0.14.2-linux-x86_64.tar.gz', os_key='linux',
          label='Linux archive', source='mirrored', version='0.14.2')
    rows = store.find_for_platform('linux', 'x86_64')
    assert rows[0]['filename'] == 'Tofu-0.14.2-linux-x86_64.tar.gz', rows
    # Now the build is the newer one — it must win instead.
    _seed(tmp_store, 'Tofu-0.16.0-linux-x86_64.tar.gz', os_key='linux',
          label='Linux archive', source='built', version='0.16.0')
    rows = store.find_for_platform('linux', 'x86_64')
    assert rows[0]['filename'] == 'Tofu-0.16.0-linux-x86_64.tar.gz', rows


@pytest.mark.unit
def test_an_agent_artifact_never_shadows_the_full_installer(tmp_store):
    """The kind axis: a freshly-wrapped agent installer is NEWER than the
    full build at the same version and same source — without the kind
    filter it wins the shared platform row and the panel offers the agent
    to a full-app seeker (the exact regression A2's first artifact
    introduced the moment it landed)."""
    _seed(tmp_store, 'Tofu-Setup-0.16.0-win64.exe', source='built',
          version='0.16.0', fetched_at=time.time() - 100)
    agent = _entry('TofuAgent-Setup-0.16.0-win64.exe', source='built',
                   version='0.16.0', label='Windows agent installer')
    agent['kind'] = 'agent'
    (tmp_store / agent['filename']).write_bytes(b'x' * 1024)
    store.record_artifact(agent)
    rows = store.find_for_platform('windows', 'x86_64')
    assert [r['filename'] for r in rows] == ['Tofu-Setup-0.16.0-win64.exe'], (
        'the agent shadowed the full installer on its own row')
    rows = store.find_for_platform('windows', 'x86_64', kind='agent')
    assert [r['filename'] for r in rows] == [
        'TofuAgent-Setup-0.16.0-win64.exe']
    # Legacy kindless entries read as 'full' — zero manifest migration.
    e = store.artifacts()['Tofu-Setup-0.16.0-win64.exe']
    assert 'kind' not in e


@pytest.mark.unit
def test_is_loopback_url():
    """The preseed-usability predicate: loopback/unspecified hosts are
    only reachable from the server's own machine."""
    for u in ('http://127.0.0.1:15000', 'http://127.0.1.1:15000/',
              'http://localhost', 'https://localhost:15000/p/',
              'http://[::1]:15000', 'http://0.0.0.0:15000'):
        assert store.is_loopback_url(u), u
    for u in ('https://tofu.example.com', 'http://192.168.1.5:15000',
              'http://10.0.0.2', '', 'not-a-url'):
        assert not store.is_loopback_url(u), u


# ═══════════════════════════════════════════════════════════════════
#  mirror
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_refresh_downloads_every_matching_asset_once(tmp_store, fake_net):
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    arts = store.artifacts()
    assert len(arts) == 4, arts
    assert len(fake_net['downloads']) == 4
    for name, e in arts.items():
        assert e['source'] == 'mirrored'
        assert e['size'] > 0 and e['sha256']
        assert e['version'] == '0.14.2'
        assert (tmp_store / name).is_file()
    m = store.load_manifest()
    assert m['tag'] == 'v0.14.2' and m['refreshed_at'] > 0
    assert m['last_error'] is None


@pytest.mark.unit
def test_refresh_skips_assets_that_have_not_changed(tmp_store, fake_net):
    """A 115 MB installer is NOT re-fetched every cycle: same name AND size
    means keep. This is what makes the 6 h cadence affordable."""
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    before = list(fake_net['downloads'])
    assert mirror.refresh_now() is True
    assert fake_net['downloads'] == before, (
        'an unchanged asset was re-downloaded')


@pytest.mark.unit
def test_a_failed_probe_keeps_everything_servable(tmp_store, fake_net):
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    names = set(store.artifacts())
    fake_net['probe'] = None   # probe now yields nothing (HTTP failure)
    assert mirror.refresh_now() is False
    assert set(store.artifacts()) == names, 'a failed probe must not prune'
    for n in names:
        assert store.resolve_file(n) is not None
    assert store.load_manifest()['last_error'], (
        'the failure must be recorded, not swallowed')


@pytest.mark.unit
def test_a_new_release_replaces_the_old_files(tmp_store, fake_net):
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    old_names = set(store.artifacts())
    fake_net['probe'] = _release(tag='v0.15.0', ver='0.15.0')
    assert mirror.refresh_now() is True
    arts = store.artifacts()
    assert all('0.15.0' in n for n in arts), arts
    for n in old_names:
        assert not (tmp_store / n).exists(), (
            f'stale artifact {n} survived the release rollover')
    assert store.load_manifest()['tag'] == 'v0.15.0'


@pytest.mark.unit
def test_pruning_never_strands_a_platform_whose_replacement_failed(
        tmp_store, fake_net):
    """The v2 Windows download dies mid-refresh: the OLD windows installer
    must stay servable — stale beats absent. The other platforms still roll
    forward."""
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    fake_net['probe'] = _release(tag='v0.15.0', ver='0.15.0')
    fake_net['fail_for'] = {'Tofu-Setup-0.15.0-win64.exe'}
    assert mirror.refresh_now() is False, 'partial failure must report False'
    assert store.resolve_file('Tofu-Setup-0.14.2-win64.exe') is not None, (
        'the old Windows installer was pruned with no replacement — a '
        'Windows visitor now gets nothing at all')
    # The platforms whose downloads succeeded DID move forward.
    assert store.resolve_file('Tofu-0.15.0-linux-x86_64.tar.gz') is not None
    assert store.resolve_file('Tofu-0.14.2-linux-x86_64.tar.gz') is None
    # And no .part debris is left behind by the failed download.
    assert not list(tmp_store.glob('*.part'))


@pytest.mark.unit
def test_ensure_fresh_is_single_flight_and_nonblocking(tmp_store,
                                                       monkeypatch):
    """The status route calls this on EVERY poll: it must return immediately
    and must never pile up workers."""
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _slow_refresh():
        calls.append(1)
        entered.set()
        release.wait(10)

    monkeypatch.setattr(mirror, 'refresh_now', _slow_refresh)
    assert mirror.ensure_fresh(force=True) is True
    assert entered.wait(5), 'worker never started'
    t0 = time.time()
    assert mirror.ensure_fresh(force=True) is True  # still one flight
    assert time.time() - t0 < 5, 'ensure_fresh blocked behind the worker'
    release.set()
    mirror._worker.join(5)
    assert len(calls) == 1, f'second kick spawned a second worker: {calls}'


@pytest.mark.unit
def test_stale_is_true_when_empty_and_false_right_after_refresh(
        tmp_store, fake_net):
    assert mirror.stale() is True
    fake_net['probe'] = _release()
    assert mirror.refresh_now() is True
    assert mirror.stale() is False


@pytest.mark.unit
def test_the_real_downloader_streams_hashes_and_fsyncs(tmp_store,
                                                       monkeypatch):
    """Drive the REAL ``_download`` (not the fake) with a stubbed transport.

    Written after the first live refresh crashed on ``flush of closed file``
    AFTER 594 s of successful downloading: the fake downloader in this suite
    replaces the whole function, so nothing exercised its real write/flush/
    fsync sequence. The transport is the only thing stubbed here.
    """
    import contextlib
    import hashlib

    chunks = [b'a' * 700_000, b'b' * 300_000]

    class _Resp:
        status_code = 200

        def iter_content(self, chunk_size=1):
            assert chunk_size == 1024 * 1024
            return iter(chunks)

    @contextlib.contextmanager
    def _stream(method, url, **kw):
        assert method == 'GET'
        yield _Resp()

    monkeypatch.setattr('lib.http_client.http_stream', _stream)
    dest = str(tmp_store / 'file.bin.part')
    size, sha = mirror._download('https://example.test/x', dest)
    body = b''.join(chunks)
    assert size == len(body)
    assert sha == hashlib.sha256(body).hexdigest()
    assert (tmp_store / 'file.bin.part').read_bytes() == body


@pytest.mark.unit
def test_the_real_downloader_rejects_non_200_and_empty(tmp_store,
                                                       monkeypatch):
    import contextlib

    class _Resp:
        status_code = 403

        def iter_content(self, chunk_size=1):
            return iter([])

    @contextlib.contextmanager
    def _stream(method, url, **kw):
        yield _Resp()

    monkeypatch.setattr('lib.http_client.http_stream', _stream)
    with pytest.raises(RuntimeError):
        mirror._download('https://example.test/x',
                         str(tmp_store / 'x.part'))


# ═══════════════════════════════════════════════════════════════════
#  builder
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_artifact_name_matches_the_ci_linux_leg():
    """The server-built artifact is named EXACTLY like the CI's Linux one —
    same shape as a mirrored file, so the platform globs and the UI cannot
    tell them apart (by design)."""
    from lib.desktop_dist import builder
    assert (builder.artifact_name('0.16.0')
            == 'Tofu-0.16.0-linux-x86_64.tar.gz')


def _fake_dist(workdir):
    dist = os.path.join(workdir, 'dist')
    os.makedirs(os.path.join(dist, 'Tofu'))
    with open(os.path.join(dist, 'Tofu', 'Tofu'), 'w') as f:
        f.write('fake-binary')
    return dist


@pytest.mark.unit
def test_a_successful_build_records_a_built_artifact(tmp_store, monkeypatch):
    """The heavy half is faked; the REAL tar + manifest recording runs."""
    from lib.desktop_dist import builder
    monkeypatch.setattr(builder, '_pipeline',
                        lambda workdir, version, sha, log_fh:
                        _fake_dist(workdir))
    builder._run('test')
    st = builder.state()
    assert st['state'] == 'ok', st
    name = builder.artifact_name(st['version'])
    e = store.artifacts().get(name)
    assert e, f'no artifact recorded for {name}'
    assert e['source'] == 'built'
    assert e['os'] == 'linux' and e['arch'] == 'x86_64'
    assert e['size'] > 0 and e['sha256']
    assert store.resolve_file(name) is not None
    # A built artifact of CURRENT code beats an older mirrored one.
    _seed(tmp_store, 'Tofu-0.14.2-linux-x86_64.tar.gz', os_key='linux',
          label='Linux archive', source='mirrored', version='0.14.2')
    rows = store.find_for_platform('linux', 'x86_64')
    assert rows[0]['filename'] == name, rows


@pytest.mark.unit
def test_a_failed_build_records_the_error_and_ships_nothing(
        tmp_store, monkeypatch):
    from lib.desktop_dist import builder

    def _boom(workdir, version, sha, log_fh):
        raise RuntimeError('pyinstaller exploded')

    monkeypatch.setattr(builder, '_pipeline', _boom)
    builder._run('test')
    st = builder.state()
    assert st['state'] == 'error', st
    assert 'pyinstaller exploded' in st['error']
    assert not [n for n, e in store.artifacts().items()
                if e.get('source') == 'built'], (
        'a failed build left an artifact behind')


@pytest.mark.unit
def test_build_is_single_flight(tmp_store, monkeypatch):
    from lib.desktop_dist import builder
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _slow(workdir, version, sha, log_fh):
        calls.append(1)
        entered.set()
        release.wait(10)
        return _fake_dist(workdir)

    monkeypatch.setattr(builder, '_pipeline', _slow)
    builder.start('test')
    assert entered.wait(5), 'builder never started'
    builder.start('test')   # must not spawn a second worker
    release.set()
    builder._worker.join(5)
    assert len(calls) == 1, f'second kick spawned a second builder: {calls}'


@pytest.mark.unit
def test_tofu_search_source_prefers_the_sibling_checkout(tmp_store,
                                                         monkeypatch):
    """install.sh's order: sibling checkout → vendor wheel → index name.

    Pinned because the floor (>=0.5.3) is on NO index (measured: public
    PyPI tops at 0.5.1, the internal mirror carries none), so getting this
    resolution wrong is a build that can never solve its requirements.
    """
    from lib.desktop_dist import builder
    root = tmp_store / 'root'
    root.mkdir()
    monkeypatch.setattr(builder, '_REPO_ROOT', str(root))
    # Sibling checkout present → it wins.
    sib = tmp_store / 'tofu-search'
    sib.mkdir()
    (sib / 'pyproject.toml').write_text('[project]')
    assert builder._tofu_search_source() == os.path.abspath(sib)
    # No sibling checkout → vendor wheel; none → bare name.
    sib.rename(tmp_store / 'not-tofu-search')
    vendor = root / 'vendor'
    vendor.mkdir()
    whl = vendor / 'tofu_search-0.5.3-py3-none-any.whl'
    whl.write_bytes(b'x')
    assert builder._tofu_search_source() == str(whl)
    whl.unlink()
    vendor.rmdir()
    assert builder._tofu_search_source() == 'tofu-search'


@pytest.mark.unit
def test_requirements_filter_drops_exactly_the_tofu_search_line(tmp_store):
    from lib.desktop_dist import builder
    req = tmp_store / 'requirements.txt'
    req.write_text('# comment\nflask>=3\ntofu-search>=0.5.3\n'
                   '  tofu-search-extra>=1\nrequests\n')
    out = builder._requirements_without_tofu_search(str(req),
                                                    str(tmp_store))
    text = open(out).read()
    assert 'tofu-search>=0.5.3' not in text
    # The exemption is ONE line, not a family: an unrelated lookalike stays.
    assert 'tofu-search-extra>=1' in text
    assert 'flask>=3' in text and 'requests' in text
    # A requirements file that lost its tofu-search pin must fail LOUD —
    # otherwise the exemption silently stops exempting and the build dies
    # on an unsatisfiable public solve.
    req2 = tmp_store / 'requirements2.txt'
    req2.write_text('flask>=3\n')
    with pytest.raises(RuntimeError):
        builder._requirements_without_tofu_search(str(req2),
                                                  str(tmp_store))


@pytest.mark.unit
def test_the_build_route_is_authenticated():
    """Static pin (same shape as the devices-endpoints guard): a build is
    minutes of CPU, so the kick must never sit behind an open route."""
    import inspect
    import routes.api_v1.desktop as dmod
    src = inspect.getsource(dmod)
    idx = src.index("route('/api/v1/desktop/build'")
    assert '@require_auth' in src[idx:idx + 200]


# ═══════════════════════════════════════════════════════════════════
#  routes
# ═══════════════════════════════════════════════════════════════════

_UA_WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'


def _bearer():
    from lib.api_keys import create_key
    _row, token = create_key(name='dist-test', scopes=['chat'],
                             user_id='u-dist')
    return {'Authorization': f'Bearer {token}'}


@pytest.mark.api
def test_the_status_endpoint_serves_store_backed_absolute_urls(
        tmp_store, flask_client, monkeypatch):
    """The whole feature in one assertion: a Windows visitor gets a
    SAME-ORIGIN absolute URL into the local store, marked hosted=server,
    with the size the UI displays."""
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')
    kicks = []
    monkeypatch.setattr(mirror, 'ensure_fresh',
                        lambda *a, **k: kicks.append((a, k)) or False)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    dl = r.get_json()['downloads']
    assert len(dl) == 1, dl
    e = dl[0]
    assert e['filename'] == 'Tofu-Setup-0.14.2-win64.exe'
    assert '/api/v1/desktop/download/Tofu-Setup-0.14.2-win64.exe' in e['url']
    assert e['url'].startswith(('http://', 'https://')), e['url']
    assert e['hosted'] == 'server'
    assert e['size'] == 1024
    assert kicks, 'the mirror must be kicked so an empty/stale store fills'


@pytest.mark.api
def test_the_status_endpoint_serves_downloads_with_the_network_hard_down(
        tmp_store, flask_client, monkeypatch):
    """THE root-cause guard (the panel stall): the request path performs no
    network, so a hard-down GitHub cannot delay or break it."""
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')

    def _boom(*a, **k):
        raise AssertionError('network touched in the request path')

    monkeypatch.setattr(platforms, 'fetch_latest_release', _boom)
    monkeypatch.setattr('lib.http_client.http_get', _boom)
    monkeypatch.setattr(mirror, 'ensure_fresh', lambda *a, **k: False)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    assert r.get_json()['downloads'][0]['hosted'] == 'server'


@pytest.mark.api
def test_the_status_payload_carries_both_component_downloads(
        tmp_store, flask_client, monkeypatch):
    """A3 serving: downloads (full) and agent_downloads (agent) are two
    independent rows, each entry carrying kind — the UI must be able to
    offer the agent installer as the PRIMARY action in the remote case."""
    _seed(tmp_store, 'Tofu-Setup-0.16.0-win64.exe', source='built',
          version='0.16.0')
    agent = {'os': 'windows', 'arch': 'x86_64',
             'label': 'Windows agent installer',
             'filename': 'TofuAgent-Setup-0.16.0-win64.exe',
             'size': 2048, 'sha256': 'ab' * 32, 'source': 'built',
             'version': '0.16.0', 'kind': 'agent',
             'fetched_at': time.time()}
    (tmp_store / agent['filename']).write_bytes(b'x' * 2048)
    from lib.desktop_dist import store as _st
    _st.record_artifact(agent)
    monkeypatch.setattr(mirror, 'ensure_fresh', lambda *a, **k: False)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    body = r.get_json()
    assert [d['filename'] for d in body['downloads']] == [
        'Tofu-Setup-0.16.0-win64.exe']
    assert body['downloads'][0]['kind'] == 'full'
    assert [d['filename'] for d in body['agent_downloads']] == [
        'TofuAgent-Setup-0.16.0-win64.exe']
    ad = body['agent_downloads'][0]
    assert ad['kind'] == 'agent' and ad['hosted'] == 'server'
    assert ad['size'] == 2048
    assert '/api/v1/desktop/download/TofuAgent-Setup-0.16.0-win64.exe' \
        in ad['url']
    # No preseed recorded ⇒ nothing advertised (the panel then renders
    # the mint-and-paste flow, never a false auto-connect promise).
    assert ad['preseed_url'] == ''


@pytest.mark.api
def test_the_status_payload_advertises_only_usable_preseeds(
        tmp_store, flask_client, monkeypatch):
    """The panel's zero-touch variant keys on preseed_url. A loopback
    preseed (the measured first agent artifact: http://127.0.0.1:15000)
    attaches a remote machine to a void AND suppresses the first-run
    dialog — the payload must advertise '' for it, never the URL."""
    monkeypatch.setattr(mirror, 'ensure_fresh', lambda *a, **k: False)

    def _seed_agent(name, version, pre_url):
        agent = {'os': 'windows', 'arch': 'x86_64',
                 'label': 'Windows agent installer', 'filename': name,
                 'size': 1024, 'sha256': 'ab' * 32, 'source': 'built',
                 'version': version, 'kind': 'agent',
                 'fetched_at': time.time(), 'preseed': {'url': pre_url}}
        (tmp_store / name).write_bytes(b'x' * 1024)
        store.record_artifact(agent)

    def _agent_row():
        r = flask_client.get('/api/v1/desktop/status',
                             headers={**_bearer(), 'User-Agent': _UA_WIN})
        assert r.status_code == 200
        return r.get_json()['agent_downloads'][0]

    _seed_agent('TofuAgent-Setup-0.16.0-win64.exe', '0.16.0',
                'http://127.0.0.1:15000')
    assert _agent_row()['preseed_url'] == '', (
        'a loopback preseed must never be advertised as auto-connect')
    _seed_agent('TofuAgent-Setup-0.16.1-win64.exe', '0.16.1',
                'https://tofu.example.com')
    assert _agent_row()['preseed_url'] == 'https://tofu.example.com'


@pytest.mark.api
def test_the_status_payload_projects_bridge_token_requirement(
        tmp_store, flask_client, monkeypatch):
    """bridge_token_required mirrors TOFU_BRIDGE_SECRET: unset ⇒ open
    bridge (a preseeded agent connects with zero input); set ⇒ the
    panel must keep the mint-and-paste step."""
    monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
    monkeypatch.setattr(mirror, 'ensure_fresh', lambda *a, **k: False)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.get_json()['bridge_token_required'] is False
    monkeypatch.setenv('TOFU_BRIDGE_SECRET', 's3cret')
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.get_json()['bridge_token_required'] is True


@pytest.mark.api
def test_the_status_payload_flags_outdated_agents(
        tmp_store, flask_client, monkeypatch):
    """Owner amendment ② surfacing: an agent whose frame version differs
    from the server's is flagged; same-version and legacy (versionless)
    agents are NOT (never cry wolf)."""
    from lib.desktop import bridge as db
    from lib.version import __version__ as sv
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db._agents.clear()
    db.register_agent('agent-old', {'name': 'old', 'version': '0.14.2'},
                      user_id='u-dist')
    db.register_agent('agent-cur', {'name': 'cur', 'version': sv.strip()},
                      user_id='u-dist')
    db.register_agent('agent-legacy', {'name': 'legacy'},
                      user_id='u-dist')
    monkeypatch.setattr(mirror, 'ensure_fresh', lambda *a, **k: False)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    agents = {a['agent_id']: a for a in r.get_json()['agents']}
    assert agents['agent-old']['outdated'] is True
    assert agents['agent-cur']['outdated'] is False
    assert agents['agent-legacy']['outdated'] is False, (
        'a versionless legacy agent is unknown, not outdated')


@pytest.mark.api
def test_the_download_route_serves_the_artifact(tmp_store, flask_client):
    content = b'INSTALLER-BYTES' * 100
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe', content=content)
    r = flask_client.get(
        '/api/v1/desktop/download/Tofu-Setup-0.14.2-win64.exe',
        headers=_bearer())
    assert r.status_code == 200
    assert r.data == content
    disp = r.headers.get('Content-Disposition', '')
    assert 'attachment' in disp and 'Tofu-Setup-0.14.2-win64.exe' in disp


@pytest.mark.api
def test_the_download_route_refuses_anything_but_a_manifest_key(
        tmp_store, flask_client):
    _seed(tmp_store, 'Tofu-Setup-0.14.2-win64.exe')
    for bad in ('Tofu-Setup-9.9.9-win64.exe', 'manifest.json',
                '..', '..%2F..%2Fserver.py', 'a%2Fb'):
        r = flask_client.get(f'/api/v1/desktop/download/{bad}',
                             headers=_bearer())
        assert r.status_code == 404, (bad, r.status_code)


@pytest.mark.api
def test_the_windows_autobuild_gate(tmp_store, flask_client, monkeypatch):
    """The Windows autobuild is env-gated, single-flight, and only kicks
    when NO built installer exists — the same shape as the Linux gate.
    Since A3 the gate is PER-KIND: the full and agent surfaces build
    independently (a built full never suppresses a missing built agent)."""
    from lib.desktop_dist import winbuilder
    kicks = []
    monkeypatch.setattr(winbuilder, 'start_installer',
                        lambda *a, **k: kicks.append((a, k)) or
                        {'state': 'running'})
    monkeypatch.setattr(winbuilder, 'is_running', lambda: False)

    # No built artifact + gate ON → BOTH kinds kicked.
    monkeypatch.setenv('TOFU_DESKTOP_DIST_AUTOBUILD', '1')
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    targets = {k[1].get('target', 'full') for k in kicks}
    assert targets == {'full', 'agent'}, (
        f'an empty store must kick both component builds: {kicks}')
    assert all(k[1].get('reason') == 'autobuild' for k in kicks)

    # Gate OFF → no kick even with an empty store.
    kicks.clear()
    monkeypatch.delenv('TOFU_DESKTOP_DIST_AUTOBUILD')
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    assert not kicks, 'the gate must be opt-in, never implicit'

    # A built FULL artifact suppresses ONLY the full kick — the agent
    # surface still has nothing built, so its kick fires (per-kind).
    monkeypatch.setenv('TOFU_DESKTOP_DIST_AUTOBUILD', '1')
    kicks.clear()
    _seed(tmp_store, 'Tofu-Setup-0.16.0-win64.exe', source='built',
          version='0.16.0')
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    assert [k[1].get('target') for k in kicks] == ['agent'], (
        f'a built full must not rebuild, but the agent surface is still '
        f'empty: {kicks}')

    # Both kinds built → complete silence.
    kicks.clear()
    agent = {'os': 'windows', 'arch': 'x86_64',
             'label': 'Windows agent installer',
             'filename': 'TofuAgent-Setup-0.16.0-win64.exe',
             'size': 1024, 'sha256': 'ab' * 32, 'source': 'built',
             'version': '0.16.0', 'kind': 'agent',
             'fetched_at': time.time()}
    (tmp_store / agent['filename']).write_bytes(b'x' * 1024)
    from lib.desktop_dist import store as _st
    _st.record_artifact(agent)
    r = flask_client.get('/api/v1/desktop/status',
                         headers={**_bearer(), 'User-Agent': _UA_WIN})
    assert r.status_code == 200
    assert not kicks, f'both kinds built must not rebuild: {kicks}'


@pytest.mark.unit
def test_the_route_keeps_the_extracted_names_importable():
    """The helpers moved to lib/desktop_dist/platforms; the route re-exports
    them so the guard lattice that imports from the route sees no drift."""
    import routes.api_v1.desktop as dmod
    for name in ('_match_platform_assets', '_assets_from_release_payload',
                 '_detect_os', '_detect_arch', '_platform_assets',
                 '_latest_release_assets', '_update_repo',
                 '_desktop_download_url'):
        assert hasattr(dmod, name), f'routes.api_v1.desktop.{name} is gone'


@pytest.mark.unit
def test_the_request_path_has_no_release_probe_left():
    """Static pin against re-introducing a synchronous probe: the status
    request path must not reference the GitHub release probe at all."""
    import inspect
    import routes.api_v1.desktop as dmod
    src = inspect.getsource(dmod._request_platform_downloads)
    assert 'fetch_latest_release' not in src
    assert '_latest_release_assets' not in src
    assert 'http_get' not in src
