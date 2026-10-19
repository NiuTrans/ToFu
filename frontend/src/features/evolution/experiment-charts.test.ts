// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
vi.mock('../../i18n', () => ({t: (key: string) => key}));
import { experimentSeries, mountExperimentCharts } from './programs/experiment-charts';
import { evidenceCharts } from './evidence-charts';
import type { ExplorationReceipt, ExplorationProgram } from '../../api/evolution-programs';

const receipt = (sequence: number, tokens: number, complete = true) => ({
  sequence, program_id: 'a', actuals: {tokens, tool_calls: sequence, wall_clock_seconds: 10},
  actuals_complete: complete, outcome: 'completed', review: {outcome: 'success'},
}) as ExplorationReceipt;
const program = (id = 'a') => ({program_id: id, actuals: {tokens: 99}, actuals_complete: false}) as ExplorationProgram;
const chart = {version: 1, title: 'Measured latency', blocks: [{type: 'data_chart', id: 'latency', title: 'Latency',
  kind: 'bar', series: [{id: 'measured', label: 'Measured', points: [{label: 'Baseline', value: 123.456}, {label: 'Candidate', value: 92}]}]}]};

describe('experiment evidence charts', () => {
  it('keeps exact measurements, zero and lower bounds, and omits invalid data without mutating receipts', () => {
    const records = [receipt(4, 123.456), receipt(2, 7, false), receipt(3, NaN), receipt(1, 0), receipt(5, -1)];
    const before = records.slice();
    const series = experimentSeries(records, 'tokens');
    expect(series[0]?.points).toEqual([{x: 1, label: '#1', value: 0}, {x: 4, label: '#4', value: 123.456}]);
    expect(series[1]?.points[0]).toEqual({x: 2, label: '≥ #2', value: 7});
    expect(records).toEqual(before);
  });
  it('preserves reader state on unchanged polls, resets on owner changes and never charts another owner', () => {
    const host = document.createElement('section');
    const view = mountExperimentCharts(host);
    const rows = [receipt(1, 7), {...receipt(2, 13), program_id: 'foreign'}];
    view.update(program(), rows);
    expect(host.querySelectorAll('tbody tr')).toHaveLength(1);
    const plot = host.querySelector('svg');
    const data = host.querySelector('details')!;data.open = true;
    host.querySelector<HTMLButtonElement>('[data-chart-kind=line]')!.click();
    const line = host.querySelector('svg');expect(line).not.toBe(plot);
    view.update(program(), rows);
    expect(host.querySelector('svg')).toBe(line);expect(data.open).toBe(true);
    expect(host.textContent).toContain('≥ 99');
    view.update(program('b'), []);
    expect(host.querySelector('svg')).toBeNull();expect(data.open).toBe(false);
    expect(host.querySelector('[data-chart-kind=bar]')?.getAttribute('aria-pressed')).toBe('true');
  });
  it('renders admitted chart fences and exact tables without admitting HTML or partial payloads', () => {
    const source = 'Evidence\n```tofu-ui\n'+JSON.stringify(chart)+'\n```';
    const rendered = evidenceCharts(document, source)!;
    expect(rendered.querySelector('svg')).not.toBeNull();
    expect(rendered.querySelector('table')?.textContent).toContain('123.456');
    expect(evidenceCharts(document, source.replace(/\n```$/, ''))).toBeNull();
    expect(evidenceCharts(document, '```tofu-ui\n<script>alert(1)</script>\n```')).toBeNull();
    const unsafe = structuredClone(chart);unsafe.blocks[0]!.series[0]!.points[0]!.label = '<img src=x onerror=alert(1)>';
    expect(evidenceCharts(document, '```tofu-ui\n'+JSON.stringify(unsafe)+'\n```')?.querySelector('img')).toBeNull();
    expect(evidenceCharts(document, Array(20).fill(source).join('\n'))?.querySelectorAll('svg')).toHaveLength(8);
  });
});
