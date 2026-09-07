"""Exact owner and retained-wiring contracts for tool/search presentation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER = (
    ROOT / 'frontend/src/conversation/presentation/tool-search-presentation.ts'
)
OWNER_JS = Path(native_module_path(
    '.native/tool-search-presentation-contract.js',
    OWNER,
))


_OWNER_HARNESS = r"""
eval(process.env.OWNER_SOURCE);

const checks = [];
function check(name, condition) {
  checks.push((condition ? 'PASS ' : 'FAIL ') + name);
}
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}
const messages = {
  'toolSearch.found': '{total} candidate matches · showing {shown}',
  'toolSearch.none': 'No matching tools',
  'toolSearch.more': 'more candidates available',
  'toolSearch.failOpen': 'full catalog restored',
  'toolSearch.catalogLimit': 'Showing first {shown} of {total} matches.',
  'toolSearch.resultLimit': 'Showing first {shown} of {total} results.',
  'toolSearch.verticalLimit': 'Additional vertical results omitted from this bounded view.',
  'toolSearch.engineLimit': 'Additional engine sources omitted from this bounded view.',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
function iconHtml(name, size, style) {
  return '<ICON:' + name + ':' + (size || '') + ':' + (style || '') + '>';
}
const presentation = createToolSearchPresentation({ translate, iconHtml });
const header = Object.freeze({
  iconHtml: '<i data-slot="icon"></i>',
  queryHtml: '<b>Projected query</b>',
  rightControlsHtml: '<span data-slot="controls"></span>',
});

check('limits_and_public_port_are_frozen_and_narrow',
  Object.isFrozen(TOOL_SEARCH_PRESENTATION_LIMITS)
  && TOOL_SEARCH_PRESENTATION_LIMITS.toolCatalogRecordsScanned === 512
  && TOOL_SEARCH_PRESENTATION_LIMITS.toolCatalogCards === 64
  && TOOL_SEARCH_PRESENTATION_LIMITS.toolArgumentRows === 8
  && TOOL_SEARCH_PRESENTATION_LIMITS.webResultRows === 100
  && TOOL_SEARCH_PRESENTATION_LIMITS.verticalRecords === 64
  && TOOL_SEARCH_PRESENTATION_LIMITS.verticalSourcesScanned === 256
  && TOOL_SEARCH_PRESENTATION_LIMITS.verticalItemsScanned === 512
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 1
  && typeof presentation.renderSearchHtml === 'function');

const argumentsList = Array.from({ length: 10 }, (_, index) => ({
  name: 'arg_' + index,
  type: index === 0 ? 'string<script>' : 'value',
  required: index === 0,
}));
const catalogRound = Object.freeze({
  status: 'done', toolName: 'search_tools', toolSearchTotal: 73,
  toolSearchNextCursor: 'next', toolSearchFailOpen: true,
});
const catalogResults = Object.freeze([
  Object.freeze({ type: 'unrelated', toolName: 'hidden_tool' }),
  Object.freeze({
    type: 'tool_catalog_match',
    toolName: 'mcp__xuecheng__update_doc',
    namespace: 'xuecheng',
    snippet: 'Update <the> document.',
    arguments: Object.freeze(argumentsList.map(Object.freeze)),
  }),
]);
const catalogBefore = JSON.stringify([catalogRound, catalogResults, header]);
const catalogHtml = presentation.renderSearchHtml(
  catalogRound, catalogResults, header,
);
check('tool_catalog_uses_projected_count_and_trusted_header_slots',
  catalogHtml.includes('ptool-tool-search-block')
  && catalogHtml.includes('73 candidate matches · showing 1')
  && catalogHtml.includes('<i data-slot="icon"></i>')
  && catalogHtml.includes('<b>Projected query</b>')
  && catalogHtml.includes('data-slot="controls"'));
check('tool_catalog_renders_only_native_match_records',
  catalogHtml.includes('mcp__xuecheng__update_doc')
  && catalogHtml.includes('xuecheng')
  && !catalogHtml.includes('hidden_tool'));
check('tool_arguments_are_bounded_and_required_state_is_visible',
  catalogHtml.includes('ptool-tool-arg-required')
  && catalogHtml.includes('arg_7')
  && !catalogHtml.includes('arg_8')
  && catalogHtml.includes('ptool-tool-arg-more">+2'));
check('tool_catalog_escapes_untrusted_schema_copy',
  catalogHtml.includes('Update &lt;the&gt; document.')
  && catalogHtml.includes('string&lt;script&gt;')
  && !catalogHtml.includes('<the>'));
check('tool_catalog_surfaces_cursor_and_fail_open_state',
  catalogHtml.includes('more candidates available')
  && catalogHtml.includes('full catalog restored'));
check('tool_catalog_projection_is_not_mutated',
  JSON.stringify([catalogRound, catalogResults, header]) === catalogBefore);

const manyMatches = Array.from({ length: 600 }, (_, index) => ({
  type: 'tool_catalog_match', toolName: 'tool_' + index,
}));
Object.defineProperty(manyMatches, 512, {
  get() { throw new Error('catalog scan exceeded its budget'); },
});
const boundedCatalogHtml = presentation.renderSearchHtml(
  { status: 'done', toolName: 'search_tools', toolSearchTotal: 600 },
  manyMatches,
  header,
);
check('tool_catalog_card_count_has_a_visible_hard_bound',
  occurrences(boundedCatalogHtml, '<div class="ptool-tool-search-card">') === 64
  && boundedCatalogHtml.includes('600 candidate matches · showing 64')
  && boundedCatalogHtml.includes('Showing first 64 of 600 matches.'));
const emptyCatalogHtml = presentation.renderSearchHtml(
  { status: 'done', toolName: 'search_tools', toolSearchTotal: 0 },
  [],
  header,
);
check('empty_tool_catalog_has_localized_explanation',
  emptyCatalogHtml.includes('No matching tools')
  && emptyCatalogHtml.includes('0 candidate matches · showing 0'));
check('tool_catalog_status_and_family_guards_fail_closed',
  presentation.renderSearchHtml({
    status: 'searching', toolName: 'search_tools',
  }, catalogResults, header) === ''
  && presentation.renderSearchHtml({
    status: 'done', toolName: 'read_files',
  }, catalogResults, header) === '');

const plainZeroHtml = presentation.renderSearchHtml(
  { toolName: 'web_search' }, [], header,
);
check('zero_result_search_has_an_explicit_terminal_badge',
  plainZeroHtml.includes('ptool-badge-warn">no results</span>'));
const networkZeroHtml = presentation.renderSearchHtml({
  toolName: 'web_search', searchDiag: { reason: 'network_error' },
}, [], header);
check('network_failure_is_not_presented_as_no_matches',
  networkZeroHtml.includes('ptool-badge-err">network error</span>')
  && networkZeroHtml.includes('limited internet access'));
const partialZeroHtml = presentation.renderSearchHtml({
  toolName: 'web_search', cacheSource: 'prefetch',
  searchDiag: {
    reason: 'partial_network_error',
    engine_errors: { 'bad<script>': 'offline' },
  },
}, [], header);
check('partial_failure_names_safe_engines_and_cache_source',
  partialZeroHtml.includes('partial failure')
  && partialZeroHtml.includes('bad&lt;script&gt;')
  && !partialZeroHtml.includes('bad<script>')
  && partialZeroHtml.includes('streaming prefetch'));
check('exception_and_clean_no_match_have_distinct_copy',
  presentation.renderSearchHtml({
    toolName: 'web_search', searchDiag: { reason: 'exception' },
  }, [], header).includes('internal error')
  && presentation.renderSearchHtml({
    toolName: 'web_search', searchDiag: { reason: 'no_matches' },
  }, [], header).includes('Try different keywords'));

const webResults = Object.freeze([
  Object.freeze({
    _q: 'safe <query>', title: 'Safe <title>', source: 'PDF',
    url: 'https://example.test/a?x=<y>', snippet: '<summary>',
    fetched: true, fetchedChars: 2500,
  }),
  Object.freeze({
    _q: 'second', title: 'Unsafe URL', source: 'other',
    url: 'javascript:alert(1)', irrelevant: true,
  }),
]);
const webBefore = JSON.stringify(webResults);
const webHtml = presentation.renderSearchHtml({
  roundNum: 8, toolName: 'web_search',
}, webResults, header);
check('web_results_allow_only_http_links_but_keep_visible_url_text',
  webHtml.includes('href="https://example.test/a?x=&lt;y&gt;"')
  && !webHtml.includes('href="javascript:')
  && webHtml.includes('javascript:alert(1)'));
check('web_result_copy_is_escaped',
  webHtml.includes('Safe &lt;title&gt;')
  && webHtml.includes('&lt;summary&gt;')
  && !webHtml.includes('<summary>'));
check('fetched_and_irrelevant_states_remain_distinct',
  webHtml.includes('search-result-fetched pdf">3k chars')
  && webHtml.includes('>irrelevant</span>'));
check('multi_query_results_keep_ordered_group_headers',
  occurrences(webHtml, 'search-query-group-header') === 2
  && webHtml.indexOf('safe &lt;query&gt;') < webHtml.indexOf('second')
  && webHtml.includes('<ICON:search:13:>'));
check('web_header_uses_explicit_slots_and_result_count',
  webHtml.includes('<i data-slot="icon"></i>')
  && webHtml.includes('<b>Projected query</b>')
  && webHtml.includes('data-slot="controls"')
  && webHtml.includes('2 results'));
check('web_result_projection_is_not_mutated',
  JSON.stringify(webResults) === webBefore);

const manyWebResults = Array.from({ length: 105 }, (_, index) => ({
  title: 'result-' + index, url: 'https://example.test/' + index,
}));
Object.defineProperty(manyWebResults, 100, {
  get() { throw new Error('web-result scan exceeded its budget'); },
});
const boundedWebHtml = presentation.renderSearchHtml(
  { roundNum: 9, toolName: 'web_search' },
  manyWebResults,
  header,
);
check('web_result_rows_have_a_visible_hard_bound',
  occurrences(boundedWebHtml, '<div class="search-result-item">') === 100
  && boundedWebHtml.includes('Showing first 100 of 105 results.'));

const paperOne = Object.freeze({
  title: 'Paper One', url: 'https://papers.test/one', upvotes: 1,
});
const paperTwo = Object.freeze({
  title: 'Paper Two', url: 'https://papers.test/two', citations: 10,
});
const extraPapers = Array.from({ length: 12 }, (_, index) => Object.freeze({
  title: 'Extra ' + index, url: 'https://papers.test/extra-' + index,
}));
const verticalRound = Object.freeze({
  roundNum: 10,
  toolName: 'web_search',
  verticals: Object.freeze([
    Object.freeze({
      domain: 'academic', query: 'transformer',
      sources: Object.freeze([Object.freeze({ source: 'hf', identifier: 'p' })]),
      items: Object.freeze([paperOne, paperTwo, ...extraPapers]),
    }),
    Object.freeze({
      batch: Object.freeze([Object.freeze({
        domain: 'academic',
        sources: Object.freeze([Object.freeze({ source: 'hf', identifier: 'p' })]),
        items: Object.freeze([Object.freeze({
          title: 'Paper One', url: 'https://papers.test/one',
          upvotes: 9, citations: 3,
        })]),
      })]),
    }),
  ]),
});
const verticalBefore = JSON.stringify(verticalRound);
const verticalHtml = presentation.renderSearchHtml(
  verticalRound,
  [{ title: 'web', url: 'https://example.test' }],
  header,
);
check('vertical_batches_merge_into_one_domain_card',
  occurrences(verticalHtml, '<div class="vertical-card vertical-domain-academic">') === 1
  && verticalHtml.includes('vertical: academic'));
check('vertical_items_deduplicate_and_keep_highest_metrics',
  occurrences(verticalHtml, '>Paper One</a>') === 1
  && verticalHtml.includes('>9</span>')
  && verticalHtml.includes('>3</span>')
  && verticalHtml.indexOf('Paper One') < verticalHtml.indexOf('Paper Two'));
check('vertical_sources_and_query_are_preserved_once',
  verticalHtml.includes('>hf · transformer</span>')
  && occurrences(verticalHtml, '>hf · transformer</span>') === 1);
check('vertical_cards_keep_twelve_rows_and_report_more',
  occurrences(verticalHtml, '<div class="vertical-row">') === 12
  && verticalHtml.includes('vertical-card-more'));
check('vertical_merge_does_not_mutate_frozen_projection_items',
  JSON.stringify(verticalRound) === verticalBefore
  && paperOne.upvotes === 1);

const hostileVerticalHtml = presentation.renderSearchHtml({
  roundNum: 11,
  toolName: 'web_search',
  vertical: {
    domain: 'Academic" onclick="injected',
    items: [{ title: '<paper>' }],
  },
}, [{ title: 'web' }], header);
check('vertical_domain_and_title_are_visible_but_attribute_safe',
  hostileVerticalHtml.includes('Academic&quot; onclick=&quot;injected sources')
  && hostileVerticalHtml.includes('&lt;paper&gt;')
  && !hostileVerticalHtml.includes('class="vertical-card vertical-domain-academic"'));

const verticalOverflowItems = Array.from(
  { length: 520 },
  (_, index) => ({ title: 'v-' + index }),
);
Object.defineProperty(verticalOverflowItems, 512, {
  get() { throw new Error('vertical-item scan exceeded its budget'); },
});
const verticalOverflowHtml = presentation.renderSearchHtml({
  roundNum: 12,
  toolName: 'web_search',
  vertical: {
    domain: 'code',
    items: verticalOverflowItems,
  },
}, [{ title: 'web' }], header);
check('vertical_scan_budget_is_visible_when_exhausted',
  verticalOverflowHtml.includes('Additional vertical results omitted'));

const guardedVerticalRecords = Array.from({ length: 70 }, (_, index) => ({
  domain: 'domain-' + index,
  items: [{ title: 'record-' + index }],
}));
Object.defineProperty(guardedVerticalRecords, 64, {
  get() { throw new Error('vertical-record scan exceeded its budget'); },
});
const boundedVerticalRecordsHtml = presentation.renderSearchHtml({
  roundNum: 12,
  toolName: 'web_search',
  vertical: { batch: guardedVerticalRecords },
}, [{ title: 'web' }], header);
check('vertical_record_budget_stops_before_reading_the_tail',
  occurrences(boundedVerticalRecordsHtml, '<div class="vertical-card ') === 64
  && boundedVerticalRecordsHtml.includes('Additional vertical results omitted'));

const guardedVerticalSources = Array.from({ length: 300 }, (_, index) => ({
  source: 'source-' + index,
}));
Object.defineProperty(guardedVerticalSources, 256, {
  get() { throw new Error('vertical-source scan exceeded its budget'); },
});
const boundedVerticalSourcesHtml = presentation.renderSearchHtml({
  roundNum: 12,
  toolName: 'web_search',
  vertical: {
    domain: 'code',
    sources: guardedVerticalSources,
    items: [{ title: 'bounded-source-result' }],
  },
}, [{ title: 'web' }], header);
check('vertical_source_budget_stops_before_reading_the_tail',
  boundedVerticalSourcesHtml.includes('source-255')
  && !boundedVerticalSourcesHtml.includes('source-256')
  && boundedVerticalSourcesHtml.includes('Additional vertical results omitted'));

const engineRound = Object.freeze({
  roundNum: 13,
  toolName: 'web_search',
  engineBreakdown: Object.freeze({
    google: Object.freeze([Object.freeze({
      title: 'Safe', url: 'https://example.test/safe',
    })]),
    hostile: Object.freeze([Object.freeze({
      title: '<unsafe>', url: 'javascript:alert(1)',
    })]),
  }),
});
const engineHtml = presentation.renderSearchHtml(
  engineRound, [{ title: 'web' }], header,
);
check('engine_breakdown_reports_raw_to_final_and_toggle_action',
  engineHtml.includes('2 raw → 1 final')
  && engineHtml.includes("classList.toggle('eb-expanded')")
  && engineHtml.includes('<ICON:chevronDown:16:transform:rotate(-90deg)>'));
check('engine_urls_follow_the_same_safe_link_policy',
  engineHtml.includes('href="https://example.test/safe"')
  && !engineHtml.includes('href="javascript:')
  && engineHtml.includes('&lt;unsafe&gt;'));

const hugeBreakdown = {};
for (let engine = 0; engine < 40; engine += 1) {
  hugeBreakdown['engine-' + engine] = Array.from({ length: 20 }, (_, index) => ({
    title: 'source', url: 'https://example.test/' + engine + '/' + index,
  }));
}
const boundedEngineHtml = presentation.renderSearchHtml({
  roundNum: 14, toolName: 'web_search', engineBreakdown: hugeBreakdown,
}, [{ title: 'web' }], header);
check('engine_breakdown_has_count_and_url_budgets',
  occurrences(boundedEngineHtml, '<div class="eb-engine">') === 32
  && occurrences(boundedEngineHtml, '<div class="eb-url-item">') === 512
  && boundedEngineHtml.includes('Additional engine sources omitted'));

const hostileRoundIdHtml = presentation.renderSearchHtml({
  roundNum: '1" onmouseover="injected', toolName: 'web_search',
}, [{ title: 'web' }], header);
check('round_identity_attribute_is_escaped',
  hostileRoundIdHtml.includes('data-rn="1&quot; onmouseover=&quot;injected"')
  && !hostileRoundIdHtml.includes('data-rn="1" onmouseover="injected"'));
check('invalid_inputs_and_unrelated_tools_fail_closed',
  presentation.renderSearchHtml(null, null, header) === ''
  && presentation.renderSearchHtml({ toolName: 'read_files' }, [], header) === '');

console.log(checks.join('\n'));
"""


_WIRING_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2]],
  globals: {
    _featureFlags: { debug_mode: false },
    projectState: { extraRoots: [] },
  },
});

check('entry_exposed', typeof renderToolRoundsHTML === 'function');
const catalogHtml = _renderUnifiedToolLine({
  status: 'done', toolName: 'search_tools', query: '编辑学城文档',
  toolSearchTotal: 1,
  results: [{
    type: 'tool_catalog_match', toolName: 'mcp__xuecheng__update_doc',
    namespace: 'xuecheng',
  }],
}, false);
check('catalog_owner_is_wired',
  catalogHtml.includes('ptool-tool-search-block')
  && catalogHtml.includes('mcp__xuecheng__update_doc'));

const webHtml = _renderUnifiedToolLine({
  roundNum: 2, status: 'done', toolName: 'web_search', query: 'q',
  results: [{ title: 'result', url: 'https://example.test' }],
}, false);
check('web_owner_is_wired',
  webHtml.includes('ptool-results-block') && webHtml.includes('result'));

const boundedCatalogHtml = _renderUnifiedToolLine({
  status: 'done', toolName: 'search_tools', query: 'many',
  toolSearchTotal: 70,
  results: Array.from({ length: 70 }, (_, index) => ({
    type: 'tool_catalog_match', toolName: 'tool_' + index,
  })),
}, false);
check('owner_bound_reaches_retained_dispatch',
  boundedCatalogHtml.split('<div class="ptool-tool-search-card">').length - 1 === 64
  && boundedCatalogHtml.includes('ptool-tool-search-limit'));

const gatewayOnly = renderToolRoundsHTML([{
  roundNum: 1, llmRound: 0, status: 'done', toolName: 'execute_tools',
  query: 'execute_tools', toolContent: '{"status":"ok"}', results: [],
}], false);
check('protocol_gateway_stays_hidden', gatewayOnly === '');
const childOnly = renderToolRoundsHTML([
  {
    roundNum: 1, llmRound: 0, status: 'done', toolName: 'execute_tools',
    query: 'execute_tools', toolContent: '{"status":"ok"}', results: [],
  },
  {
    roundNum: 8700000, llmRound: 0, status: 'error', toolName: 'read_files',
    query: 'read_files', toolContent: 'failed',
    results: [{ toolName: 'read_files', title: 'read_files', badge: 'error' }],
  },
], false);
check('gateway_child_remains_visible',
  childOnly.includes('read_files')
  && !childOnly.includes('execute_tools')
  && childOnly.includes('data-full-count="1"'));
report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_tool_search_presentation_owner_contract() -> None:
    process = subprocess.run(
        [shutil.which('node'), '-e', _OWNER_HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            'OWNER_SOURCE': OWNER_JS.read_text(encoding='utf-8'),
        },
    )
    assert process.returncode == 0, process.stderr
    failures = [
        line for line in process.stdout.splitlines() if line.startswith('FAIL ')
    ]
    assert not failures, process.stdout
    passes = [
        line for line in process.stdout.splitlines() if line.startswith('PASS ')
    ]
    assert len(passes) == 35, process.stdout


def test_retained_dispatch_wires_search_owner_and_filters_gateway() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_WIRING_HARNESS,
        expect_pass=6,
        label='tool-search retained wiring',
    )
