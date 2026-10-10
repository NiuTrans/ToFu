"""Header policy through real backend projections and the materialized dispatcher.

Historical wire fields are supplied explicitly; generated diff bodies must not be
mistaken for header text when checking redundant counters.
"""
from __future__ import annotations

import html
import json
import re

from pathlib import Path

import pytest

from lib.project_mod.tools import project_tool_display
from lib.tools.meta import build_project_tool_meta
from tests._jsdom import run_harness
from tests._runtime_sections import native_module_graph
from tests.frontend.test_frontend_tool_rounds_wire_parity import _run_harness

pytestmark = pytest.mark.unit
BATCH_TOOLS = ('edit_file', 'apply_diffs', 'insert_contents')


def _text(markup):
    return html.unescape(re.sub(r'<[^>]*>', '', markup)).strip()


def _spans(markup, class_name):
    return [_text(match) for match in re.findall(
        rf'<span\b[^>]*class="[^"]*\b{class_name}\b[^"]*"[^>]*>(.*?)</span>',
        markup, re.S)]


def _header(markup):
    # First summary is the visible outer header, not a per-edit summary.
    match = re.search(r'<summary\b[^>]*>(.*?)</summary>', markup, re.S)
    return match.group(1) if match else markup


def _round(tool, args, content):
    return {'status': 'done', 'toolName': tool, 'roundNum': 1,
            'query': project_tool_display(tool, args),
            'toolArgs': json.dumps(args), 'toolContent': content,
            'results': [build_project_tool_meta(tool, args, content)]}


def _batch(tool, count=2, failures=0, path='src/a.py', description='Update behavior'):
    edits = [{'path': path if i == 0 else 'src/b.py',
              'operation': 'replace' if i == 0 else 'insert_after',
              'anchor': f'old_{i} (7 edits)', 'search': f'old_{i} (7 edits)',
              'replace': f'new_{i} (7 edits)', 'content': f'new_{i} (7 edits)',
              'position': 'after', 'description': f'Operation {i}'}
             for i in range(count)]
    args = {'description': description, 'edits': edits}
    verb = 'Inserted' if tool == 'insert_contents' else 'Applied'
    content = f'{verb} {count - failures}/{count} edits ({failures} failed)\n'
    content += '\n'.join(
        f'[{i + 1}] ' + (f'FAIL {e["path"]}: anchor missing' if i >= count - failures
                         else f'OK {e["path"]}: 1 lines changed (3L → 3L)')
        for i, e in enumerate(edits))
    return _round(tool, args, content)


@pytest.fixture
def render(tmp_path):
    def run(round_value):
        path = tmp_path / 'rounds.json'
        path.write_text(json.dumps([round_value]), encoding='utf-8')
        result, = _run_harness(path)
        assert result['err'] is None, result['err']
        assert result['html']
        return result['html']
    return run


@pytest.mark.parametrize('tool', BATCH_TOOLS)
@pytest.mark.parametrize('count,tokens', [(1, 321), (2, None)])
def test_fresh_success_has_clean_header_and_intact_diff(render, tool, count, tokens):
    value = _batch(tool, count)
    if tokens is not None:
        value['toolTokens'] = tokens
    assert value['results'][0]['fetchedChars'] > 0
    markup = render(value)
    header = _header(markup)
    assert _spans(header, 'ptool-text') == [value['query']]
    assert not _spans(header, 'ptool-badge')
    if count > 1 or tool == 'edit_file':
        assert 'new_0 (7 edits)' in _text(markup)
    else:
        assert value['toolContent'] in _text(markup)
    assert 'new_0 (7 edits)' not in _text(header)


@pytest.mark.parametrize('tool', BATCH_TOOLS)
@pytest.mark.parametrize('failures', [0, 1, 2])
def test_historical_generated_counts_removed_without_losing_author_text(render, tool, failures):
    value = _batch(tool, failures=failures, path='src/report (2 edits).py',
                   description='Keep literal (2 edits) — human intent')
    expected = value['query']
    title, description = expected.split(' — ', 1)
    # Match the actual legacy producers; author literals above stay untouched.
    title_unit = 'insertions' if tool == 'insert_contents' else 'edits'
    badge_unit = 'inserted' if tool == 'insert_contents' else 'edits'
    value['query'] = f'{title} (2 {title_unit}) — {description}'
    value['results'][0]['badge'] = f'{2 - failures}/2 {badge_unit}'
    markup = render(value)
    header = _header(markup)
    assert _spans(header, 'ptool-text') == [expected]
    assert not _spans(header, 'ptool-badge')
    if failures:
        assert 'anchor missing' in _text(header)
        assert 'anchor missing' in _text(markup)
    else:
        assert 'new_0 (7 edits)' in _text(markup)
        assert 'new_0 (7 edits)' not in _text(header)


