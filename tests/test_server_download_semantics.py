"""Server/client download semantics and authenticated shell fallback."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('command', 'downloader', 'output'),
    [
        (
            "curl -fsSL -b 'sid=secret' -o citadel.zip "
            "https://files.test/download",
            'curl', 'citadel.zip',
        ),
        (
            "curl -fsS -H 'Cookie: sid=secret' -O "
            "https://files.test/download",
            'curl', '[remote filename]',
        ),
        (
            "wget --load-cookies cookies.txt -O citadel.zip "
            "https://files.test/download",
            'wget', 'citadel.zip',
        ),
    ],
)
def test_cookie_authenticated_file_commands_have_one_safe_intent(
        command, downloader, output):
    from lib.project_mod.download_intent import (
        parse_authenticated_download_command,
    )

    intent = parse_authenticated_download_command(command)
    assert intent is not None and intent.redirectable
    assert intent.downloader == downloader
    assert intent.url == 'https://files.test/download'
    assert intent.requested_output == output
    assert 'secret' not in repr(intent)


@pytest.mark.parametrize('command', [
    "curl -fsS -H 'Cookie: sid=secret' https://files.test/api",
    "curl -fsSL -o file.zip https://files.test/download",
    "curl -fsSL -b 'sid=secret' -X POST -o out https://files.test/api",
    "curl -fsSL -b 'sid=secret' -d a=b -o out https://files.test/api",
    "wget -O - --load-cookies cookies.txt https://files.test/api",
])
def test_cookie_redirect_does_not_rewrite_api_upload_or_cookie_free_commands(
        command):
    from lib.project_mod.download_intent import (
        parse_authenticated_download_command,
    )

    assert parse_authenticated_download_command(command) is None


@pytest.mark.parametrize('command', [
    "curl -fsSL -b 'sid=secret' https://files.test/download > citadel.zip",
    "curl -fsSL -b \"$COOKIE\" -o citadel.zip https://files.test/download",
    (
        "HTTPS_PROXY=https://proxy.test curl -b 'sid=secret' -o citadel.zip "
        "https://files.test/download"
    ),
    "curl -b 'sid=secret -o citadel.zip https://files.test/download",
    (
        "curl -fsSL -b 'sid=secret' -o citadel.zip "
        "https://files.test/a https://files.test/b"
    ),
])
def test_ambiguous_cookie_file_commands_are_blocked_before_shell(command):
    from lib.project_mod.download_intent import (
        parse_authenticated_download_command,
    )

    intent = parse_authenticated_download_command(command)
    assert intent is not None
    assert intent.redirectable is False
    assert intent.block_reason
    assert 'secret' not in repr(intent)


def test_cookie_tool_returns_metadata_but_never_replayable_values(monkeypatch):
    from lib.browser.handlers._capture import _handle_get_cookies
    import lib.browser.access as access

    class Runtime:
        owner_user_id = '41'

        @staticmethod
        def send(command, params, timeout):
            assert command == 'get_cookies'
            return [{
                'name': 'session',
                'value': 'top-secret-cookie-value',
                'domain': '.files.test',
                'path': '/',
                'secure': True,
                'httpOnly': True,
                'sameSite': 'lax',
            }], None

    monkeypatch.setattr(access, 'is_read_allowed', lambda *_args, **_kwargs: True)
    result = _handle_get_cookies({'domain': 'files.test'}, Runtime())

    assert 'top-secret-cookie-value' not in result
    assert 'session = <redacted>' in result
    assert 'browser_download_url_to_server' in result
    assert '1 cookies found' in result


def test_download_tool_is_the_declared_server_location_contract():
    from lib.tools.registry import all_specs
    from lib.tools.search import build_browser_download_url_to_server_tool

    schema = build_browser_download_url_to_server_tool()['function']
    assert schema['name'] == 'browser_download_url_to_server'
    assert {'url', 'tab_id', 'text', 'selector'} <= set(
        schema['parameters']['properties'])
    assert 'server_staging' in schema['description']
    assert 'browser_get_cookies' in schema['description']
    owners = [
        spec.key for spec in all_specs()
        if 'browser_download_url_to_server' in spec.provides
    ]
    assert owners == ['browser_download']
    owner = next(spec for spec in all_specs() if spec.key == owners[0])
    assert owner.category == 'browser'
    assert owner.discovery_policy == 'eager'
    assert owner.cacheable_tools == frozenset()


def test_download_tool_has_a_dedicated_human_display():
    from lib.tasks_pkg.tool_display import (
        _TOOL_DISPLAY_DISPATCH,
        _tool_display_generic,
        tool_round_label,
    )

    handler = _TOOL_DISPLAY_DISPATCH['browser_download_url_to_server']
    assert handler is not _tool_display_generic
    label = tool_round_label(
        'browser_download_url_to_server',
        {'url': 'https://files.test/releases/citadel.zip?token=secret'},
    )
    assert label == 'Download to server: files.test/releases/citadel.zip'


def test_browser_element_resolves_exact_untruncated_link_without_click_or_cookie(
        monkeypatch):
    import lib.browser.download_target as target

    calls = []

    class Runtime:
        route_key = ('41', 'browser-a')

        def __init__(self, owner_user_id, client_id):
            assert owner_user_id == '41'
            assert client_id == 'browser-a'

        @staticmethod
        def send(command, params=None, timeout=30):
            calls.append(command)
            if command == 'list_tabs':
                return [{
                    'id': 7, 'active': True,
                    'url': 'https://files.test/releases',
                }], None
            if command == 'get_interactive_elements':
                return {'elements': [{
                    'text': '下载最新版', 'selector': '#latest',
                    # Deliberately unusable/truncated discovery projection.
                    'href': 'https://files.test/download?token=truncated',
                }]}, None
            if command == 'execute_js':
                assert '#latest' in params['code']
                return {
                    'url': (
                        'https://files.test/download?token='
                        + 'signed-value-' * 40),
                }, None
            raise AssertionError(f'unexpected browser command: {command}')

    monkeypatch.setattr(target, 'BrowserToolRuntime', Runtime)
    monkeypatch.setattr(
        'lib.browser.queue.get_connected_clients',
        lambda **_kwargs: [{
            'client_id': 'browser-a', 'last_poll': 10,
            'capabilities': ['file_export'],
        }],
    )
    monkeypatch.setattr(target, 'browser_tool_access', lambda *_a, **_k: '')

    url, client_id = target.resolve_browser_download_element(
        owner_user_id='41', text='下载最新版')

    assert client_id == 'browser-a'
    assert url.endswith('signed-value-' * 40)
    assert calls == ['list_tabs', 'get_interactive_elements', 'execute_js']
    assert 'click_element' not in calls
    assert 'get_cookies' not in calls


def test_browser_element_read_denial_is_a_typed_download_failure(monkeypatch):
    import lib.browser.download_target as target

    class Runtime:
        route_key = ('41', 'browser-a')

        def __init__(self, owner_user_id, client_id):
            assert (owner_user_id, client_id) == self.route_key

        @staticmethod
        def send(command, params=None, timeout=30):
            assert command == 'list_tabs'
            return [{
                'id': 7,
                'active': True,
                'url': 'https://denied.test/releases',
            }], None

    monkeypatch.setattr(target, 'BrowserToolRuntime', Runtime)
    monkeypatch.setattr(
        'lib.browser.queue.get_connected_clients',
        lambda **_kwargs: [{
            'client_id': 'browser-a',
            'last_poll': 10,
            'capabilities': ['file_export'],
        }],
    )

    def deny_browser_read(*_args, **_kwargs):
        raise target.BrowserAccessDenied('denied.test')

    monkeypatch.setattr(target, 'browser_tool_access', deny_browser_read)

    with pytest.raises(target.BrowserDownloadTargetError) as caught:
        target.resolve_browser_download_element(
            owner_user_id='41', text='下载最新版')

    assert caught.value.code == 'browser_download_access_denied'
    assert caught.value.retryable is False


def test_download_handler_routes_page_element_and_transfer_through_same_device(
        monkeypatch):
    import lib.browser.download_target as target
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core
    import lib.tasks_pkg.handlers.search._handlers as handlers

    resolved_url = 'https://files.test/download?token=exact-signed-value'
    monkeypatch.setattr(
        target, 'resolve_browser_download_element',
        lambda **kwargs: (
            resolved_url,
            'browser-a' if kwargs['owner_user_id'] == '41' else 'wrong',
        ),
    )
    acquired = []
    monkeypatch.setattr(
        core, 'download_url_to_server',
        lambda url, **_kwargs: acquired.append(url) or {
            'location': 'server_staging',
            'saved_path': '/safe/exact.zip',
            'size_bytes': 9,
            'sha256': 'd' * 64,
            'content_type': 'application/zip',
            'transport': 'browser_authenticated',
            'error_code': None,
        },
    )
    bindings = []

    @contextmanager
    def fake_binding(*, user_id='', client_id='', required_capabilities=()):
        bindings.append((user_id, client_id, tuple(required_capabilities)))
        yield (user_id, client_id, 'Default')

    monkeypatch.setattr(search_bridge, 'bind_search_browser', fake_binding)
    monkeypatch.setattr(
        handlers, '_finalize_tool_round', lambda *_args, **_kwargs: None)

    _tc_id, content, is_read = handlers._handle_browser_download_url_to_server(
        {'_userId': '41'}, {}, 'browser_download_url_to_server', 'tc-link',
        {'text': '下载最新版', 'tab_id': 7}, 1, {},
        {'browserClientId': ''}, '', False,
    )

    assert is_read is True
    assert acquired == [resolved_url]
    assert bindings == [('41', 'browser-a', ('file_export',))]
    assert json.loads(content)['path'] == '/safe/exact.zip'


def test_unselected_download_prefers_freshest_file_export_device(monkeypatch):
    import lib.browser.queue as queue
    from lib.search_bridge import bind_search_browser

    monkeypatch.setattr(
        queue,
        'get_connected_clients',
        lambda *, owner_user_id: [
            {
                'client_id': 'old-but-fresher',
                'profile': 'Default',
                'last_poll': 20,
                'capabilities': ['downloads'],
            },
            {
                'client_id': 'file-export-ready',
                'profile': 'Work',
                'last_poll': 10,
                'capabilities': ['downloads', 'file_export'],
            },
        ] if owner_user_id == '41' else [],
    )

    with bind_search_browser(
            user_id='41', required_capabilities=('file_export',)) as binding:
        assert binding[:2] == ('41', 'file-export-ready')
    with bind_search_browser(
            user_id='41', client_id='old-but-fresher',
            required_capabilities=('file_export',)) as explicit:
        assert explicit[:2] == ('41', 'old-but-fresher'), (
            'an explicit user-selected device must fail upgrade, not silently switch')


def _patch_fetched_root(monkeypatch, tmp_path):
    import lib.config_dir as config_dir
    import lib.browser.file_transfer as transfer_mod

    resolver = lambda *parts: str(tmp_path.joinpath(*parts))
    monkeypatch.setattr(config_dir, 'fetched_path', resolver)
    monkeypatch.setattr(transfer_mod, 'fetched_path', resolver)
    monkeypatch.setattr(transfer_mod, 'audit_log', lambda *args, **kwargs: None)


def test_explicit_server_download_uses_direct_transport_when_available(
        monkeypatch, tmp_path):
    import lib.tasks_pkg.handlers.search._core as core

    _patch_fetched_root(monkeypatch, tmp_path)
    payload = b'PK\x03\x04direct zip bytes'
    monkeypatch.setattr(
        core, 'fetch_url_bytes',
        lambda _url: (payload, 'application/zip'),
    )

    result = core.download_url_to_server(
        'https://files.test/citadel.zip', owner_user_id='41')

    assert result['location'] == 'server_staging'
    assert result['transport'] == 'server_direct'
    assert result['size_bytes'] == len(payload)
    assert result['sha256'] == hashlib.sha256(payload).hexdigest()
    assert Path(result['saved_path']).read_bytes() == payload


def test_server_direct_and_browser_files_share_one_bounded_staging_budget(
        monkeypatch, tmp_path):
    import lib.browser.file_transfer as transfer_mod
    from lib.browser.file_transfer import (
        BrowserFileTransferError,
        BrowserFileTransferStore,
        SERVER_DOWNLOAD_FILENAME_PREFIX,
    )

    _patch_fetched_root(monkeypatch, tmp_path)
    monkeypatch.setattr(transfer_mod, '_live_staging_headroom', lambda _n: True)
    monkeypatch.setattr(transfer_mod, '_browser_staging_budget_bytes', lambda: 8)
    store = BrowserFileTransferStore(clock=lambda: 1000.0)
    first = store.stage_server_response(
        owner_user_id='41',
        source_url='https://files.test/first.bin',
        body=b'12345678',
        content_type='application/octet-stream',
    )

    assert Path(first['path']).name.startswith(SERVER_DOWNLOAD_FILENAME_PREFIX)
    with pytest.raises(BrowserFileTransferError) as full:
        store.stage_server_response(
            owner_user_id='42',
            source_url='https://files.test/second.bin',
            body=b'x',
            content_type='application/octet-stream',
        )
    assert full.value.code == 'server_download_staging_capacity'
    assert Path(first['path']).exists(), (
        'fresh staging must reject new work instead of invalidating its receipt')


def test_server_direct_staging_requires_explicit_positive_owner(
        monkeypatch, tmp_path):
    import lib.browser.file_transfer as transfer_mod
    from lib.browser.file_transfer import (
        BrowserFileTransferError,
        BrowserFileTransferStore,
    )

    _patch_fetched_root(monkeypatch, tmp_path)
    monkeypatch.setattr(transfer_mod, '_live_staging_headroom', lambda _n: True)
    store = BrowserFileTransferStore()
    with pytest.raises(BrowserFileTransferError) as invalid:
        store.stage_server_response(
            owner_user_id='',
            source_url='https://files.test/file.bin',
            body=b'x',
        )
    assert invalid.value.code == 'browser_file_transfer_invalid_owner'


def test_direct_login_html_automatically_switches_to_browser_export(
        monkeypatch, tmp_path):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    _patch_fetched_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        core, 'fetch_url_bytes',
        lambda _url: (b'<!doctype html><title>SSO</title>', 'text/html'),
    )
    payload = b'PK\x03\x04browser zip bytes'
    staged = tmp_path / 'browser-transfer-authenticated.zip'
    staged.write_bytes(payload)
    receipt = {
        'transferId': 'transfer-1',
        'location': 'server_staging',
        'path': str(staged),
        'contentType': 'application/zip',
        'isAttachment': True,
        'hasFilename': True,
        'sizeBytes': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(),
    }
    calls = []
    monkeypatch.setattr(
        search_bridge, 'require_bound_browser_file',
        lambda url, **kwargs: calls.append((url, kwargs)) or receipt,
    )

    result = core.download_url_to_server(
        'https://files.test/download?version=latest', owner_user_id='41')

    assert len(calls) == 1
    assert result['location'] == 'server_staging'
    assert result['transport'] == 'browser_authenticated'
    assert result['saved_path'] == str(staged)
    assert staged.read_bytes() == payload


def test_browser_login_page_is_deleted_and_reported_as_retryable(
        monkeypatch, tmp_path):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    _patch_fetched_root(monkeypatch, tmp_path)
    monkeypatch.setattr(core, 'fetch_url_bytes', lambda _url: None)
    payload = b'<!doctype html><title>Login</title>'
    staged = tmp_path / 'browser-transfer-login.html'
    staged.write_bytes(payload)
    monkeypatch.setattr(
        search_bridge, 'require_bound_browser_file',
        lambda *_args, **_kwargs: {
            'transferId': 'transfer-login',
            'location': 'server_staging',
            'path': str(staged),
            'contentType': 'text/html',
            'isAttachment': False,
            'hasFilename': False,
            'sizeBytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        },
    )

    result = core.download_url_to_server(
        'https://files.test/download', owner_user_id='41')

    assert result['error_code'] == 'download_response_not_file'
    assert result['retryable'] is True
    assert 'finish login' in result['next_action']
    assert not staged.exists()


def test_offline_browser_error_survives_as_actionable_download_failure(
        monkeypatch):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core
    from lib.browser.file_transfer import BrowserFileTransferError

    monkeypatch.setattr(core, 'fetch_url_bytes', lambda _url: None)

    def offline(*_args, **_kwargs):
        raise BrowserFileTransferError(
            'browser_file_transfer_offline',
            'No compatible browser is connected',
            status=503,
        )

    monkeypatch.setattr(search_bridge, 'require_bound_browser_file', offline)
    result = core.download_url_to_server(
        'https://files.test/download', owner_user_id='41')

    assert result['error_code'] == 'browser_file_transfer_offline'
    assert result['retryable'] is True
    assert '5.4' in result['next_action']


def test_direct_failure_is_safely_observable_before_browser_fallback(
        monkeypatch, caplog):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    def fail_direct(_url):
        raise RuntimeError('direct transport unavailable')

    def fail_browser(*_args, **_kwargs):
        raise RuntimeError('browser transport unavailable')

    monkeypatch.setattr(core, 'fetch_url_bytes', fail_direct)
    monkeypatch.setattr(
        search_bridge, 'require_bound_browser_file', fail_browser)

    with caplog.at_level(
        logging.DEBUG,
        logger='lib.tasks_pkg.handlers.search._core',
    ):
        result = core.download_url_to_server(
            'https://files.test/download', owner_user_id='41')

    assert result['error_code'] == 'server_download_failed'
    assert 'server transport: direct transport unavailable' in result['error_msg']
    assert 'direct server transport failed' in caplog.text
    assert 'direct transport unavailable' in caplog.text


def test_missing_file_export_is_an_explicit_upgrade_failure(monkeypatch):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core
    from lib.browser.protocol import BrowserUpgradeRequired

    monkeypatch.setattr(core, 'fetch_url_bytes', lambda _url: None)

    def outdated(*_args, **_kwargs):
        raise BrowserUpgradeRequired(
            {'file_export'}, client_id='old-browser', protocol_version=2)

    monkeypatch.setattr(
        search_bridge, 'require_bound_browser_file', outdated)
    result = core.download_url_to_server(
        'https://files.test/download', owner_user_id='41')

    assert result['error_code'] == 'browser_extension_upgrade_required'
    assert result['retryable'] is False
    assert '5.4' in result['next_action']


def test_cookie_shell_redirect_invokes_canonical_acquisition_without_secret(
        monkeypatch):
    import lib.tasks_pkg.handlers.search._core as core
    from lib.tasks_pkg.handlers.authenticated_download import (
        maybe_redirect_authenticated_download,
    )

    monkeypatch.setattr(
        core,
        'download_url_to_server',
        lambda _url, **_kwargs: {
            'location': 'server_staging',
            'saved_path': '/safe/browser-transfer-citadel.zip',
            'size_bytes': 23,
            'sha256': 'a' * 64,
            'content_type': 'application/zip',
            'transport': 'browser_authenticated',
            'error_code': None,
        },
    )
    redirected = maybe_redirect_authenticated_download(
        task={'_userId': '41'},
        cfg={'browserClientId': 'browser-a'},
        command=(
            "curl -fsSL -H 'Cookie: session=top-secret' -o citadel.zip "
            "https://files.test/download"),
    )

    assert redirected is not None and redirected.ok
    assert 'top-secret' not in redirected.tool_content
    assert redirected.receipt['location'] == 'server_staging'
    assert redirected.receipt['destinationWritten'] is False
    assert redirected.receipt['requestedDestination'] == 'citadel.zip'


def test_standalone_run_command_never_spawns_for_cookie_file_download(
        monkeypatch):
    import lib.project_mod as project_mod
    import lib.tasks_pkg.handlers.code_exec as code_exec
    import lib.tasks_pkg.handlers.search._core as core

    monkeypatch.setattr(
        core,
        'download_url_to_server',
        lambda _url, **_kwargs: {
            'location': 'server_staging',
            'saved_path': '/safe/browser-transfer-citadel.zip',
            'size_bytes': 23,
            'sha256': 'b' * 64,
            'content_type': 'application/zip',
            'transport': 'browser_authenticated',
            'error_code': None,
        },
    )
    monkeypatch.setattr(
        project_mod,
        'execute_standalone_command',
        lambda *_args, **_kwargs: pytest.fail('subprocess path must not run'),
    )
    finalized = {}
    monkeypatch.setattr(
        code_exec,
        '_finalize_tool_round',
        lambda _task, _rn, _entry, results, **kwargs: finalized.update(
            results=results, status=kwargs.get('status')),
    )
    command = (
        "curl -fsSL -b 'session=top-secret' -o citadel.zip "
        "https://files.test/download")
    _tc_id, content, _is_read = code_exec._handle_code_exec(
        {'_userId': '41'}, {}, 'run_command', 'tc-1',
        {'command': command}, 1, {}, {'browserClientId': 'browser-a'},
        '', False,
    )

    assert finalized['status'] == 'done'
    assert finalized['results'][0]['authenticatedDownloadRedirected'] is True
    assert 'top-secret' not in content
    assert '[exit code: 0]' in content


def test_project_run_command_uses_the_same_pre_spawn_redirect(monkeypatch):
    import lib.tasks_pkg.handlers.project as project_handler
    import lib.tasks_pkg.handlers.search._core as core

    monkeypatch.setattr(
        core,
        'download_url_to_server',
        lambda _url, **_kwargs: {
            'location': 'server_staging',
            'saved_path': '/safe/server-download-citadel.zip',
            'size_bytes': 23,
            'sha256': 'd' * 64,
            'content_type': 'application/zip',
            'transport': 'browser_authenticated',
            'error_code': None,
        },
    )
    finalized = {}
    monkeypatch.setattr(
        project_handler,
        '_finalize_tool_round',
        lambda _task, _rn, _entry, results, **kwargs: finalized.update(
            results=results, status=kwargs.get('status')),
    )
    command = (
        "curl -fsSL -b 'session=top-secret' -o citadel.zip "
        "https://files.test/download")
    _tc_id, content, _is_read = project_handler._handle_project_tool(
        {'_userId': '41', 'id': 'task-1', 'convId': 'conv-1'},
        {}, 'run_command', 'tc-1', {'command': command}, 1, {},
        {'browserClientId': 'browser-a'}, '/project', True,
    )

    assert finalized['status'] == 'done'
    assert finalized['results'][0]['authenticatedDownloadRedirected'] is True
    assert finalized['results'][0]['badge'] == 'server staged'
    assert 'top-secret' not in content


def test_download_handler_preserves_typed_recovery_error(monkeypatch):
    import lib.tasks_pkg.handlers.search._core as core
    import lib.tasks_pkg.handlers.search._handlers as handlers
    from lib.tools.result_envelope import tool_result_error

    monkeypatch.setattr(
        core,
        'download_url_to_server',
        lambda _url, **_kwargs: {
            'location': None,
            'saved_path': None,
            'is_asset': False,
            'error_code': 'browser_extension_upgrade_required',
            'error_msg': 'file_export is missing',
            'retryable': False,
            'next_action': 'Reload extension 5.4 and retry.',
        },
    )
    finalized = {}
    monkeypatch.setattr(
        handlers,
        '_finalize_tool_round',
        lambda _task, _rn, _entry, results, **kwargs: finalized.update(
            results=results, status=kwargs.get('status')),
    )
    _tc_id, content, is_read = handlers._handle_browser_download_url_to_server(
        {'_userId': '41'}, {}, 'browser_download_url_to_server', 'tc-1',
        {'url': 'https://files.test/download'}, 1, {},
        {'browserClientId': 'browser-a'}, '', False,
    )

    error = tool_result_error(content)
    assert is_read is False
    assert error is not None
    assert error.code == 'browser_extension_upgrade_required'
    assert error.retryable is False
    assert error.next_action == 'Reload extension 5.4 and retry.'
    assert finalized['status'] == 'error'


def test_download_handler_returns_machine_readable_server_receipt(monkeypatch):
    import lib.tasks_pkg.handlers.search._core as core
    import lib.tasks_pkg.handlers.search._handlers as handlers

    monkeypatch.setattr(
        core,
        'download_url_to_server',
        lambda _url, **_kwargs: {
            'location': 'server_staging',
            'saved_path': '/safe/browser-transfer-citadel.zip',
            'size_bytes': 23,
            'sha256': 'c' * 64,
            'content_type': 'application/zip',
            'transport': 'browser_authenticated',
            'error_code': None,
        },
    )
    finalized = {}
    monkeypatch.setattr(
        handlers,
        '_finalize_tool_round',
        lambda _task, _rn, _entry, results, **kwargs: finalized.update(
            results=results, status=kwargs.get('status')),
    )
    _tc_id, content, is_read = handlers._handle_browser_download_url_to_server(
        {'_userId': '41'}, {}, 'browser_download_url_to_server', 'tc-1',
        {'url': 'https://files.test/download'}, 1, {},
        {'browserClientId': 'browser-a'}, '', False,
    )

    receipt = json.loads(content)
    assert is_read is True
    assert receipt['location'] == 'server_staging'
    assert receipt['path'] == '/safe/browser-transfer-citadel.zip'
    assert receipt['sha256'] == 'c' * 64
    assert finalized['results'][0]['badge'] == 'server staged'
