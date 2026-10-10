"""Saved tool-output readability through Surface, real owners and shipped CSS.

Synthetic accepted rounds only: the frontend_visual fixture serves an ephemeral
asset origin. No stored conversation, real tool action or application server is
used. The same fixtures exercise collapsed copy, disclosed request evidence,
native keyboard interaction and an immutable replay with changed result text.
"""
from __future__ import annotations

import json
import re

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

_SEARCH_ALIAS = 'tr:search-evidence:d15ea5ed4a11babe'
_READ_ALIAS = 'tr:research-pages:bbbbbbbbcccccccc'
_DIGEST_REF = 'tool-result:' + 'a' * 64
_SEARCH_CURSOR = 'trq1.synthetic-search.signature-not-a-byte-offset'
_LONG_QUERY = (
    'Complete saved-output search terms remain visible: '
    'NativeRuntimeOwnershipBoundaryNativeRuntimeOwnershipBoundary'
    'NativeRuntimeOwnershipBoundary with wrapping, not hover-only summaries.'
)
_SEARCH_TERMS = ['TimeoutError', 'stage": "job_read', 'Traceback', _LONG_QUERY]
_FAILURE_TERMS = [
    'retryable false',
    'artifact unavailable',
    '<img src=x onerror="window.__artifactQueryExecuted=true">',
    'failure evidence remains visible',
    'fifth term must not become a hover-only chip',
]
_SOURCE = 'Read 1 file: tool_output_presenter.ts'
_REPLAY_SUFFIX = '\nReplay appended this synthetic result line.'


def _fixtures():
    """Reported terms plus a long wrapping term deliberately share one output."""
    searches = [
        {'artifact_ref': _SEARCH_ALIAS, 'query': query, 'cursor': cursor, 'limit': limit}
        for query, cursor, limit in zip(_SEARCH_TERMS, ['0', _SEARCH_CURSOR, '23', '0'], [7, 11, 13, 8])
    ]
    reads = [
        {'artifact_ref': _DIGEST_REF, 'cursor': '8192', 'limit': 6144,
         'selector': '/items/0/details/presentation'},
        {'artifact_ref': _READ_ALIAS, 'cursor': '24576', 'limit': 2048,
         'selector': '/items/1'},
    ]
    failures = [
        {'artifact_ref': _SEARCH_ALIAS, 'query': query, 'cursor': '0', 'limit': 8}
        for query in _FAILURE_TERMS
    ]
    return [
        {
            'id': 'three-term-search', 'toolName': 'search_tool_artifact',
            'query': 'Search 4 saved tool results: ' + '; '.join(
                f'{_SEARCH_ALIAS}: {query}' for query in _SEARCH_TERMS),
            'toolArgs': {'searches': searches}, 'terms': _SEARCH_TERMS,
            'titleKey': 'toolArtifact.search', 'status': 'done',
            'content': 'Search evidence remains original.\nThree requested terms, one saved result.',
            'requestNumbers': [], 'selectors': [],
        },
        {
            'id': 'multi-result-read', 'toolName': 'read_tool_artifact',
            'query': f'Read 2 saved tool results: {_DIGEST_REF}; {_READ_ALIAS}',
            # Historical toolArgs are also serialized JSON, not only objects.
            'toolArgs': json.dumps({'reads': reads}), 'terms': [],
            'titleKey': 'toolArtifact.readPlural', 'status': 'done',
            'content': 'First saved result: original text.\nSecond saved result: <b>literal, not markup</b>.',
            'requestNumbers': [8192, 6144, 24576, 2048],
            'selectors': [read['selector'] for read in reads],
        },
        {
            'id': 'provenance-read', 'toolName': 'read_tool_artifact',
            'query': f'Read compacted result of R54 · {_SOURCE}',
            'toolArgs': {'artifact_ref': _READ_ALIAS, 'cursor': '16384',
                         'limit': 4096, 'selector': '/records/0/content'},
            'origin': {'kind': 'read', 'sourceRound': 54, 'source': _SOURCE},
            'terms': [], 'titleKey': 'toolArtifact.read', 'status': 'done',
            'content': 'Saved file provenance is not a new observation.\nOriginal retained content stays inspectable.',
            'requestNumbers': [16384, 4096], 'selectors': ['/records/0/content'],
        },
        {
            'id': 'failed-search', 'toolName': 'search_tool_artifact',
            'query': f'Search 5 saved tool results: {_SEARCH_ALIAS}',
            'toolArgs': {'searches': failures}, 'terms': _FAILURE_TERMS,
            'titleKey': 'toolArtifact.search', 'status': 'error',
            'content': 'artifact_unavailable: Synthetic saved output expired.\nHistorical evidence was not recovered.',
            # A cosmetic success-like badge must not override the real error.
            'metadata': {'badge': '5 matches', 'status': 'error',
                         'error': {'code': 'artifact_unavailable', 'retryable': False}},
            'requestNumbers': [], 'selectors': [],
        },
    ]