@pytest.mark.parametrize('tool', BATCH_TOOLS)
@pytest.mark.parametrize('status', ['searching', 'done'])
def test_large_history_uses_metadata_without_rewriting_stored_title(render, tool, status):
    value = _batch(tool, count=18, description='')
    args = json.loads(value['toolArgs'])
    for index, edit in enumerate(args['edits']):
        edit['path'] = f'src/file_{index % 8}.py'
    value = _round(tool, args, value['toolContent'])
    expected = value['query']
    unit = 'insertions' if tool == 'insert_contents' else 'edits'
    value['query'] = f'{expected} (18 {unit})'
    value['status'] = status
    value.pop('toolArgs')
    value['results'][0]['badge'] = '18/18 edits'
    header = _header(render(value))
    assert _spans(header, 'ptool-text') == [expected]
    assert '18/18 edits' not in _spans(header, 'ptool-badge')
    assert value['query'].endswith(f'(18 {unit})')


@pytest.mark.parametrize('tool', BATCH_TOOLS)
def test_ambiguous_history_title_is_preserved(render, tool):
    value = _batch(tool)
    value.pop('toolArgs')
    value['results'][0].pop('editSummaries')
    value['query'] = 'Edit notes (2 edits)'
    value['results'][0]['badge'] = '2 edits'
    header = _header(render(value))
    assert _spans(header, 'ptool-text') == ['Edit notes (2 edits)']
    assert not _spans(header, 'ptool-badge')


@pytest.mark.parametrize('tool', BATCH_TOOLS)
@pytest.mark.parametrize('kind,legacy,label,structured', [
    ('read_first', 'read first', 'tool.gateReadFirstBadge', False),
    ('stale', 'stale', 'tool.gateStaleBadge', False),
    ('read_first', '', 'tool.gateReadFirstBadge', True),
    ('stale', '', 'tool.gateStaleBadge', True),
    ('outside_workspace', '', 'tool.gateOutsideWorkspaceBadge', True),
])
def test_refusal_badge_wins_over_count_and_token_policy(render, tool, kind, legacy, label, structured):
    value = _batch(tool, failures=2)
    meta = value['results'][0]
    meta['badge'] = '0/2 edits' if structured else legacy
    if structured:
        meta['refusal'] = {'kind': kind, 'paths': ['src/a.py']}
    value['toolTokens'] = 321
    header = _header(render(value))
    assert label in _spans(header, 'ptool-badge')
    assert 'ptool-badge-gate' in header
    assert '321 tok' not in _text(header)


@pytest.mark.parametrize('tool', BATCH_TOOLS)
def test_backend_outside_workspace_stamp_is_visible(render, tool):
    value = _batch(tool, failures=2)
    content = value['toolContent'].replace(
        'anchor missing', 'outside every registered workspace root')
    value = _round(tool, json.loads(value['toolArgs']), content)
    assert value['results'][0]['refusal']['kind'] == 'outside_workspace'
    header = _header(render(value))
    assert 'tool.gateOutsideWorkspaceBadge' in _spans(header, 'ptool-badge')
    assert 'ptool-badge-gate' in header


@pytest.mark.parametrize('tool', BATCH_TOOLS)
@pytest.mark.parametrize('content', ['', 'Refused <unsafe>\nKeep the full reason'])
def test_terminal_error_keeps_refusal_without_count_or_success_badge(render, tool, content):
    value = {'status': 'error', 'toolName': tool, 'query': 'Edit src/a.py',
             'roundNum': 1, 'toolContent': content, 'toolTokens': 321,
             'results': [{'writeOk': False, 'badge': '0/2 edits',
                          'refusal': {'kind': 'outside_workspace', 'paths': ['src/a.py']}}]}
    markup = render(value)
    assert _spans(markup, 'ptool-badge') == ['tool.gateOutsideWorkspaceBadge']
    # The compact outcome card replaced the raw ptool-error log dump for
    # settled-failed batch edits; the refusal keeps its typed gate notice.
    assert 'ptool-error' not in markup
    assert 'ptool-batch-done-block' in markup
    assert 'ptool-gate-note' in markup
    assert 'ptool-badge-ok' not in markup
    assert '<unsafe>' not in markup
    for line in content.splitlines():
        assert line in _text(markup)


