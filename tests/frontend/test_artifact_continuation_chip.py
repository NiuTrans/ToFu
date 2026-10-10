"""Saved-output continuations: action and terms first, scope inside one disclosure.

Exercises the actual retained dispatcher plus its typed header/result owners.
Historical provenance, bounded arguments, result authority and failures survive
without leaking storage handles or cursor mechanics into the visible summary.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'Read 1 file: panel.ts'
FLAT_READ = f'Read compacted result of R54 · {SOURCE}'
REF = 'tr:mkdir-p-work-cp-mnt-dolp:7d26e5a5'


def _render(rounds, tmp_path):
    if shutil.which('node') is None:
        pytest.skip('node is required for the tool-round renderer')
    fixture = tmp_path / 'rounds.json'
    fixture.write_text(json.dumps(rounds), encoding='utf-8')
    process = subprocess.run([
        'node', str(ROOT / 'tests/_tool_rounds_wire_parity_harness.js'),
        runtime_section_path('ui/tool_rounds.js'), str(fixture),
    ], capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    rows = json.loads(process.stdout)
    assert not [row['err'] for row in rows if row['err']]
    return [row['html'] for row in rows]


def _summary(markup):
    match = re.search(r'<summary\b[^>]*>(.*?)</summary>', markup, re.S)
    return match.group(1) if match else markup


def _text(markup):
    return html.unescape(re.sub(r'<[^>]*>', '', markup))


def _round(name, args=None, **fields):
    value = {'status': 'done', 'toolName': name, 'roundNum': 15,
             'query': 'Search saved tool result' if name == 'search_tool_artifact'
             else 'Read saved tool result', 'results': [], **fields}
    if args is not None:
        value['toolArgs'] = args
    return value


def test_screenshot_search_has_one_title_labeled_terms_and_no_repeated_handle(tmp_path):
    patterns = ['TimeoutError', 'stage": "job_read', 'Traceback']
    value = _round('search_tool_artifact', {'searches': [
        {'artifact_ref': REF, 'query': query, 'cursor': '0'} for query in patterns
    ]}, toolContent='Original matching output', toolTokens=1400)
    before = json.dumps(value)
    (markup,) = _render([value], tmp_path)
    summary = _summary(markup)
    assert _text(summary).count('Search previous tool output') == 1
    assert 'Search terms:' in summary
    for pattern in patterns:
        assert _text(summary).count(pattern) == 1
    assert summary.count('class="ptool-artifact-query"') == 3
    assert 'mkdir-p' not in summary and '7d26e5a5' not in summary
    assert 'beginning' not in summary and 'byte' not in summary
    assert 'ptool-badge-err' not in summary
    assert '1.4k tok' in summary
    assert markup.count('<details ') == 1
    assert 'ptool-artifact-request' in markup and 'mkdir-p-work-cp-mnt-dolp #7d26e5a5' in markup
    assert 'Original matching output' in _text(markup)
    assert json.dumps(value) == before


def test_structured_and_legacy_source_is_meaningful_once_and_round_is_secondary(tmp_path):
    values = [
        _round('read_tool_artifact', query=FLAT_READ,
               _artifactOrigin={'kind': 'read', 'sourceRound': 54, 'source': SOURCE}),
        _round('read_tool_artifact', query=FLAT_READ),
        _round('search_tool_artifact', _artifactOrigin={
            'kind': 'search', 'sourceRound': 10, 'source': 'web_search: citadel',
            'queries': ['download url'],
        }),
    ]
    read, legacy, search = _render(values, tmp_path)
    for markup in [read, legacy]:
        summary = _summary(markup)
        assert 'Read a portion of previous tool output' in summary
        assert _text(summary).count(SOURCE) == 1
        assert 'compacted' not in summary and 'R54' not in summary
        assert 'Output from round 54' in markup
    assert 'Source: web_search: citadel' in _text(_summary(search))
    assert '>download url</code>' in _summary(search)


def test_every_supported_pattern_is_visible_not_hover_only_and_escaped(tmp_path):
    patterns = ['procedural generation', 'hybrid environments',
                '<img src=x onerror=alert(1)>', 'fourth pattern', 'fifth pattern']
    (markup,) = _render([_round('search_tool_artifact', _artifactOrigin={
        'kind': 'search', 'sourceRound': 5, 'source': 'Research results', 'queries': patterns,
    })], tmp_path)
    summary = _summary(markup)
    assert summary.count('class="ptool-artifact-query"') == len(patterns)
    for pattern in patterns:
        assert pattern in _text(summary)
    assert '<img src=x' not in summary
    assert 'ptool-artifact-query-more' not in summary
    assert not re.search(r'<code[^>]*class="ptool-artifact-query"[^>]*title=', summary)


def test_batch_sources_offsets_selectors_and_limits_are_disclosure_evidence(tmp_path):
    args = {'reads': [
        {'artifact_ref': 'tool-result:' + 'a' * 64, 'cursor': '0', 'limit': 8192},
        {'artifact_ref': 'tr:research-pages:' + 'b' * 16, 'cursor': '23000',
         'selector': '/items/1', 'limit': 14000},
    ]}
    (markup,) = _render([_round('read_tool_artifact', json.dumps(args),
        _artifactOrigin={'kind': 'read', 'sourceRound': 54, 'source': SOURCE})], tmp_path)
    summary = _summary(markup)
    assert 'Read portions of previous tool outputs' in summary
    for technical in ['aaaaaaaa', 'research-pages', '23,000', '/items/1', '14,000', SOURCE]:
        assert technical not in summary
    assert 'Output from round 54' not in markup  # first-item provenance is not batch provenance
    assert 'Starting byte: 23,000' in _text(markup)
    assert 'JSON selector: /items/1' in _text(markup)
    assert 'Read limit: 14,000 bytes' in _text(markup)
    assert 'Output reference: research-pages #bbbbbbbbbbbb' in _text(markup)
    assert markup.count('<li>') == 2 and markup.count('<details ') == 1


@pytest.mark.parametrize('name,expected', [
    ('read_tool_artifact', 'Continue reading'),
    ('search_tool_artifact', 'Continue the search'),
])
def test_opaque_cursor_is_private_and_not_a_byte_or_page_number(tmp_path, name, expected):
    args = {'artifact_ref': REF, 'cursor': 'trq1.signed.private', 'query': 'needle'}
    (markup,) = _render([_round(name, args)], tmp_path)
    assert 'trq1.signed.private' not in markup
    assert expected in _text(markup)
    assert 'Starting byte' not in markup and 'page 2' not in markup


@pytest.mark.parametrize('args', ['{broken', 'null', '[]', {}, {'reads': []}, {'reads': [None]}])
def test_malformed_history_retains_its_original_title_without_invented_scope(tmp_path, args):
    value = _round('read_tool_artifact', args, query='Read saved tool result: original intent',
                   toolContent='Original evidence')
    (markup,) = _render([value], tmp_path)
    assert value['query'] in _text(_summary(markup))
    assert 'ptool-artifact-header' not in markup and 'ptool-artifact-request' not in markup
    assert 'Original evidence' in _text(markup)


def test_tool_name_gate_and_unknown_sparse_title_are_preserved(tmp_path):
    other, custom, generic = _render([
        _round('read_files', query=FLAT_READ),
        _round('read_tool_artifact', query='Custom historical explanation'),
        _round('read_tool_artifact'),
    ], tmp_path)
    assert FLAT_READ in other and 'ptool-artifact-title' not in other
    assert 'Custom historical explanation' in custom and 'ptool-artifact-title' not in custom
    assert 'Read a portion of previous tool output' in generic


@pytest.mark.parametrize('status', ['error', 'failed'])
def test_failure_reason_verdict_and_request_remain_inspectable(tmp_path, status):
    reason = 'Saved output expired\nOriginal error detail <unsafe>'
    (markup,) = _render([_round('read_tool_artifact', {'artifact_ref': REF, 'cursor': '23000'},
        status=status, toolContent=reason, results=[{'badge': 'success-like'}])], tmp_path)
    summary = _summary(markup)
    assert 'ptool-badge-err' in summary and '>failed</span>' in summary
    assert 'Saved output expired' in _text(summary)
    assert 'ptool-badge-ok' not in summary and 'ptool-spinner' not in markup
    assert 'Starting byte: 23,000' in markup
    assert 'Original error detail <unsafe>' in _text(markup) and '<unsafe>' not in markup
    assert markup.count('<details ') == 1


def test_deferred_snapshot_keeps_lazy_authority_and_request_details(tmp_path):
    (markup,) = _render([_round('read_tool_artifact', {'artifact_ref': REF, 'limit': 14000},
        _snapshotToolResult={'preview': 'Stored preview', 'chars': 500000},
        toolContent='Do not use this competing body')], tmp_path)
    assert 'data-lazy-tool-result=""' in markup
    assert 'data-tool-result-authority="deferred"' in markup
    assert 'Read limit: 14,000 bytes' in markup and 'Stored preview' in markup
    assert 'Do not use this competing body' not in markup


def test_summary_only_first_paint_preserves_request_without_materializing_result(tmp_path):
    (markup,) = _render([_round('read_tool_artifact', {'artifact_ref': REF, 'limit': 14000},
        toolContent='Deferred original evidence', _deferToolResultBody=True)], tmp_path)
    assert 'Read a portion of previous tool output' in markup
    assert 'data-deferred-tool-body=""' in markup
    assert 'ptool-artifact-request' in markup and 'Deferred original evidence' not in markup
    assert 'Read limit: 14,000 bytes' in markup


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_typed_owner_is_localized_bounded_escaped_and_immutable(language):
    target = native_module_path('.native/artifact-readability-contract.js',
        ROOT / 'frontend/src/conversation/presentation/tool-artifact-presentation.ts')
    run_harness(target_js=target, label=f'artifact readability {language}', body_js=r'''
const fs=require('fs');
const {setup}=require(process.env.JSDOM_HARNESS);
const {check,report}=setup({root:process.argv[3],targets:[process.argv[2]]});
const locale=JSON.parse(fs.readFileSync(process.argv[3]+'/frontend/src/i18n/locales/LANGUAGE.json','utf8'));
const translate=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>String(params?.[k]??''));
const ref='tr:mkdir-p-work-cp-mnt-dolp:7d26e5a5';
const hostile='<img src=x onerror="pwn()"> & <script>';
function node(markup){const n=document.createElement('div');n.innerHTML=markup;return n;}
const frozen=Object.freeze({toolName:'search_tool_artifact',query:'Search saved tool result',
  toolArgs:Object.freeze({searches:Object.freeze([Object.freeze({artifact_ref:ref,query:hostile})])}),
  _artifactOrigin:Object.freeze({source:hostile,sourceRound:2})});
const before=JSON.stringify(frozen);
const header=node(renderToolArtifactHeaderHtml(frozen,translate));
const request=node(renderToolArtifactRequestHtml(frozen,translate));
check('title and field labels use the owning locale',
  header.querySelector('.ptool-artifact-title').textContent===locale['toolArtifact.search']
  && header.querySelector('.ptool-artifact-label').textContent===locale['toolArtifact.queries']
  && request.textContent.includes(locale['toolArtifact.requestDetails']));
check('source/query remain escaped selectable text',!header.querySelector('img,script')
  && header.querySelector('.ptool-artifact-query').textContent===hostile
  && !request.querySelector('img,script') && JSON.stringify(frozen)===before);
const limits=TOOL_ARTIFACT_PRESENTATION_LIMITS;
const oversized={toolName:'read_tool_artifact',query:'Read saved tool result',toolArgs:'x'.repeat(limits.argumentUnits+1)};
check('oversized historical arguments retain fallback',renderToolArtifactHeaderHtml(oversized,translate)===''
  && renderToolArtifactRequestHtml(oversized,translate)==='');
const inherited=Object.create({artifact_ref:ref,cursor:'0'});
check('inherited fields do not invent a valid request',renderToolArtifactHeaderHtml({toolName:'read_tool_artifact',toolArgs:inherited},translate)==='');
const huge=Array.from({length:1000},(_,i)=>({artifact_ref:ref,query:String(i)+' '+hostile.repeat(100)}));
const large={toolName:'search_tool_artifact',toolArgs:{searches:huge}};
const limited=node(renderToolArtifactHeaderHtml(large,translate));
check('item and query budgets are bounded with visible omissions',limited.querySelectorAll('.ptool-artifact-query').length===limits.items
  && [...limited.querySelectorAll('.ptool-artifact-query')].every(n=>n.textContent.length<=limits.queryUnits+1&&n.textContent.endsWith('…'))
  && limited.querySelector('.ptool-artifact-omitted').textContent.includes('984'));
const invalid=Object.create({toolName:'read_tool_artifact',toolArgs:{artifact_ref:ref}});
check('inherited tool identity never re-labels a row',renderToolArtifactHeaderHtml(invalid,translate)==='');
report();
'''.replace('LANGUAGE', language), min_pass=6)


def test_partial_failure_summary_uses_actual_error_not_raw_cursor_or_success_item(tmp_path):
    private_cursor = 'trq1.signed.private'
    reference = 'tool-result:' + 'd' * 64
    result = {'status': 'partial_failure', 'items': [
        {'status': 'ok', 'artifactRef': reference, 'nextCursor': private_cursor,
         'content': 'Successful first result'},
        {'status': 'error', 'artifactRef': reference, 'resumeCursor': private_cursor,
         'error': {'code': 'artifact_unavailable', 'message': 'Saved output expired; use a new authorized observation.'}},
    ]}
    (markup,) = _render([_round('read_tool_artifact', {'artifact_ref': REF},
        status='error', toolContent=json.dumps(result))], tmp_path)
    summary = _summary(markup)
    assert 'artifact_unavailable' in _text(summary) and 'Saved output expired' in _text(summary)
    for secret in [private_cursor, reference, 'nextCursor', 'resumeCursor', 'Successful first result']:
        assert secret not in summary
    assert private_cursor in _text(markup)  # full authoritative evidence is not rewritten


@pytest.mark.parametrize('origin', [None, {'source': SOURCE, 'sourceRound': 54}])
@pytest.mark.parametrize('args', ['{broken', 'x' * 32769])
def test_bad_arguments_do_not_bypass_fallback_via_legacy_title_or_origin(tmp_path, origin, args):
    fields = {'_artifactOrigin': origin} if origin else {}
    (markup,) = _render([_round('read_tool_artifact', args, query=FLAT_READ,
        toolContent='Original evidence', **fields)], tmp_path)
    assert FLAT_READ in _text(_summary(markup))
    assert 'ptool-artifact-header' not in markup and 'ptool-artifact-request' not in markup


def test_significant_whitespace_and_unicode_queries_never_silently_merge(tmp_path):
    queries = ['needle', ' needle ', '😀' * 100 + 'A', '😀' * 100 + 'B']
    (markup,) = _render([_round('search_tool_artifact', {'searches': [
        {'artifact_ref': REF, 'query': query} for query in queries
    ]})], tmp_path)
    shown = [html.unescape(query) for query in re.findall(
        r'<code class="ptool-artifact-query">(.*?)</code>', _summary(markup), re.S)]
    assert shown == queries


def test_missing_batch_arguments_do_not_claim_shared_origin(tmp_path):
    (markup,) = _render([_round('search_tool_artifact', _artifactOrigin={
        'source': SOURCE, 'sourceRound': 54, 'queries': ['first', 'second'],
    })], tmp_path)
    assert SOURCE not in _summary(markup) and 'Output from round 54' not in markup
    assert '>first</code>' in markup and '>second</code>' in markup


def test_search_limit_and_numeric_cursor_are_requested_not_returned_claims(tmp_path):
    (markup,) = _render([_round('search_tool_artifact', {
        'artifact_ref': REF, 'query': 'needle', 'cursor': '23', 'limit': 7,
    })], tmp_path)
    assert 'Match limit: 7' in markup
    assert 'Nonzero numeric search cursor (not a byte offset)' in markup
    assert 'Starting byte: 23' not in markup and 'Match limit' not in _summary(markup)