def _translated(locale, key, **params):
    return re.sub(r'\{(\w+)\}', lambda match: str(params.get(match[1], '')), locale[key])


def _numeric_text(text):
    # UI may group requested byte counts; their exact numeric value must survive.
    return text.replace(',', '').replace('\u202f', '').replace('\u00a0', '')


def _assert_no_overflow(frame):
    assert frame.locator('body').evaluate('''element =>
      element.scrollWidth <= window.innerWidth + 1
        && document.documentElement.scrollWidth <= window.innerWidth + 1
    ''')
    assert frame.locator('.ptool-result-block').evaluate_all('''nodes => nodes.every(node => {
      const rect = node.getBoundingClientRect();
      return rect.left >= -1 && rect.right <= window.innerWidth + 1
        && node.scrollWidth <= node.clientWidth + 1;
    })''')
    assert frame.locator('.ptool-artifact-request').evaluate_all('''nodes => nodes.every(node => {
      if (!node.getClientRects().length) return true;
      const rect = node.getBoundingClientRect();
      return rect.left >= -1 && rect.right <= window.innerWidth + 1
        && node.scrollWidth <= node.clientWidth + 1;
    })''')


def _assert_terms(page, summary, terms, locale, width):
    queries = summary.locator('.ptool-artifact-queries')
    if not terms:
        assert queries.count() == 0
        return
    assert queries.count() == 1
    assert queries.is_visible()
    assert locale['toolArtifact.queries'] in queries.inner_text()
    # No extra terms may be available only through a tooltip or a +N chip.
    assert not re.search(r'\+\d+', queries.inner_text())
    assert queries.bounding_box()['width'] >= min(200, width * 0.55)
    for term in terms:
        shown = queries.get_by_text(term, exact=True)
        assert shown.count() == 1, f'Search term was omitted, clipped or duplicated: {term}'
        assert shown.is_visible()
        layout = shown.evaluate('''node => {
          const style = getComputedStyle(node);
          const range = document.createRange(); range.selectNodeContents(node);
          const rect = node.getBoundingClientRect();
          const lines = Array.from(range.getClientRects()).filter(line => line.width > 0);
          const selection = document.getSelection();
          selection.removeAllRanges(); selection.addRange(range);
          const selected = selection.toString(); selection.removeAllRanges();
          const first = lines[0], last = lines[lines.length - 1];
          return {whiteSpace: style.whiteSpace, textOverflow: style.textOverflow,
            userSelect: style.userSelect, selected, width: rect.width,
            start: {x: first.left + 1, y: first.top + first.height / 2},
            end: {x: last.right - 1, y: last.top + last.height / 2},
            lines: lines.length, fits: node.scrollWidth <= node.clientWidth + 1
              && node.scrollHeight <= node.clientHeight + 1
              && lines.every(line => line.left >= -1 && line.right <= window.innerWidth + 1)};
        }''')
        assert layout['whiteSpace'] != 'nowrap'
        assert layout['textOverflow'] != 'ellipsis'
        assert layout['userSelect'] in {'text', 'all', 'auto'}
        assert layout['selected'] == term
        assert layout['fits'], f'Search term overflows or is clipped: {term}'
        if term == _LONG_QUERY:
            assert layout['width'] >= 160, 'A wrapping chip must not squeeze text to one-character columns'
            if width == 390:
                assert layout['lines'] > 1
            # Programmatic selection alone ignores user-select:none. A real
            # pointer drag proves the wrapping term can actually be selected.
            page.mouse.move(layout['start']['x'], layout['start']['y'])
            page.mouse.down()
            page.mouse.move(layout['end']['x'], layout['end']['y'], steps=12)
            page.mouse.up()
            assert shown.evaluate('()=>document.getSelection().toString()') == term
            assert shown.evaluate('(node)=>!node.closest("details").open')
            shown.evaluate('()=>document.getSelection().removeAllRanges()')


