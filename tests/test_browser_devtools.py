"""DevTools Bridge contracts: model boundary, policy and extension behavior."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / 'browser_extension' / 'background.js'


@pytest.mark.parametrize('action', [
    'console_read', 'console_clear', 'context_list',
    'debug_start', 'debug_state', 'debug_stop',
    'breakpoint_remove', 'script_source',
])
def test_diagnostic_and_cleanup_actions_need_no_domain_write_grant(action):
    from lib.browser.access import browser_tool_requires_write

    assert not browser_tool_requires_write(
        'browser_devtools', {'action': action})


@pytest.mark.parametrize('action', [
    'evaluate', 'inspect', 'breakpoint_set', 'pause', 'resume',
    'step_over', 'step_into', 'step_out', 'frame_evaluate',
])
def test_executable_and_execution_control_actions_are_domain_writes(action):
    from lib.browser.access import browser_tool_requires_write

    assert browser_tool_requires_write(
        'browser_devtools', {'action': action})


def test_handler_bounds_arguments_and_redacts_results(monkeypatch):
    import lib.browser.handlers._devtools as owner
    import lib.browser.protocol as protocol

    required = []
    sent = []
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda client_id, capabilities: required.extend(capabilities) or {})
    monkeypatch.setattr(owner, '_url_is_visible', lambda _owner, _url: True)

    class Runtime:
        client_id = 'client-a'
        owner_user_id = '41'

        @staticmethod
        def send(command, params, timeout):
            sent.append((command, params, timeout))
            return ({
                'url': 'https://example.test/app?token=do-not-leak',
                'action': params['action'],
                'value': {'access_token': 'secret-value', 'answer': 42},
            }, None)

    output = owner._handle_devtools({
        'action': 'inspect', 'expression': 'x' * 60_000,
        'maxDepth': 99, 'observeMs': -1, 'sessionTtlMs': 999_999,
        'sessionId': 's' * 500,
    }, Runtime())

    assert sent[0][0] == 'devtools'
    assert sent[0][1]['maxDepth'] == 6
    assert sent[0][1]['observeMs'] == 50
    assert sent[0][1]['sessionTtlMs'] == 120_000
    assert len(sent[0][1]['expression']) == 50_000
    assert len(sent[0][1]['sessionId']) == 256
    assert sent[0][2] == 35
    assert protocol.BrowserCapability.DEVTOOLS_CONSOLE in required
    assert 'secret-value' not in output
    assert 'do-not-leak' not in output
    assert '[redacted]' in output


def test_debugger_action_reports_upgrade_instead_of_dispatch(monkeypatch):
    import lib.browser.handlers._devtools as owner
    import lib.browser.protocol as protocol

    def missing(_client_id, _capabilities):
        raise protocol.BrowserUpgradeRequired(
            ['js_debugger'], client_id='old', protocol_version=2)

    monkeypatch.setattr(protocol, 'require_capabilities', missing)

    class Runtime:
        client_id = 'old'
        owner_user_id = '41'

        @staticmethod
        def send(*_args, **_kwargs):
            raise AssertionError('an unsupported extension must not be called')

    output = owner._handle_devtools({'action': 'debug_start'}, Runtime())
    assert 'upgrade required' in output
    assert 'js_debugger' in output


def test_result_sanitizer_filters_denied_sources_and_gets_secrets(monkeypatch):
    from lib.browser import access
    from lib.browser.handlers._devtools import sanitize_devtools_result

    monkeypatch.setattr(
        access, 'is_read_allowed',
        lambda _owner, url: 'denied.example' not in str(url))
    sanitized = sanitize_devtools_result({
        'entries': [
            {'url': 'https://allowed.example/app.js', 'text': 'ok'},
            {'url': 'https://denied.example/private.js', 'text': 'hidden'},
        ],
        'scripts': [
            {'scriptId': '1', 'url': 'https://allowed.example/app.js'},
            {'scriptId': '2', 'url': 'https://denied.example/private.js'},
        ],
        'value': {'password': 'very-secret', 'normal': 'visible'},
    }, owner_user_id='41')

    assert [row['text'] for row in sanitized['entries']] == ['ok']
    assert [row['scriptId'] for row in sanitized['scripts']] == ['1']
    assert sanitized['value']['password'] == '[redacted]'
    assert sanitized['value']['normal'] == 'visible'


def test_dispatch_translates_related_target_session_id():
    from lib.browser.dispatch import normalize_browser_args

    assert normalize_browser_args({
        'action': 'script_source', 'script_id': '17', 'session_id': 'child-2',
    }) == {
        'action': 'script_source', 'scriptId': '17', 'sessionId': 'child-2',
    }


def test_breakpoint_reauthorizes_source_url_and_current_tab(monkeypatch):
    from lib.browser import access, protocol, queue

    monkeypatch.setattr(
        queue, 'get_connected_clients',
        lambda **_kwargs: [{'client_id': 'client-a', 'last_poll': 1}])
    monkeypatch.setattr(
        queue, 'client_owner_user_id', lambda _client_id: '41')
    monkeypatch.setattr(protocol, 'client_protocol', lambda _client_id: {
        'client_id': 'client-a', 'profile': 'Default',
        'protocol_version': 2, 'capabilities': [],
    })
    monkeypatch.setattr(
        access, 'browser_tool_domain', lambda *_args, **_kwargs: 'app.example')
    checks = []

    def record(owner, target, *, access, client_id, profile):
        checks.append((owner, target, access, client_id, profile))
        return target

    monkeypatch.setattr(access, 'require_access', record)
    access.browser_tool_access(
        'browser_devtools', {
            'action': 'breakpoint_set',
            'source_url': 'https://cdn.example/assets/app.js',
        }, owner_user_id='41', client_id='client-a')

    assert checks == [
        ('41', 'https://cdn.example/assets/app.js', 'read',
         'client-a', 'Default'),
        ('41', 'app.example', 'write', 'client-a', 'Default'),
    ]


def test_extension_reuses_cdp_and_inspects_without_running_getters():
    """Exercise the real command router in a mocked MV3 service worker."""
    probe = textwrap.dedent(r"""
        const fs = require('node:fs');
        const vm = require('node:vm');
        const {webcrypto} = require('node:crypto');
        const listeners = () => {
          const handlers = [];
          return {
            addListener(fn) { handlers.push(fn); },
            removeListener(fn) {
              const index = handlers.indexOf(fn);
              if (index >= 0) handlers.splice(index, 1);
            },
            emit(...args) { for (const handler of [...handlers]) handler(...args); },
          };
        };
        const counters = {attach: 0, detach: 0, commands: []};
        let currentUrl = 'https://example.test/app';
        const debuggerEvents = listeners();
        const chrome = {
          debugger: {
            onEvent: debuggerEvents, onDetach: listeners(),
            attach: async () => { counters.attach += 1; },
            detach: async () => { counters.detach += 1; },
            sendCommand: async (_target, method, params = {}) => {
              counters.commands.push(method);
              if (method === 'Runtime.evaluate') {
                return {result: {type: 'object', objectId: 'root', description: 'Object'}};
              }
              if (method === 'Runtime.getProperties') {
                return {result: [
                  {name: 'answer', value: {type: 'number', value: 42}},
                  {name: 'self', value: {type: 'object', objectId: 'root'}},
                  {name: 'dangerous', get: {type: 'function', objectId: 'getter'}},
                ]};
              }
              return {};
            },
          },
          webNavigation: {
            onCommitted: listeners(), onHistoryStateUpdated: listeners(),
            onReferenceFragmentUpdated: listeners(),
          },
          tabs: {
            onRemoved: listeners(), onUpdated: listeners(),
            get: async (id) => ({id, url: currentUrl, title: 'App', status: 'complete'}),
          },
          runtime: {
            getManifest: () => ({version: 'test'}), getURL: (p) => p,
            onInstalled: listeners(), onStartup: listeners(), onMessage: listeners(),
          },
          alarms: {create() {}, onAlarm: listeners()},
          storage: {local: {get() {}, set() {}}},
          action: {setBadgeBackgroundColor() {}, setBadgeText() {}},
          webRequest: {onCompleted: listeners()},
        };
        const context = vm.createContext({
          chrome, console, crypto: webcrypto, navigator: {userAgent: 'Chrome/140'},
          URL, AbortController, TextDecoder, Uint8Array,
          setTimeout, clearTimeout, fetch: async () => { throw new Error('unexpected fetch'); },
          atob: (v) => Buffer.from(v, 'base64').toString('binary'),
        });
        vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context,
                        {filename: process.argv[1]});
        (async () => {
          const observed = await vm.runInContext(`Promise.all([
            executeCommand('devtools', {tabId: 7, action: 'console_read', observeMs: 50}),
            executeCommand('devtools', {tabId: 7, action: 'console_read', observeMs: 50})
          ])`, context);
          const afterObservers = {...counters};
          const inspected = await vm.runInContext(
            `executeCommand('devtools', {tabId: 7, action: 'inspect', expression: 'window.x'})`,
            context);
          const started = await vm.runInContext(
            `executeCommand('devtools', {tabId: 7, action: 'debug_start', sessionTtlMs: 10000})`,
            context);
          debuggerEvents.emit(
            {tabId: 7}, 'Target.attachedToTarget',
            {sessionId: 'cross', targetInfo: {
              targetId: 'x', type: 'iframe', url: 'https://other.test/frame'}});
          await new Promise((resolve) => setTimeout(resolve, 0));
          debuggerEvents.emit(
            {tabId: 7, sessionId: 'cross'}, 'Runtime.consoleAPICalled',
            {type: 'log', args: [{type: 'string', value: 'cross-secret'}]});
          debuggerEvents.emit(
            {tabId: 7}, 'Target.attachedToTarget',
            {sessionId: 'same', targetInfo: {
              targetId: 's', type: 'iframe', url: 'https://example.test/frame'}});
          await new Promise((resolve) => setTimeout(resolve, 0));
          debuggerEvents.emit(
            {tabId: 7, sessionId: 'same'}, 'Runtime.consoleAPICalled',
            {type: 'log', args: [{type: 'string', value: 'same-origin'}]});
          const debugState = await vm.runInContext(
            `executeCommand('devtools', {tabId: 7, action: 'debug_state'})`, context);
          await vm.runInContext(
            `executeCommand('devtools', {tabId: 7, action: 'evaluate', expression: 'window.x'})`,
            context);
          const stopped = await vm.runInContext(
            `executeCommand('devtools', {tabId: 7, action: 'debug_stop'})`, context);
          const beforeProtected = counters.attach;
          currentUrl = 'chrome://settings';
          let protectedError = '';
          try {
            await vm.runInContext(
              `executeCommand('devtools', {tabId: 7, action: 'console_read'})`, context);
          } catch (error) { protectedError = String(error.message || error); }
          process.stdout.write(JSON.stringify({
            observed: observed.length,
            observerAttach: afterObservers.attach,
            observerDetach: afterObservers.detach,
            inspected, started, debugState, stopped,
            finalAttach: counters.attach, finalDetach: counters.detach,
            protectedError, protectedAttached: counters.attach !== beforeProtected,
            getterRead: counters.commands.includes('Runtime.callFunctionOn'),
          }));
        })().catch((error) => { console.error(error); process.exit(1); });
    """)
    completed = subprocess.run(
        ['node', '-e', probe, str(BACKGROUND)], cwd=ROOT,
        check=True, text=True, capture_output=True, timeout=20)
    result = json.loads(completed.stdout)

    assert result['observed'] == 2
    assert result['observerAttach'] == 1
    assert result['observerDetach'] == 1
    assert result['inspected']['value']['properties']['answer'] == 42
    assert result['inspected']['value']['properties']['self'] == '[circular]'
    assert result['inspected']['value']['properties']['dangerous'] == '[Getter]'
    assert not result['getterRead']
    assert result['started']['active'] is True
    console_text = ' '.join(
        str(row.get('text', '')) for row in result['debugState']['consoleEntries'])
    assert 'same-origin' in console_text
    assert 'cross-secret' not in console_text
    assert result['stopped']['stopped'] is True
    assert result['finalAttach'] == 3
    assert result['finalDetach'] == 3
    assert 'protected page' in result['protectedError']
    assert result['protectedAttached'] is False
