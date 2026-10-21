import { describe, expect, it } from 'vitest';
import policy from '../../../../contracts/llm_canvas_links_v1.json';

import original from '../../../../tests/fixtures/llm_canvas_original_fork.json';
import { llmCanvasExperimentId, recoverLlmCanvasLinks } from './llm-canvas-links';
const base = policy.origin + policy.canvasPathPrefix;
const row = (toolArgs: unknown, extra = {}) => ({ toolName: 'mcp__llm__experiment_get', toolArgs, ...extra });
const success = (data: unknown) => ({ok: true, data});

describe('historical LLM canvas evidence', () => {
  it.each(policy.inputKeys)('recovers exact numeric %s even on failure', key => {
    expect(recoverLlmCanvasLinks(row({[key]: '381153'}, {status: 'error', toolContent: 'Error 991'})))
      .toEqual([{label: '381153', href: base + '381153', role: 'input'}]);
  });
  it('accepts source-verified canvas and run URLs, emits only canvas', () => {
    for (const input of [base + '381153/991/0', base + '381153']) {
      expect(recoverLlmCanvasLinks(row({experiment_or_url: input}))[0].href).toBe(base + '381153');
    }
  });
  it.each([true, 0, -1, 1.2, Number.MAX_SAFE_INTEGER + 1, '0', 'name', '１２３', '123 error',
    'javascript:123', 'http://model.internal.example.com/user/modelManage/experimentalTaskDetail/123',
    base + '123?x=1', base + '123#x', base + '123" onclick="x',
    base.replace('model.internal.example.com', 'evil.test') + '123',
    'https://model.internal.example.com@evil.test/123',
    policy.origin + '/a/../user/modelManage/experimentalTaskDetail/123',
    policy.origin + '/explore/experimental/task/detail' + '?experimentId=123&x=1',
    policy.origin + '/explore/experimental/task/detail' + '?experimentId=123&experimentId=124',
    '1'.repeat(513)])('rejects ambiguous/untrusted input %s', input => {
    expect(llmCanvasExperimentId(input)).toBe('');
  });
  it('keeps metadata primary, accepts empty metadata fallback, never mutates', () => {
    const original = Object.freeze(row(Object.freeze({experiment_id: 381153}), {_mcpLinks: {'canvas': base + '381153'}}));
    expect(recoverLlmCanvasLinks(original)).toEqual([]);
    expect(recoverLlmCanvasLinks({...original, _mcpLinks: {}})).toHaveLength(1);
    expect(recoverLlmCanvasLinks({...original, _mcpLinks: {bad: 'javascript:alert(1)'}})).toEqual([]);
  });
  it('requires successful structured result to resolve a name; labels remain raw data', () => {
    const name = '<script>&"' + 'x'.repeat(60);
    for (const field of ['result', 'toolContent']) for (const serialized of [false, true]) {
      const result = success({experimentId: 381154, canvasUrl: base + '381154'});
      const output = recoverLlmCanvasLinks(row({experiment_id: name}, {[field]: serialized ? JSON.stringify(result) : result}));
      expect(output).toEqual([{label: name.slice(0, 39) + '…', href: base + '381154', role: 'resolved'}]);
    }
  });
  it.each([{ok: false, data: {experimentId: 381154}}, {experimentId: 381154},
    success({id: 381154, taskId: 381154}), success({payload: {experimentId: 381154}}),
    success({experimentId: true}), success({experimentId: 381154, error: 'bad'}),
    success({experimentId: 381154, dry_run: true}),
    success({experimentId: 381154, canvasUrl: base + '381155'}),
    success({experimentId: 381154, page: 'javascript:alert(1)'}),
    'Error: experimentId=381154', '{"ok":true,"data":{"experimentId":381154}} truncated',
    ' '.repeat(65537) + '{"ok":true,"data":{"experimentId":381154}}'])('does not resolve from %s', toolContent => {
    expect(recoverLlmCanvasLinks(row({experiment_id: 'name'}, {toolContent}))).toEqual([]);
  });
  it.each(policy.forkTools)('shows distinct source/new %s even absent from old title', tool => {
    const historical = row({base_id: '381153', name: 'new name'}, {toolName: 'mcp__llm__' + tool,
      query: 'llm/' + tool + ' — 381153', toolContent: JSON.stringify(success({experimentId: 381154}))});
    expect(recoverLlmCanvasLinks(historical)).toEqual([
      {label: 'source: 381153', href: base + '381153', role: 'source'},
      {label: 'new: new name', href: base + '381154', role: 'new'}]);
    expect(recoverLlmCanvasLinks({...historical, toolArgs: {base_id: 'source name', name: 'new name'}}))
      .toEqual([{label: 'new: new name', href: base + '381154', role: 'new'}]);
    expect(recoverLlmCanvasLinks({...historical, status: 'failed'})).toHaveLength(1);
  });
  it('preserves two roles for identical fork source and returned ID', () => {
    const links = recoverLlmCanvasLinks(row({base_id: 123, name: '123'}, {toolName: 'mcp__llm__experiment_fork', toolContent: success({experimentId: 123})}));
    expect(links.map(link => link.role)).toEqual(['source', 'new']);
  });
  it.each(policy.progressiveTools)('unwraps only trusted progressive %s', toolName => {
    expect(recoverLlmCanvasLinks({toolName, toolArgs: JSON.stringify({name: 'mcp__llm__experiment_get', arguments: {experiment_id: 123}})})).toHaveLength(1);
  });
  it('rejects other servers, eval IDs, inherited fields and title-only evidence', () => {
    for (const toolName of ['mcp__other__experiment_get', 'mcp__llm__eval_get', 'llm/experiment_get'])
      expect(recoverLlmCanvasLinks(row({experiment_id: 123}, {toolName}))).toEqual([]);
    expect(recoverLlmCanvasLinks(row(Object.create({experiment_id: 123})))).toEqual([]);
    expect(recoverLlmCanvasLinks(row({}, {query: 'llm/experiment_get — 381153'}))).toEqual([]);
  });
  it('bounds inputs and does not harvest a secondary successful result after failure', () => {
    expect(recoverLlmCanvasLinks(row({ids: '123,124,125'}))).toHaveLength(2);
    expect(recoverLlmCanvasLinks(row({ids: [123, 124, 125]}))).toHaveLength(2);
    expect(recoverLlmCanvasLinks(row({experiment_id: 'name'}, {toolContent: {ok: false}, result: success({experimentId: 123})}))).toEqual([]);
    expect(recoverLlmCanvasLinks(row('{"experiment_id":123} truncated'))).toEqual([]);
  });
});