@pytest.mark.parametrize('width,language,theme', [
    (1100, 'en', 'light'), (1100, 'zh', 'tofu'),
    (390, 'en', 'dark'), (390, 'zh', 'tofu'),
])
def test_saved_tool_output_summary_and_requested_evidence_remain_readable(
    frontend_visual_page, tmp_path, width, language, theme,  # noqa: F811
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1400})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate('''() => {
      const iframe = document.createElement('iframe'); iframe.id = 'artifact-reading-fixture';
      iframe.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:999999;border:0';
      document.body.appendChild(iframe);
    }''')
    frame = page.frames[-1]
    frame.set_content(f'<html lang="{language}" data-theme="{theme}"><body>'
                      '<main id="surface"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    # Host geometry only: all artifact, tool, turn and disclosure CSS is shipped.
    frame.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);height:auto;overflow:auto}'
                               '#surface{max-width:900px;margin:auto}.conversation-turn{--rail-w:0px}')
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r'''locale => {
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window._featureFlags={debug_mode:false};window.projectState={extraRoots:[]};
      window.getActiveConv=()=>null;window.renderMarkdown=value=>escapeHtml(value);
      window.Icon=()=>'<svg viewBox="0 0 24 24" width="14" height="14"><path d="m5 6 6 6-6 6m8 0h6" stroke="currentColor" fill="none"/></svg>';
    }''', locale)
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds_rich.js'))
    frame.add_script_tag(path=native_module_path(
        '.native/tool-artifact-readability-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    fixtures = _fixtures()
    frame.evaluate(r'''({fixtures,language,replaySuffix}) => {
      const scope={llmRound:0,attemptId:'artifact-fixture-attempt',taskId:'artifact-fixture-task'};
      const blocks=fixtures.map((fixture,index)=>({kind:'tool',blockId:fixture.id,ordinal:index+1,source:scope,
        round:{...scope,roundNum:index+1,toolCallId:fixture.id,toolName:fixture.toolName,
          status:fixture.status,query:fixture.query,toolArgs:fixture.toolArgs,
          results:[fixture.metadata||{}],toolContent:fixture.content,
          ...(fixture.origin?{_artifactOrigin:fixture.origin}:{})}}));
      blocks.push({kind:'text',blockId:'answer',ordinal:5,source:{},deliverable:true,terminal:true,
        text:'Outcome',markdown:language==='zh'?'保留的结果与请求范围保持可读。':'Saved results and requested scope stay readable.'});
      const turn={turnId:'artifact-reading-turn',presentationId:'artifact-reading-turn',actor:'assistant',role:'assistant',
        laneId:'main',ordinal:1,status:'completed',terminal:true,blocks,queuedTurns:[],branches:[],actions:[],
        metadata:{translation:{pending:false,streaming:false}},finish:null,
        source:{projection:{timestamp:'2026-10-07T09:00:00Z'},createdAt:'2026-10-07T09:00:00Z'}};
      window.model={conversationId:'artifact-fixture',conversationRevision:1,transport:'ready',
        mainLane:{laneId:'main',title:'Main',kind:'main',expanded:true,turns:[turn]},
        orphanLanes:[],queue:[],planDecision:null,transportNotice:null};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',localizedText:(key,fallback)=>t(key)||fallback,
        renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,false),
      }));
      let revision=1;
      window.replay=(changedId=null)=>{
        const snapshot=JSON.parse(JSON.stringify(model));snapshot.conversationRevision=++revision;
        if(changedId){
          const round=snapshot.mainLane.turns[0].blocks.find(block=>block.blockId===changedId).round;
          round.toolContent+=replaySuffix;
        }
        surface.render(snapshot);
      };
      replay();
    }''', {'fixtures': fixtures, 'language': language, 'replaySuffix': _REPLAY_SUFFIX})

    assert frame.locator('.ptool-result-block').count() == 4
    assert frame.locator('.conversation-round-header').count() == 0
    assert frame.locator('[data-block-id="answer"]').is_visible()
    hidden_tokens = [_SEARCH_ALIAS, _READ_ALIAS, _DIGEST_REF, _SEARCH_CURSOR,
                     'd15ea5ed4a11', 'bbbbbbbbcccc', 'a' * 12, 'tr:', 'tool-result:',
                     '/items/0/details/presentation', '/items/1', '/records/0/content']
    for fixture in fixtures:
        block = frame.locator(f'[data-block-id="{fixture["id"]}"]')
        assert block.locator('details').count() == 1
        card = block.locator('details.ptool-result-block')
        summary = card.locator('summary.ptool-result-header')
        assert summary.count() == 1
        title = summary.locator('.ptool-artifact-title')
        assert title.count() == 1
        assert title.inner_text() == _translated(locale, fixture['titleKey'], n=2, count=2)
        # Attention-bearing failures open on first paint; don't hide their evidence.
        if fixture['status'] == 'error':
            assert card.get_attribute('open') is not None
            summary.focus()
            page.keyboard.press('Enter')
        assert card.get_attribute('open') is None
        assert not card.locator('.ptool-result-pre').is_visible()
        assert card.locator('.ptool-artifact-request').count() == 1
        assert not card.locator('.ptool-artifact-request').is_visible()
        summary_text = summary.inner_text()
        assert all(token not in summary_text for token in hidden_tokens), summary_text
        for number in fixture['requestNumbers']:
            assert not re.search(rf'(?<!\w){number}(?!\w)', _numeric_text(summary_text))
        _assert_terms(page, summary, fixture['terms'], locale, width)
        source = summary.locator('.ptool-artifact-source')
        if 'origin' in fixture:
            assert source.count() == 1
            assert _translated(locale, 'toolArtifact.source', source=_SOURCE) in source.inner_text()
            assert summary_text.count(_SOURCE) == 1
        else:
            assert source.count() == 0, 'Unknown or multiple results must not invent shared provenance'

    failure = frame.locator('[data-block-id="failed-search"]')
    assert failure.locator('summary .ptool-badge-err').inner_text() == locale['tool.failed']
    assert 'artifact_unavailable' in failure.locator('summary').inner_text()
    assert '5 matches' not in failure.locator('summary').inner_text()
    assert frame.evaluate('window.__artifactQueryExecuted !== true')
    _assert_no_overflow(frame)
    frame.evaluate('document.activeElement?.blur()')
    collapsed = tmp_path / f'tool-artifact-readability-collapsed-{width}-{language}-{theme}.png'
    page.locator('#artifact-reading-fixture').screenshot(path=str(collapsed))
    print(f'Saved tool-output readability screenshot: {collapsed}')

    for fixture in fixtures:
        card = frame.locator(f'[data-block-id="{fixture["id"]}"] details.ptool-result-block')
        summary = card.locator('summary.ptool-result-header')
        summary.focus()
        assert summary.evaluate('(node)=>node===document.activeElement')
        page.keyboard.press('Enter')
        assert card.get_attribute('open') is not None
        request = card.locator('.ptool-artifact-request')
        assert request.is_visible()
        request_text = request.inner_text()
        assert locale['toolArtifact.requestDetails'] in request_text
        for number in fixture['requestNumbers']:
            assert re.search(rf'(?<!\w){number}(?!\w)', _numeric_text(request_text)), request_text
        for selector in fixture['selectors']:
            assert selector in request_text
        for term in fixture['terms']:
            assert term in request_text
        if fixture['id'] == 'three-term-search':
            assert locale['toolArtifact.fromStart'] in request_text
            assert locale['toolArtifact.continueSearch'] in request_text
            assert _SEARCH_CURSOR not in request_text, 'Signed cursors are opaque, not invented byte offsets'
        if 'origin' in fixture:
            assert _translated(locale, 'toolArtifact.sourceRound', n=54) in request_text
        code = card.locator('.ptool-result-pre code')
        assert code.inner_text() == fixture['content']
        assert code.locator('b, img').count() == 0
        _assert_no_overflow(frame)
        # Retain the actual disclosure node and focus during a changed-body replay,
        # not just an equal model/cache hit. Results remain separate from requests.
        summary.evaluate('(node)=>window.retainedArtifactSummary=node')
        frame.evaluate('(id)=>replay(id)', fixture['id'])
        assert card.get_attribute('open') is not None
        assert summary.evaluate('(node)=>node===window.retainedArtifactSummary && node===document.activeElement')
        assert request.inner_text() == request_text
        assert code.inner_text() == fixture['content'] + _REPLAY_SUFFIX
        _assert_no_overflow(frame)
        page.keyboard.press('Space')
        assert card.get_attribute('open') is None
        assert not request.is_visible()
        assert summary.evaluate('(node)=>node===document.activeElement')

    # Show disclosed exact read scope while keeping the three-term search visible.
    frame.evaluate('replay()')
    expanded_card = frame.locator('[data-block-id="multi-result-read"] details.ptool-result-block')
    expanded_summary = expanded_card.locator('summary')
    expanded_summary.focus()
    page.keyboard.press('Enter')
    assert expanded_card.locator('.ptool-artifact-request').is_visible()
    assert expanded_card.locator('.ptool-result-pre code').inner_text() == fixtures[1]['content']
    _assert_no_overflow(frame)
    expanded = tmp_path / f'tool-artifact-readability-expanded-{width}-{language}-{theme}.png'
    page.locator('#artifact-reading-fixture').screenshot(path=str(expanded))
    print(f'Saved tool-output readability screenshot: {expanded}')
    page.keyboard.press('Enter')
    assert expanded_card.get_attribute('open') is None
    assert frame.locator('[data-block-id="answer"]').is_visible()
    assert frame.evaluate('window.__artifactQueryExecuted !== true')
    _assert_no_overflow(frame)
    frame.evaluate('surface.dispose()')