@pytest.mark.parametrize('tool', BATCH_TOOLS)
def test_custom_batch_error_badge_and_detail_survive(render, tool):
    value = _batch(tool, failures=2)
    value['results'][0]['badge'] = 'transaction rejected'
    header = _header(render(value))
    assert _spans(header, 'ptool-badge') == ['transaction rejected']
    assert 'ptool-badge-err' in header
    assert 'anchor missing' in _text(header)


@pytest.mark.parametrize('tool', BATCH_TOOLS)
def test_failed_batch_without_edit_detail_keeps_failure_fallback(render, tool):
    value = _batch(tool, failures=2)
    meta = value['results'][0]
    meta.pop('editSummaries')
    meta.pop('badge', None)
    value.pop('toolArgs')
    value['toolContent'] = ''
    header = _header(render(value))
    assert _spans(header, 'ptool-badge') == ['failed']
    assert 'ptool-badge-err' in header


@pytest.mark.parametrize('tool,content', [
    ('write_file', 'File updated: src/a.py'),
    ('apply_diff', 'Applied diff to src/a.py'),
    ('read_files', 'File: src/a.py (lines 1-1 of 1)\nhello'),
])
@pytest.mark.parametrize('badge', [None, '2 edits', 'custom result'])
def test_nonbatch_explicit_badges_and_token_fallback_are_not_suppressed(render, tool, content, badge):
    value = _round(tool, {'path': 'src/a.py', 'content': 'hello',
                          'search': 'old', 'replace': 'new'}, content)
    meta = value['results'][0]
    meta.pop('badge', None)
    if badge is not None:
        meta['badge'] = badge
    value['toolTokens'] = 321
    header = _header(render(value))
    assert _spans(header, 'ptool-badge') == [badge or '321 tok']


@pytest.mark.parametrize('args', [None, '{broken', 'null', '[]', '{"edits":[null,3,"bad"]}'])
def test_sparse_malformed_history_is_safe_and_preserves_ambiguous_title(render, args):
    value = {'status': 'done', 'toolName': 'edit_file', 'roundNum': 1,
             'query': 'Edit notes (2 edits)', 'toolArgs': args, 'results': [{}]}
    header = _header(render(value))
    assert _spans(header, 'ptool-text') == [value['query']]


def test_header_owner_is_pure_escaped_bounded_and_preserves_fallbacks():
    root = Path(__file__).resolve().parents[2]
    presentation = root / 'frontend/src/conversation/presentation'
    graph = native_module_graph([
        ('.native/header-contract.js', presentation / 'tool-header-presentation.ts'),
        ('.native/header-refusal-contract.js', presentation / 'write-gate-refusal.ts'),
    ])
    run_harness(target_js=graph, label='tool header owner', body_js=r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3], targets: [process.argv[2]]});
const translate = (key, params) => params && params.paths ? `${key}: ${params.paths}` : key;
const gate = createWriteGateRefusalPresentation({translate, iconHtml: () => ''});
const header = createToolHeaderPresentation({translate, writeGateRefusal: gate});
const limits = TOOL_HEADER_PRESENTATION_LIMITS;
const frozen = Object.freeze({toolName:'edit_file', query:'Edit <a>.py (2 edits) — <intent>',
  toolArgs:Object.freeze({edits:Object.freeze([Object.freeze({path:'<a>.py'}), Object.freeze({path:'<a>.py'})])}),
  results:Object.freeze([Object.freeze({badge:'2/2 edits', writeOk:true})])});
const before = JSON.stringify(frozen);
check('frozen history is normalized without mutation',
  header.queryText(frozen) === 'Edit <a>.py — <intent>'
  && header.renderBadgeHtml(frozen, frozen.results[0]) === ''
  && JSON.stringify(frozen) === before);
function node(markup) { const n = document.createElement('div'); n.innerHTML = markup; return n; }
const hostile = '<img src=x onerror="pwn()"> & " <script>';
const escaped = node(header.renderBadgeHtml({toolName:'write_file'}, {badge:hostile}));
check('explicit badges contain text, never injected markup',
  escaped.textContent === hostile && !escaped.querySelector('img,script'));
const refusalHtml = node(header.renderBadgeHtml({toolName:'edit_file'}, {refusal:{kind:hostile, paths:[hostile]}}));
check('unknown refusal kind remains an escaped warning',
  refusalHtml.textContent === hostile && !refusalHtml.querySelector('img,script')
  && refusalHtml.querySelector('.ptool-badge-gate'));
check('chars-only and token precedence remain for single writes',
  node(header.renderBadgeHtml({toolName:'write_file'}, {fetchedChars:2500})).textContent === '3k chars'
  && node(header.renderBadgeHtml({toolName:'write_file',toolTokens:1234}, {fetchedChars:2500})).textContent === '1.2k tok');
