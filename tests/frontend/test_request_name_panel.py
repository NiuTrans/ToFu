#!/usr/bin/env python3
"""tests/frontend/test_request_name_panel.py — the request-name panel's rules.

WHY
===
The panel exists because a request name may need its own selector header
(aigc answers 200 for ``kunlunbedrock.gpt-6.1-sol-m17`` only with
``x-context-provider: kunlun``, and 400 ``配置不存在`` without it). Two failure
modes must not survive to a save:

  * a credential-named header, which the server's ``sanitise_extra_headers``
    rejects — today that refusal arrives after the whole form is filled;
  * a duplicate wire id inside one ProviderAccess, which the server rejects as
    a duplicate Deployment.

Both are validated in the browser against the SAME rules, so this suite pins
the rules rather than the widget.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/frontend/test_request_name_panel.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _node() -> str:
    return shutil.which('node') or 'node'


def _run(script: str) -> dict:
    module = native_module_path(
        'request-name-panel.js',
        ROOT + '/frontend/src/features/settings/provider-request-name-panel.ts',
    )
    # ``native_module_path`` bundles the owner as a classic script whose named
    # exports land on ``globalThis`` (the production graph stays ESM), so the
    # harness must read them there — ``require()`` returns an empty namespace.
    program = (
        "require(%s);"
        "const panel = globalThis;"
        "const out = (() => { %s })();"
        "process.stdout.write(JSON.stringify(out));"
        % (json.dumps(module), script)
    )
    result = subprocess.run([_node(), '-e', program], capture_output=True,
                            text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_selector_header_is_accepted():
    out = _run("""
        return panel.validateRequestNameDraft({
          wireModelId: 'kunlunbedrock.gpt-6.1-sol-m17',
          headers: [{ name: 'x-context-provider', value: 'kunlun' }],
        });
    """)
    assert out == ''


def test_credential_named_headers_are_refused_before_save():
    """The server refuses these; refusing here keeps the form fillable."""
    for name in ('Authorization', 'X-Api-Key', 'x-goog-api-key', 'cookie'):
        out = _run("""
            return panel.validateRequestNameDraft({
              wireModelId: 'm',
              headers: [{ name: %s, value: 'v' }],
            });
        """ % json.dumps(name))
        assert out, name


def test_reserved_transport_headers_are_refused():
    for name in ('Host', 'Content-Length', 'Transfer-Encoding', 'Proxy-Authorization'):
        out = _run("""
            return panel.validateRequestNameDraft({
              wireModelId: 'm',
              headers: [{ name: %s, value: 'v' }],
            });
        """ % json.dumps(name))
        assert out, name


def test_an_empty_name_is_refused_but_empty_header_rows_are_ignored():
    assert _run("return panel.validateRequestNameDraft("
                "{ wireModelId: '  ', headers: [] });")
    assert _run("return panel.validateRequestNameDraft("
                "{ wireModelId: 'm', headers: [{ name: '', value: '' }] });") == ''


def test_a_header_name_without_a_value_is_a_valid_route_marker():
    """Some selectors are presence-only; only a NAMELESS row is an error."""
    assert _run("return panel.validateRequestNameDraft("
                "{ wireModelId: 'm', headers: [{ name: 'x-thing', value: '' }] });") == ''


def test_duplicate_header_names_are_refused():
    out = _run("""
        return panel.validateRequestNameDraft({
          wireModelId: 'm',
          headers: [{ name: 'x-a', value: '1' }, { name: 'X-A', value: '2' }],
        });
    """)
    assert out


def test_the_panel_reports_its_headers_so_the_row_can_mark_them():
    """``requestNameDraftFrom`` round-trips a stored deployment."""
    out = _run("""
        return panel.requestNameDraftFrom({
          wire_model_id: 'alias',
          extra_headers: { 'x-context-provider': 'kunlun' },
        });
    """)
    assert out == {'wireModelId': 'alias',
                   'headers': [{'name': 'x-context-provider', 'value': 'kunlun'}]}


def test_the_panel_owns_its_styling_instead_of_borrowing_app_dialog_classes():
    """The application dialog CSS is demand-loaded with the dialog controller.

    Borrowing ``.app-dialog-overlay`` rendered this panel as an unstyled form
    appended at the end of <body> (reported as "the add-request-name form pops
    up at the bottom"). The panel must therefore carry its own overlay/card
    classes, which the always-loaded settings sheet styles.
    """
    source = (ROOT + '/frontend/src/features/settings/provider-request-name-panel.ts')
    text = open(source, encoding='utf-8').read()
    assert 'stg-rnp-overlay' in text and 'stg-rnp-card' in text
    for borrowed in ('app-dialog-overlay', 'app-dialog-card', 'app-dialog-input',
                     'app-dialog-title', 'app-dialog-message', 'app-dialog-actions'):
        assert "'" + borrowed not in text, borrowed
    css = open(ROOT + '/frontend/src/styles/settings/07-access-matrix.css',
               encoding='utf-8').read()
    assert '.stg-rnp-overlay' in css and 'position: fixed' in css


def test_a_deployment_without_headers_opens_with_one_empty_row():
    out = _run("return panel.requestNameDraftFrom({ wire_model_id: 'plain' });")
    assert out == {'wireModelId': 'plain', 'headers': []}


def test_a_header_on_the_connection_is_shown_as_this_names_own_row():
    """The reported "the header is missing" case.

    A face-pinned alias inherits ``x-context-provider`` from its Connection.
    The panel opened empty because it read only the deployment, so the route
    header looked lost while every request carried it. The Connection's
    headers must render as the request name's effective route.
    """
    out = _run("""
        return panel.requestNameHeaderRows(
          [], { 'x-context-provider': 'kunlun' });
    """)
    assert out == [{'name': 'x-context-provider', 'value': 'kunlun',
                    'inherited': True}]


def test_an_own_header_overrides_the_inherited_value_in_place():
    """One row, not two, and it keeps the Connection's spelling.

    Dispatch merges Connection and request-name headers as two dicts, so an
    override spelled ``X-Context-Provider`` against an inherited
    ``x-context-provider`` would send both. HTTP names are case-insensitive:
    the row keeps the Connection's spelling, so the merge collapses.
    """
    out = _run("""
        return panel.requestNameHeaderRows(
          [{ name: 'X-Context-Provider', value: 'east' }],
          { 'x-context-provider': 'kunlun' });
    """)
    assert out == [{'name': 'x-context-provider', 'value': 'east',
                    'inherited': False}]
    resolved = _run("""
        return panel.resolveRequestNameHeaders(
          [{ name: 'X-Context-Provider', value: 'east' }],
          { 'x-context-provider': 'kunlun' });
    """)
    assert resolved == {'deployment': {'x-context-provider': 'east'},
                        'removed': []}


def test_credential_named_connection_headers_are_not_editable_here():
    """They belong to the encrypted credential; the panel cannot own them."""
    out = _run("""
        return panel.requestNameHeaderRows([], {
          'x-context-provider': 'kunlun', 'X-Api-Key': 'k', 'Authorization': 'b',
        });
    """)
    assert [row['name'] for row in out] == ['x-context-provider']


def test_an_untouched_inherited_value_stays_on_the_connection():
    """No redundant per-name override for a value the Connection already sends."""
    out = _run("""
        return panel.resolveRequestNameHeaders(
          [{ name: 'x-context-provider', value: 'kunlun' }],
          { 'x-context-provider': 'kunlun' });
    """)
    assert out == {'deployment': {}, 'removed': []}


def test_a_changed_inherited_value_becomes_the_deployment_override():
    out = _run("""
        return panel.resolveRequestNameHeaders(
          [{ name: 'x-context-provider', value: 'east' }],
          { 'x-context-provider': 'kunlun' });
    """)
    assert out == {'deployment': {'x-context-provider': 'east'}, 'removed': []}


def test_a_deleted_inherited_header_is_reported_for_the_connection_owner():
    """A deployment can add or override but never subtract (dispatch merges
    the Connection UNDER it), so removing the row must edit the Connection."""
    out = _run("""
        return panel.resolveRequestNameHeaders(
          [], { 'x-context-provider': 'kunlun' });
    """)
    assert out == {'deployment': {}, 'removed': ['x-context-provider']}


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
