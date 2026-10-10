"""Contract tests for the single-pass browser_fill_form implementation.

The whole form rides ONE execute_js bridge command; the page fills and
verifies every field, only then submits, and the filled form's HTML lands in
a bounded per-owner snapshot file. These tests pin the server-side mapping:
result envelope shape, failure semantics, snapshot bounds, and owner scoping.
Page-side matching logic is covered by the script-template invariants in
test_browser_tooling_fixes.py and the browser e2e tier.
"""

import os

import pytest

pytestmark = pytest.mark.unit


def _sender(script, calls=None):
    if calls is None:
        calls = []

    def fake(cmd, params, timeout=None):
        calls.append((cmd, params))
        entry = script.get(cmd, ({'ok': True, 'results': []}, None))
        if callable(entry):
            return entry(params)
        return entry

    return fake, calls


def _page(results, **extra):
    page = {'ok': all(r.get('ok') for r in results), 'results': results,
            'submitted': False, 'url': 'http://form', 'title': 'Form',
            'formHtml': ''}
    page.update(extra)
    return page


# ── envelope shape ──────────────────────────────────────────────────

def test_success_envelope_carries_verification_and_details():
    from lib.browser.advanced import fill_form_single_pass
    send, calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
        {'index': 1, 'kind': 'checkbox', 'ok': True, 'verified': True,
         'actual': 'checked'},
        {'index': 2, 'kind': 'select', 'ok': True, 'verified': True,
         'matched': 'CC BY 4.0'},
    ]), None)})
    out = fill_form_single_pass(7, [
        {'text': 'Title', 'value': 'My Paper', 'type': 'type'},
        {'text': 'Code Of Ethics', 'value': 'true', 'type': 'checkbox'},
        {'text': 'License', 'value': 'CC BY', 'type': 'select'},
    ], field_delay=0, send=send, owner_user_id='41')

    assert out['success'] is True
    assert out['fields_filled'] == 3 and out['fields_failed'] == 0
    assert out['steps_completed'] == 3
    assert out['submitted'] is False
    assert out['details']['verified'] == '3/3 fields verified'
    # The plan sent to the page carries labels, kinds and the submit gate.
    code = calls[0][1]['code']
    assert '"text": "Code Of Ethics"' in code
    assert '"kind": "checkbox"' in code
    assert calls[0][1]['tabId'] == 7
    # Plan echoes label/selector per field so failures stay addressable.
    assert out['field_results'][0]['text'] == 'Title'
    assert out['field_results'][2]['matched'] == 'CC BY 4.0'


def test_submit_reported_only_when_page_gate_passed():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
    ], submitted=True), None)})
    out = fill_form_single_pass(1, [
        {'selector': '#q', 'value': 'x', 'type': 'type'},
    ], submit_text='Submit', field_delay=0, send=send)
    assert out['success'] is True and out['submitted'] is True


def test_submit_error_turns_success_into_failure():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
    ], submitted=False, submitError='submit button not found', ok=True), None)})
    out = fill_form_single_pass(1, [
        {'selector': '#q', 'value': 'x', 'type': 'type'},
    ], submit_selector='#nope', field_delay=0, send=send)
    assert out['success'] is False
    assert out['submitted'] is False
    assert 'submit button not found' in out['error']


# ── typed failures ──────────────────────────────────────────────────

def test_bridge_error_is_a_typed_failure():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (None, 'extension not connected')})
    out = fill_form_single_pass(1, [
        {'selector': '#q', 'value': 'x', 'type': 'type'},
    ], field_delay=0, send=send)
    assert out['success'] is False
    assert out['fields_filled'] == 0
    assert 'extension not connected' in out['error']


def test_page_script_error_is_a_typed_failure():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (
        {'__error': True, 'message': 'CSP blocked'}, None)})
    out = fill_form_single_pass(1, [
        {'selector': '#q', 'value': 'x', 'type': 'type'},
    ], field_delay=0, send=send)
    assert out['success'] is False
    assert 'CSP blocked' in out['error']


def test_empty_fields_fail_fast_without_a_bridge_command():
    from lib.browser.advanced import fill_form_single_pass
    send, calls = _sender({})
    out = fill_form_single_pass(1, [], field_delay=0, send=send)
    assert out['success'] is False
    assert calls == [], 'no fields must not cost a bridge round trip'