check('timeout and successful memory keep their badge classes',
  header.renderBadgeHtml({toolName:'await_agents'}, {badge:'timeout',awaitTimedOut:true,memoryOk:true}).includes('ptool-badge-warn')
  && header.renderBadgeHtml({toolName:'create_memory'}, {badge:'saved',memoryOk:true}).includes('ptool-badge-ok'));
const largeBadge = node(header.renderBadgeHtml({toolName:'write_file'}, {badge:'x'.repeat(limits.badgeUnits + 10)}));
check('custom badge truncation is visible and bounded', largeBadge.textContent.length <= limits.badgeUnits + 1 && largeBadge.textContent.endsWith('…'));
const tooLong = {...frozen, toolArgs:' '.repeat(limits.argumentsUnits + 1)};
const tooMany = {...frozen, toolArgs:{edits:Array(limits.edits + 1).fill({path:'<a>.py'})}};
check('oversized historical evidence is left verbatim',
  header.queryText(tooLong) === frozen.query && header.queryText(tooMany) === frozen.query);
const paths = Array(1000).fill('x'.repeat(10000));
const refusal = gate.resolveRefusal({toolName:'edit_file'}, {refusal:{kind:'read_first', paths}});
check('refusal path normalization is bounded and truncation remains visible',
  refusal.paths.length < paths.length && refusal.paths[0].length < paths[0].length
  && gate.renderNoticeHtml(refusal).includes('…') && Object.isFrozen(refusal.paths));
const enormous = node(header.renderBadgeHtml({toolName:'edit_file'}, {refusal:{kind:'x'.repeat(10000), paths}}));
check('unknown refusal badges honor the header budget',
  enormous.textContent.length <= limits.badgeUnits + 1 && enormous.textContent.endsWith('…'));
check('prototype property names are not legacy refusals',
  gate.resolveRefusal({toolName:'write_file'}, {badge:'toString'}) === null);
report();
''')


def test_repo_map_explains_code_structure_with_map_icon(render):
    value = {'toolName': 'repo_map', 'status': 'done', 'query': 'repo_map frontend/src',
             'roundNum': 1, 'toolArgs': {'path': 'frontend/src'},
             'results': [{'text': 'src/example.ts\n  class Example', 'fetchedChars': 29}],
             'toolContent': 'src/example.ts\n  class Example'}
    markup = render(value)
    assert 'tool.repoMap.label' in _header(markup)
    assert 'tool.repoMap.description' in _header(markup)
    assert 'm3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Z' in _header(markup)
    assert 'src/example.ts' in markup


@pytest.mark.parametrize('status', ['searching', 'done', 'error'])
@pytest.mark.parametrize('serialized', [False, True])
def test_repo_map_header_preserves_supplied_scope_and_result_evidence(render, status, serialized):
    args = {'path': 'lib', 'focus': 'swarm', 'max_tokens': 4500}
    output = 'lib/swarm/example.py:\n  class Example'
    value = {'toolName': 'repo_map', 'status': status, 'query': 'repo_map lib',
             'roundNum': 1, 'toolArgs': json.dumps(args) if serialized else args,
             'results': [{'badge': '25/34 files', 'fetchedChars': len(output)}],
             'toolContent': output}
    before = json.dumps(value)
    markup = render(value)
    header = _header(markup)
    assert 'path: lib · focus: swarm · max_tokens: 4500' in _text(header)
    assert 'tool.repoMap.label' in header and 'tool.repoMap.description' in header
    if status == 'done':
        assert '25/34 files' in header
        assert output in _text(markup)
    if status == 'error':
        assert 'ptool-badge-err' in markup
        assert all(line in _text(markup) for line in output.splitlines())
    assert json.dumps(value) == before


@pytest.mark.parametrize('serialized', [False, True])
@pytest.mark.parametrize('results', [[], [{'interrupted': True}]])
def test_interrupted_repo_map_keeps_scope_without_claiming_completion(render, serialized, results):
    args = {'path': 'lib', 'focus': 'swarm', 'max_tokens': 4500}
    value = {'toolName': 'repo_map', 'status': 'aborted', 'query': 'repo_map lib',
             'roundNum': 1, 'toolArgs': json.dumps(args) if serialized else args,
             'results': results}
    markup = render(value)
    assert 'path: lib · focus: swarm · max_tokens: 4500' in _text(markup)
    assert 'ptool-badge-interrupted' in markup
    assert 'ptool-spinner' not in markup
    assert 'ptool-active' not in markup