it('recovers only complete toolContent snapshot previews, never prefixes or private refs', () => {
  const preview = JSON.stringify(success({ experimentId: '381154' }));
  const reference = { fields: ['toolContent'], preview, chars: Array.from(preview).length };
  const fork = row({ base_id: '381153', name: 'New canvas' }, {
    toolName: 'mcp__llm__experiment_fork', status: 'done', _snapshotToolResult: reference });
  expect(recoverLlmCanvasLinks(fork).map(link => link.role)).toEqual(['source', 'new']);
  for (const malformed of [{ ...reference, chars: reference.chars + 1 },
      { ...reference, fields: ['results'] }, { ...reference, preview: preview.slice(0, -1) },
      { ...reference, chars: -1 }, { ...reference, preview: 'tool-result:381154' }]) {
    expect(recoverLlmCanvasLinks({ ...fork, _snapshotToolResult: malformed }).map(link => link.role)).toEqual(['source']);
  }
  expect(recoverLlmCanvasLinks({ ...fork, toolContent: null, result: success({ experimentId: '381154' }) }).map(link => link.role))
    .toEqual(['source']);
  expect(recoverLlmCanvasLinks({ ...fork, status: 'error' }).map(link => link.role)).toEqual(['source']);
});

const wrappedForkData = { experimentId: 381491, experimentName: 'Fork canvas', page: base + '381491' };
const wrappedForkItem = success(wrappedForkData);
const wrappedFork = { items: [wrappedForkItem], summary: 'Tool returned a structured result.' };
const forkRow = (toolContent: unknown, extra = {}) => row({ base_id: 379851, dry_run: false }, {
  toolName: 'mcp__llm__experiment_fork', toolContent, ...extra });

it('recovers the historical sole-item fork wrapper as object, JSON and complete preview', () => {
  const preview = JSON.stringify(wrappedFork);
  const reference = { fields: ['toolContent'], preview, chars: Array.from(preview).length };
  for (const historical of [forkRow(wrappedFork), forkRow(preview),
      { toolName: 'mcp__llm__experiment_fork', toolArgs: { base_id: 379851, dry_run: false },
        _snapshotToolResult: reference }]) {
    expect(recoverLlmCanvasLinks(historical)).toEqual([
      { label: 'source: 379851', href: base + '379851', role: 'source' },
      { label: 'new: Fork canvas', href: base + '381491', role: 'new' }]);
  }
  expect(recoverLlmCanvasLinks(forkRow(preview, {
    toolArgs: { base_id: 379851, dry_run: true } })).map(link => link.role)).toEqual(['source']);
  for (const status of ['failed', 'cancelled', 'canceled', 'partial'])
    expect(recoverLlmCanvasLinks(forkRow(preview, { status })).map(link => link.role)).toEqual(['source']);
});

