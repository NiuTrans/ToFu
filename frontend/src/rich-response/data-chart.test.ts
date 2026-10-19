// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { chartDomain, renderDataChart } from './data-chart';
import { validateRichResponse } from './validation';
import type { data_chart } from './contract.generated';
const text = (_key: string, fallback: string) => fallback;
function chart(kind: data_chart['kind'] = 'line'): data_chart {
  return { type: 'data_chart', id: 'data', title: 'Measurements', kind, xLabel: 'Time', yLabel: 'Delta', xUnit: 's', yUnit: 'ms',
    series: [{ id: 'a', label: 'Observed', points: [{ x: 2, value: -4, label: 'First' }, { x: 0, value: 0, label: 'Zero' }, { x: 1, value: 6, label: 'Last' }] }] };
}
const admitted = (block: unknown) => validateRichResponse({ version: 1, title: 'Report', blocks: [block] });
function geometry(root: HTMLElement) {
  root.querySelectorAll('svg [x],svg [y],svg [cx],svg [cy],svg [height],svg [width],svg [d]').forEach(node => {
    for (const name of ['x', 'y', 'cx', 'cy', 'height', 'width']) if (node.hasAttribute(name)) expect(Number.isFinite(Number(node.getAttribute(name)))).toBe(true);
    expect(node.getAttribute('d') ?? '').not.toMatch(/NaN|Infinity|undefined/);
    if (node.hasAttribute('height')) expect(Number(node.getAttribute('height'))).toBeGreaterThanOrEqual(0);
  });
}
describe('generated bounded data-chart contract', () => {
  it.each(['line', 'scatter', 'bar', 'pie'] as const)('admits the %s discriminator with bounded numbers', kind => {
    const block = chart(kind); if (kind === 'pie') block.series[0].points[0].value = 4;
    expect(admitted(block)).not.toBeNull();
  });
  it('admits maximum size and numeric endpoints without changing comparison chart', () => {
    const block = chart();
    block.series = Array.from({ length: 8 }, (_, i) => ({ id: `s${i}`, label: `Series ${i}`, points: Array.from({ length: 64 }, (_, j) => ({ x: j, value: j % 2 ? -1e12 : 1e12 })) }));
    expect(admitted(block)).not.toBeNull();
    expect(admitted({ type: 'chart', id: 'legacy', title: 'Comparison', metrics: [{ label: 'Latency', baseline: 1, candidate: 2, unit: 's', direction: 'lower', status: 'failed' }] })).not.toBeNull();
    expect(renderDataChart(document, block, text).querySelectorAll('tbody tr')).toHaveLength(512);
  });
  it.each(['empty-series', 'series-limit', 'empty-points', 'nan', 'infinity', 'value-bound', 'x-bound', 'label-bound', 'extra', 'svg', 'kind'])( 'rejects %s', mode => {
    const block: any = chart();
    if (mode === 'empty-series') block.series = [];
    if (mode === 'series-limit') block.series = Array(9).fill(block.series[0]);
    if (mode === 'empty-points') block.series[0].points = [];
    if (mode === 'nan') block.series[0].points[0].value = NaN;
    if (mode === 'infinity') block.series[0].points[0].x = Infinity;
    if (mode === 'value-bound') block.series[0].points[0].value = -1e12 - 1;
    if (mode === 'x-bound') block.series[0].points[0].x = 1e12 + 1;
    if (mode === 'label-bound') block.series[0].label = 'a'.repeat(161);
    if (mode === 'extra') block.series[0].points[0].onclick = 'alert(1)';
    if (mode === 'svg') block.svg = '<svg onload="alert(1)"/>';
    if (mode === 'kind') block.kind = 'remote';
    expect(admitted(block)).toBeNull();
  });
});
describe('native chart geometry and nonvisual evidence', () => {
  it('uses deterministic singleton/zero and signed domains', () => {
    expect(chartDomain([0], true)).toEqual([-1, 1]);
    expect(chartDomain([7])).toEqual([6, 8]);
    expect(chartDomain([-4, 6, 0], true)).toEqual([-4, 6]);
    expect(chartDomain([6, 0, -4], true)).toEqual([-4, 6]);
    expect(chartDomain([-3, -2], true)).toEqual([-3, 0]);
    expect(chartDomain([3, 2], true)).toEqual([0, 3]);
  });
  it.each(['line', 'scatter', 'bar'] as const)('preserves zero and negatives for %s', kind => {
    const root = renderDataChart(document, chart(kind), text); geometry(root);
    expect(root.querySelector('svg')?.getAttribute('aria-label')).toBe('Measurements · ' + kind);
    expect(root.querySelector('svg')?.getAttribute('data-y-domain')).toBe('-4,6');
    expect(root.querySelectorAll('th[scope=col]')).toHaveLength(5);
    expect(root.querySelectorAll('tbody th[scope=row]')).toHaveLength(3);
    expect(root.querySelector('caption')?.textContent).toContain('Measurements');
    expect(root.querySelector('tbody')?.textContent).toContain('-4 ms');
    expect(root.querySelector('tbody')?.textContent).toContain('0 ms');
    if (kind === 'bar') expect([...root.querySelectorAll('rect')].map(node => Number(node.getAttribute('height')))).toEqual([124, 0, 186]);
    if (kind === 'scatter') expect(root.querySelectorAll('circle')).toHaveLength(3);
  });
  it('sorts line geometry stably by x but retains original table order', () => {
    const block = chart(); const root = renderDataChart(document, block, text);
    expect(root.querySelector('path')?.getAttribute('d')).toBe('M 90 216 L 430 30 L 770 340');
    expect(root.querySelector('tbody tr')?.textContent).toContain('First');
    expect(block.series[0].points.map(point => point.x)).toEqual([2, 0, 1]);
    expect(renderDataChart(document, block, text).innerHTML).toBe(root.innerHTML);
  });
  it('uses finite geometry for all-zero/constant/extreme Cartesian data', () => {
    for (const kind of ['line', 'scatter', 'bar'] as const) for (const n of [0, -1e12, 1e12]) {
      const block = chart(kind); block.series[0].points = [{ x: n, value: n, label: 'Constant' }];
      geometry(renderDataChart(document, block, text));
    }
  });
  it('renders pie slices with complete zero-value evidence and single-full-circle support', () => {
    const block = chart('pie'); block.series[0].points = [{ label: 'Empty', value: 0 }, { label: 'All', value: 5 }];
    const root = renderDataChart(document, block, text); geometry(root);
    expect(root.querySelectorAll('circle')).toHaveLength(1);
    expect(root.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(root.querySelector('tbody tr')?.textContent).toContain('0 ms');
    expect(root.querySelector('svg text')?.textContent).toBe('2');
    block.series[0].points.push({ label: 'Other', value: 5 });
    const split = renderDataChart(document, block, text); geometry(split);
    expect(split.querySelectorAll('path')).toHaveLength(2);
  });
  it('preserves numeric precision and uses localization ports and text nodes exclusively', () => {
    const block = chart('scatter'); block.series[0].points[0].value = 0.1234567890123456;
    block.title = '<svg onload=alert(1)>'; block.series[0].label = '<img src=x onerror=alert(1)>';
    const root = renderDataChart(document, block, (key, fallback) => key === 'rich.chartData' ? '图表数据' : fallback);
    expect(root.querySelector('tbody')?.textContent).toContain('0.1234567890123456 ms');
    expect(root.querySelector('caption')?.textContent).toContain('图表数据');
    expect(root.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(root.querySelectorAll('img,script,foreignObject,use,a,style')).toHaveLength(0);
    root.querySelectorAll('*').forEach(node => [...node.attributes].forEach(attr => expect(attr.name).not.toMatch(/^on|href|style$/)));
    expect(root.querySelectorAll('[tabindex="0"]')).toHaveLength(2);
  });
});

import { renderRichResponse, disposeRichResponse } from './renderer';
describe('production chart admission', () => {
  it.each(['duplicate', 'missing-x', 'missing-label', 'negative-pie', 'zero-pie', 'multi-pie'])('rejects %s', mode => {
    const block = chart(mode.includes('pie') ? 'pie' : mode === 'missing-label' ? 'bar' : 'line');
    if (mode === 'duplicate') block.series.push(structuredClone(block.series[0]));
    if (mode === 'missing-x') delete block.series[0].points[0].x;
    if (mode === 'missing-label') delete block.series[0].points[0].label;
    if (mode === 'zero-pie') block.series[0].points.forEach(point => point.value = 0);
    if (mode === 'multi-pie') {
      block.series[0].points.forEach(point => point.value = 1);
      block.series.push({ ...structuredClone(block.series[0]), id: 'other' });
    }
    expect(admitted(block)).toBeNull();
  });
  it.each(['line', 'scatter', 'bar', 'pie'] as const)('dispatches %s through native responses', kind => {
    const block = chart(kind); if (kind === 'pie') block.series[0].points[0].value = 4;
    const host = document.createElement('div');
    expect(renderRichResponse(host, { version: 1, title: 'Report', blocks: [block] })).toBe(true);
    expect(host.querySelector('.rich-data-chart-plot')).not.toBeNull();
    expect(host.querySelectorAll('tbody tr')).toHaveLength(3);
    disposeRichResponse(host);
  });
});


it('retains all series and points above the retired collection limits',()=>{
  const block=chart();block.series=Array.from({length:9},(_,i)=>({id:'s'+i,label:'Series '+i,points:Array.from({length:65},(_,p)=>({x:p,value:i+p}))}));
  expect(admitted(block)).not.toBeNull();
  expect(renderDataChart(document,block,text).querySelectorAll('tbody tr')).toHaveLength(585);
});
it('calculates domains without spreading large arrays into argument slots',()=>{
  const values=Array.from({length:250000},(_,i)=>i-125000);
  expect(chartDomain(values)).toEqual([-125000,124999]);
});