def test_deadline_exceeded_fails_even_when_page_ok_flag_says_done():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
        {'index': 1, 'kind': 'type', 'ok': False,
         'error': 'deadline exceeded before this field'},
    ], deadlineExceeded=True), None)})
    out = fill_form_single_pass(1, [
        {'selector': '#a', 'value': 'x', 'type': 'type'},
        {'selector': '#b', 'value': 'y', 'type': 'type'},
    ], field_delay=0, send=send)
    assert out['success'] is False
    assert out['fields_filled'] == 1


def test_unknown_field_type_surfaces_as_field_failure():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'teleport', 'ok': False,
         'error': "unknown field type 'teleport'"},
    ]), None)})
    out = fill_form_single_pass(1, [
        {'selector': '#a', 'value': 'x', 'type': 'teleport'},
    ], field_delay=0, send=send)
    assert out['success'] is False
    assert "unknown field type" in out['field_results'][0]['error']


# ── type='file' uploads ─────────────────────────────────────────────

def _write_pdf(tmp_path, name='paper.pdf', payload=b'%PDF-1.4 fake bytes'):
    target = tmp_path / name
    target.write_bytes(payload)
    return target


def test_file_field_full_pipeline_tag_upload_verify(tmp_path):
    """Label-addressed file field: tag → page_upload → fill-script verify."""
    from lib.browser.advanced import fill_form_single_pass
    _write_pdf(tmp_path)
    calls = []
    script = {
        'execute_js': [
            ({'ok': True, 'results': [{'index': 1, 'ok': True}]}, None),
            (_page([
                {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
                {'index': 1, 'kind': 'file', 'ok': True, 'verified': True,
                 'actual': 'paper.pdf'},
            ], submitted=True), None),
        ],
        'page_upload': ({'value': {'filename': 'paper.pdf'}}, None),
    }

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        entry = script[cmd]
        return entry.pop(0) if isinstance(entry, list) else entry

    out = fill_form_single_pass(5, [
        {'text': 'Title', 'value': 'My Paper', 'type': 'type'},
        {'text': 'PDF', 'value': 'paper.pdf', 'type': 'file'},
    ], submit_text='Submit', field_delay=0, send=seq_send,
        project_path=str(tmp_path))

    assert out['success'] is True and out['submitted'] is True
    cmds = [c for c, _ in calls]
    assert cmds == ['execute_js', 'page_upload', 'execute_js'], cmds
    upload = calls[1][1]
    assert upload['ref'] == 'tofu-upload-1'
    assert upload['filename'] == 'paper.pdf'
    assert upload['mimeType'] == 'application/pdf'
    import base64 as _b64
    assert _b64.b64decode(upload['data']) == b'%PDF-1.4 fake bytes'
    # The fill script verifies the attachment by expected filename.
    assert '"expectFile": "paper.pdf"' in calls[2][1]['code']


def test_file_field_with_selector_skips_the_tagging_pass(tmp_path):
    from lib.browser.advanced import fill_form_single_pass
    _write_pdf(tmp_path)
    script = {
        'page_upload': ({'value': {'filename': 'paper.pdf'}}, None),
    }
    calls = []

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        if cmd == 'execute_js':
            return (_page([
                {'index': 0, 'kind': 'file', 'ok': True, 'verified': True},
            ]), None)
        return script[cmd]

    out = fill_form_single_pass(5, [
        {'selector': 'input[aria-label=pdf]', 'value': 'paper.pdf',
         'type': 'file'},
    ], field_delay=0, send=seq_send, project_path=str(tmp_path))
    assert out['success'] is True
    cmds = [c for c, _ in calls]
    assert cmds == ['page_upload', 'execute_js'], (
        f'an explicit selector must skip the tagging pass, got {cmds}')
    assert calls[0][1]['selector'] == 'input[aria-label=pdf]'


def test_file_field_missing_file_fails_without_any_bridge_command(tmp_path):
    from lib.browser.advanced import fill_form_single_pass
    calls = []

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        return (_page([]), None)  # main script: nothing left to fill

    out = fill_form_single_pass(5, [
        {'text': 'PDF', 'value': 'nope/missing.pdf', 'type': 'file'},
    ], submit_text='Submit', field_delay=0, send=seq_send,
        project_path=str(tmp_path))
    assert out['success'] is False
    assert out['submitted'] is False
    assert 'file not found' in out['field_results'][0]['error']
    # No tagging/upload command; the only bridge call is the fill script
    # (which must NOT submit: the pre-flight failure strips the submit gate).
    assert [c for c, _ in calls] == ['execute_js']
    assert '"submitText": ""' in calls[0][1]['code']
    assert '"fields": []' in calls[0][1]['code']


def test_file_field_oversize_fails_with_explicit_limit(tmp_path):
    from lib.browser.advanced import fill_form_single_pass
    big = tmp_path / 'big.pdf'
    big.write_bytes(b'0' * (20 * 1024 * 1024 + 1))
    calls = []

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        return (_page([]), None)

    out = fill_form_single_pass(5, [
        {'text': 'PDF', 'value': 'big.pdf', 'type': 'file'},
    ], field_delay=0, send=seq_send, project_path=str(tmp_path))
    assert out['success'] is False
    err = out['field_results'][0]['error']
    assert '20 MB' in err and 'attach it manually' in err
    assert 'page_upload' not in [c for c, _ in calls], (
        'an oversize file must never hit the bridge')


def test_file_field_tag_failure_skips_upload_but_keeps_others(tmp_path):
    from lib.browser.advanced import fill_form_single_pass
    _write_pdf(tmp_path)
    calls = []
    script = {
        'execute_js': [
            # tagging pass: the PDF label resolves to nothing
            ({'ok': False, 'results': [
                {'index': 1, 'ok': False,
                 'error': 'no file input matches label "PDF"'}]}, None),
            (_page([
                {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
            ]), None),
        ],
    }

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        entry = script[cmd]
        return entry.pop(0) if isinstance(entry, list) else entry

    out = fill_form_single_pass(5, [
        {'text': 'Title', 'value': 'x', 'type': 'type'},
        {'text': 'PDF', 'value': 'paper.pdf', 'type': 'file'},
    ], submit_text='Submit', field_delay=0, send=seq_send,
        project_path=str(tmp_path))
    assert out['success'] is False
    assert out['submitted'] is False
    assert [c for c, _ in calls] == ['execute_js', 'execute_js']
    file_result = [r for r in out['field_results'] if r['type'] == 'file'][0]
    assert 'no file input matches' in file_result['error']
    text_result = [r for r in out['field_results'] if r['type'] == 'type'][0]
    assert text_result['ok'] is True, 'other fields still fill normally'


def test_upload_command_failure_marks_only_the_file_field(tmp_path):
    from lib.browser.advanced import fill_form_single_pass
    _write_pdf(tmp_path)
    calls = []
    script = {
        'execute_js': [
            ({'ok': True, 'results': [{'index': 1, 'ok': True}]}, None),
            (_page([
                {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
            ]), None),
        ],
        'page_upload': (None, 'extension too old: unknown command'),
    }

    def seq_send(cmd, params, timeout=None):
        calls.append((cmd, params))
        entry = script[cmd]
        return entry.pop(0) if isinstance(entry, list) else entry

    out = fill_form_single_pass(5, [
        {'text': 'Title', 'value': 'x', 'type': 'type'},
        {'text': 'PDF', 'value': 'paper.pdf', 'type': 'file'},
    ], field_delay=0, send=seq_send, project_path=str(tmp_path))
    assert out['success'] is False
    file_result = [r for r in out['field_results'] if r['type'] == 'file'][0]
    assert 'upload failed' in file_result['error']
    assert 'extension too old' in file_result['error']


# ── type='multi_search' (search-then-pick widgets) ──────────────────

def test_multi_search_items_passthrough_and_skip_semantics():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'multi_search', 'ok': True, 'verified': True,
         'items': [
             {'value': '~Junhao_Ruan1', 'ok': True, 'skipped': True,
              'stage': 'already-added'},
             {'value': '~Bei_Li1', 'ok': True, 'stage': 'added'},
         ]},
    ]), None)})
    out = fill_form_single_pass(1, [
        {'text': 'Authors',
         'value': '~Junhao_Ruan1, ~Bei_Li1', 'type': 'multi_search'},
    ], field_delay=0, send=send)
    assert out['success'] is True
    field = out['field_results'][0]
    assert field['type'] == 'multi_search'
    assert field['items'][0]['skipped'] is True
    assert field['items'][1]['stage'] == 'added'