it('never harvests ambiguous, failed, nested or incomplete wrappers', () => {
  const hostile = [
    { ...wrappedFork, items: [] }, { ...wrappedFork, items: [wrappedForkItem, wrappedForkItem] },
    { ...wrappedFork, items: [wrappedForkItem, { ok: false }] },
    { ...wrappedFork, summary: 'Tool returned 1 items.' },
    { items: [wrappedForkItem] }, { ...wrappedFork, items: { 0: wrappedForkItem, length: 1 } },
    { ...wrappedFork, items: [wrappedFork] },
    { ...wrappedFork, items: [JSON.stringify(wrappedForkItem)] },
    ...[{ ok: false }, { success: false }, { error: 'failed' }, { isError: true },
      { status: 'cancelled' }, { status: 'partial' }, { truncated: true }, { dry_run: true },
      { artifactRef: 'tool-result:partial' }].map(extra => ({ ...wrappedFork, ...extra })),
    ...[{ ok: false }, { success: false }, { error: 'failed' }, { isError: true },
      { status: 'cancelled' }, { status: 'partial' }, { truncated: true }, { dry_run: true }]
      .flatMap(extra => [
        { ...wrappedFork, items: [{ ...wrappedForkItem, ...extra }] },
        { ...wrappedFork, items: [success({ ...wrappedForkData, ...extra })] }]),
    { ...wrappedFork, items: [wrappedForkData] },
    { ...wrappedFork, items: [success({ ...wrappedForkData, page: base + '381492' })] },
    JSON.stringify(wrappedFork).slice(0, -1), JSON.stringify(wrappedFork) + ' truncated',
    ' '.repeat(65537) + JSON.stringify(wrappedFork),
  ];
  for (const wrapper of hostile) {
    for (const content of [wrapper, typeof wrapper === 'string' ? wrapper : JSON.stringify(wrapper)])
      expect(recoverLlmCanvasLinks(forkRow(content)).map(link => link.role)).toEqual(['source']);
  }
  const preview = JSON.stringify(wrappedFork);
  expect(recoverLlmCanvasLinks({ toolName: 'mcp__llm__experiment_fork', toolArgs: { base_id: 379851 },
    _snapshotToolResult: { fields: ['toolContent'], preview, chars: preview.length + 1 } })
    .map(link => link.role)).toEqual(['source']);
});

it('recovers the exact original retained-source fork only with owning completeness evidence', () => {
  const preview = JSON.stringify(original.toolContent);
  for (const toolContent of [original.toolContent, preview]) {
    expect(recoverLlmCanvasLinks({ ...original, toolContent }).map(link => [link.role, link.href]))
      .toEqual([['source', base + '379851'], ['new', base + '381491']]);
  }
  const { toolContent: _content, ...snapshot } = original;
  expect(recoverLlmCanvasLinks({ ...snapshot, _snapshotToolResult: {
    fields: ['toolContent'], preview, chars: Array.from(preview).length } }).map(link => link.role))
    .toEqual(['source', 'new']);
  const hostileEvidence: unknown[] = [undefined, null, {}, Object.create(original.toolResultEvidence),
    ...Object.keys(policy.singleItemWrapper.retainedSourceEvidence).map(key => ({ ...original.toolResultEvidence, [key]: undefined })),
    ...[{ status: 'error' }, { status: 'failed' }, { status: 'cancelled' }, { status: 'partial' },
      { executionStatus: 'failed' }, { retentionCoverage: 'unknown' }, { retentionCoverage: 'partial' },
      { viewCoverage: 'unknown' }, { viewCoverage: 'partial' }, { sourceCoverage: 'partial' },
      { truncated: true }, { truncated: 0 }, { artifactRef: '' }, { resultId: '' }, { evidenceId: '' },
      { isError: true }, { error: 'bad' }, { success: false }, { ok: false }, { dry_run: true }]
      .map(extra => ({ ...original.toolResultEvidence, ...extra }))];
  for (const toolResultEvidence of hostileEvidence)
    expect(recoverLlmCanvasLinks({ ...original, toolResultEvidence }).map(link => link.role)).toEqual(['source']);
  for (const status of ['error', 'failed', 'cancelled', 'partial'])
    expect(recoverLlmCanvasLinks({ ...original, status }).map(link => link.role)).toEqual(['source']);
  expect(recoverLlmCanvasLinks({ ...original, toolArgs: { ...original.toolArgs, dry_run: true } }).map(link => link.role)).toEqual(['source']);
  const item = original.toolContent.items[0];
  for (const toolContent of [
      { ...original.toolContent, items: [item, item] },
      { ...original.toolContent, summary: original.toolContent.summary + ' extra' },
      { ...original.toolContent, toolResultEvidence: original.toolResultEvidence },
      { ...original.toolContent, items: [{ ...item, ok: false }] },
      { ...original.toolContent, items: [{ ...item, data: { ...item.data, page: base + '381492' } }] },
      { ...original.toolContent, items: [{ ...item, data: { ...item.data, page: 'javascript:381491' } }] },
    ]) expect(recoverLlmCanvasLinks({ ...original, toolContent }).map(link => link.role)).toEqual(['source']);
});

it('does not require retention references for undecorated inline complete results', () => {
  const { artifactRef: _ref, evidenceId: _ev, resultId: _id, ...toolResultEvidence } = original.toolResultEvidence;
  const toolContent = { ...original.toolContent, summary: policy.singleItemWrapper.summary };
  expect(recoverLlmCanvasLinks({ ...original, toolContent, toolResultEvidence }).map(link => link.role))
    .toEqual(['source', 'new']);
  expect(recoverLlmCanvasLinks({ ...original, toolContent,
    toolResultEvidence: { ...toolResultEvidence, status: 'error' } }).map(link => link.role)).toEqual(['source']);
});
