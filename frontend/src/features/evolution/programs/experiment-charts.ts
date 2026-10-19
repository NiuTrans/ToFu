/** Notebook chart island. Projects the current bounded history page and confirmed
 * totals. Shared chart/UI owners render it; the notebook remains the read owner.
 */
import type { ExplorationProgram, ExplorationReceipt } from '../../../api/evolution-programs';
import type { data_chart } from '../../../rich-response/contract.generated';
import { renderDataChart } from '../../../rich-response/data-chart';
import { createButton } from '../../../ui/index';
import { chartCopy as copy, richChartText } from '../evidence-charts';

type Metric = 'tokens' | 'tool_calls' | 'wall_clock_seconds';
const metrics: Array<[Metric, string]> = [['tokens', 'resultsTokens'], ['wall_clock_seconds', 'resultsSeconds'], ['tool_calls', 'resultsCalls']];
const measured = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;

export function experimentSeries(receipts: ExplorationReceipt[], metric: Metric): data_chart['series'] {
  return [true, false].flatMap(complete => {
    const points = receipts.filter(item => (item.actuals_complete !== false) === complete && measured(item.actuals?.[metric]))
      .slice(0, 20).sort((a, b) => a.sequence - b.sequence)
      .map(item => ({x: item.sequence, label: `${complete ? '' : '≥ '}#${item.sequence}`, value: item.actuals[metric]}));
    return points.length ? [{id: complete ? 'measured' : 'incomplete', label: copy(complete ? 'resultsMeasured' : 'resultsIncomplete'), points}] : [];
  });
}

export function mountExperimentCharts(host: HTMLElement) {
  const doc = host.ownerDocument;
  const node = (tag: string, text = '', className = '') => {
    const el = doc.createElement(tag);el.textContent = text;el.className = className;return el;
  };
  host.className = 'exp-observations ui-stack';
  const heading = node('header', '', 'ui-row ui-row--between');
  heading.append(node('h2', copy('resultsTitle')));
  const stats = node('dl', '', 'exp-chart-stats');
  const controls = node('div', '', 'ui-row ui-row--wrap');controls.setAttribute('role', 'group');controls.setAttribute('aria-label', copy('resultsTitle'));
  const plot = node('div', '', 'exp-chart-plot');
  const table = doc.createElement('details');table.className = 'exp-chart-data';
  table.append(node('summary', copy('resultsRaw')));
  const tableHost = node('div');table.append(tableHost);
  const note = node('p', copy('resultsUnknown'), 'exp-muted');
  host.append(heading, node('p', copy('resultsScope'), 'exp-muted'), stats, controls, plot, table, note);
  let receipts: ExplorationReceipt[] = [], metric: Metric = 'tokens', kind: 'bar' | 'line' = 'bar', signature = '', owner = '';
  const metricButtons = metrics.map(([key, label]) => {
    const button = createButton({label: copy(label), size: 'sm', variant: 'secondary', onClick: () => {metric = key;draw();}}, doc);
    button.dataset.chartMetric = key;controls.append(button);return {key, button, label};
  });
  const kindButtons = (['bar', 'line'] as const).map(key => {
    const button = createButton({label: copy(key === 'bar' ? 'resultsBar' : 'resultsLine'), size: 'sm', variant: 'ghost', onClick: () => {kind = key;draw();}}, doc);
    button.dataset.chartKind = key;controls.append(button);return {key, button};
  });
  function draw() {
    metricButtons.forEach(({key, button, label}) => {button.setAttribute('aria-pressed', String(key === metric));button.textContent=(key === metric ? '✓ ' : '')+copy(label);});
    kindButtons.forEach(({key, button}) => {button.setAttribute('aria-pressed', String(key === kind));button.textContent=(key === kind ? '✓ ' : '')+copy(key === 'bar' ? 'resultsBar' : 'resultsLine');});
    const series = experimentSeries(receipts, metric);
    table.hidden = !series.length;
    if (!series.length) {plot.replaceChildren(node('p', copy('resultsEmpty'), 'exp-muted'));tableHost.replaceChildren();return;}
    const label = copy(metrics.find(([key]) => key === metric)![1]);
    const chart = renderDataChart(doc, {type: 'data_chart', id: 'experiment-usage', title: label, kind,
      xLabel: copy('resultsSequence'), yLabel: label, series}, richChartText);
    tableHost.replaceChildren(chart.querySelector('.rich-data-chart-table')!);plot.replaceChildren(chart);
  }
  return {update(program: ExplorationProgram, page: ExplorationReceipt[]) {
    const next = JSON.stringify([program.program_id, program.actuals, program.actuals_complete, page.map(row => [row.sequence, row.actuals, row.actuals_complete, row.outcome, row.review?.outcome])]);
    if (next === signature) return;
    signature = next;
    if (owner !== program.program_id) {owner = program.program_id;metric = 'tokens';kind = 'bar';table.open = false;}
    receipts = page.filter(row => row.program_id === program.program_id).slice(0, 20);
    const total = program.actuals?.tokens;
    const values = [String(receipts.length), String(receipts.filter(row => row.outcome === 'completed' && row.review?.outcome === 'success').length),
      measured(total) ? `${program.actuals_complete === false ? '≥ ' : ''}${total.toLocaleString()}` : '—'];
    stats.replaceChildren(...['resultsRecords', 'resultsReviewed', 'resultsTotal'].map((label, index) => {
      const cell = node('div');cell.append(node('dt', copy(label)), node('dd', values[index]));return cell;
    }));draw();
  }};
}