def test_multi_search_stage_error_names_item_and_stage():
    from lib.browser.advanced import fill_form_single_pass
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'multi_search', 'ok': False, 'verified': False,
         'error': "~Ghost_1: no result matched '~Ghost_1' "
                  '(stage: result-match)',
         'items': [
             {'value': '~Bei_Li1', 'ok': True, 'stage': 'added'},
             {'value': '~Ghost_1', 'ok': False, 'stage': 'result-match',
              'error': "no result matched '~Ghost_1'"},
         ]},
    ], submitted=False), None)})
    out = fill_form_single_pass(1, [
        {'text': 'Authors',
         'value': '~Bei_Li1, ~Ghost_1', 'type': 'multi_search'},
    ], submit_text='Submit', field_delay=0, send=send)
    assert out['success'] is False
    assert out['submitted'] is False
    assert '~Ghost_1' in out['error']
    assert 'result-match' in out['error']
    field = out['field_results'][0]
    assert field['items'][1]['ok'] is False
    assert field['items'][1]['stage'] == 'result-match'


# ── snapshot evidence ───────────────────────────────────────────────

def test_snapshot_written_under_owner_dir_and_html_stays_out_of_result(
        monkeypatch, tmp_path):
    import lib.browser.advanced as advanced
    monkeypatch.setattr(advanced, '_SNAPSHOT_DIR', str(tmp_path))
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
    ], formHtml='<form><input value="secret-title"></form>'), None)})
    out = advanced.fill_form_single_pass(3, [
        {'selector': '#t', 'value': 'x', 'type': 'type'},
    ], field_delay=0, send=send, owner_user_id='41')

    path = out.get('snapshot_path')
    assert path and os.path.isfile(path)
    assert 'owner-41' in path
    with open(path, encoding='utf-8') as fh:
        assert 'secret-title' in fh.read()
    # The HTML itself is never mirrored into the model-facing result fields.
    assert 'secret-title' not in str(out.get('details'))
    assert 'secret-title' not in str(out.get('field_results'))


def test_snapshot_prunes_to_keep_bound(monkeypatch, tmp_path):
    import lib.browser.advanced as advanced
    monkeypatch.setattr(advanced, '_SNAPSHOT_DIR', str(tmp_path))
    keep = advanced._SNAPSHOT_KEEP
    for i in range(keep + 4):
        path = advanced._write_form_snapshot('7', 1, f'<form>{i}</form>')
        assert path and os.path.isfile(path)
        # mtime ordering needs a tick between writes on fast filesystems
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    remaining = [
        f for f in os.listdir(os.path.join(str(tmp_path), 'owner-7'))
        if f.endswith('.html')]
    assert len(remaining) == keep, (
        f'snapshot dir must stay bounded at {keep}, got {len(remaining)}')


def test_snapshot_failure_never_fails_the_fill(monkeypatch, tmp_path):
    import lib.browser.advanced as advanced
    blocker = tmp_path / 'blocker'
    blocker.write_text('a file, not a directory', encoding='utf-8')
    # makedirs under a regular file raises NotADirectoryError (an OSError) —
    # the helper must swallow it and return None, not fail the fill.
    monkeypatch.setattr(advanced, '_SNAPSHOT_DIR',
                        os.path.join(str(blocker), 'sub'))
    send, _calls = _sender({'execute_js': (_page([
        {'index': 0, 'kind': 'type', 'ok': True, 'verified': True},
    ], formHtml='<form></form>'), None)})
    out = advanced.fill_form_single_pass(1, [
        {'selector': '#t', 'value': 'x', 'type': 'type'},
    ], field_delay=0, send=send, owner_user_id='41')
    assert out['success'] is True
    assert 'snapshot_path' not in out
