// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import {
  renderReportFinishTag, reportCostBreakdown, syncReportFinishTag,
  type ReportMetadataPresentation,
} from './metadata';

const labels: Record<string, string> = {
  'paper.xpCostBody': 'Report', 'paper.xpPassInsight': 'Insight',
  'paper.xpPassTermfill': 'Glossary', 'paper.xpPassCheckpoints': 'Checkpoints',
  'paper.xpPassDeepen': 'Deepen',
};
const ports: ReportMetadataPresentation = {
  translate: key => labels[key] || key,
  formatCny: value => `¥${value.toFixed(4)}`,
};
function articleFixture() {
  document.body.innerHTML = '<div id="report"><article class="paper-report-article">'
    + '<h2>Evidence</h2><details open><summary>Tools</summary>Result</details>'
    + '<input value="draft"></article></div><div id="other"></div>';
  return { document, container: document.getElementById('report')!,
    article: document.querySelector('article')! };
}
afterEach(() => { document.body.replaceChildren(); });

describe('recorded report metadata presentation', () => {
  it('renders recorded totals, per-pass costs and compact tokens without repricing', () => {
    const { container } = articleFixture();
    const meta = {
      model: 'model-a', costCny: 0.01, totalCostCny: 0.015,
      promptTokens: 1000, completionTokens: 200,
      secondPasses: { insight: { costCny: 0.005 } },
    };
    syncReportFinishTag(container, meta, ports);
    expect(container.querySelector('.paper-finish-cost')?.textContent).toBe('¥0.0150');
    expect(container.querySelector('.paper-finish-cost')?.getAttribute('title'))
      .toBe('Report ¥0.0100 + Insight ¥0.0050');
    expect(container.querySelector('.paper-finish-tokens')?.textContent).toBe('1.0k → 200 tok');
    expect(reportCostBreakdown({ model: 'model-a' }, ports)).toBe('');
  });

  it('preserves CNY preference, USD fallback and missing-value behavior', () => {
    const { container } = articleFixture();
    syncReportFinishTag(container, { model: 'm', costCny: 2, costUsd: 1 }, ports);
    expect(container.querySelector('.paper-finish-cost')?.textContent).toBe('¥2.0000');
    syncReportFinishTag(container, { model: 'm', costUsd: 0.25, promptTokens: 2000000 }, ports);
    expect(container.querySelector('.paper-finish-cost')?.textContent).toBe('$0.2500');
    expect(container.querySelector('.paper-finish-tokens')?.textContent).toBe('2.0M → 0 tok');
    syncReportFinishTag(container, { model: 'm', costCny: 0 }, ports);
    expect(container.querySelector('.paper-finish-cost')).toBeNull();
    expect(renderReportFinishTag(null, ports)).toBe('');
    expect(renderReportFinishTag({}, ports)).toBe('');
    expect(renderReportFinishTag({ model: 'm', costCny: 0.1 }, { translate: ports.translate }))
      .toContain('¥0.1000');
  });

  it('escapes provider model names, translated labels and unknown pass names', () => {
    const { container } = articleFixture();
    const model = '<img src=x onerror=alert(1)>';
    syncReportFinishTag(container, {
      model, costCny: 1, totalCostCny: 2,
      secondPasses: { '"><script>alert(2)</script>': { costCny: 1 } },
    }, { ...ports, translate: key => `<b>${key}</b>` });
    expect(container.querySelector('.paper-finish-model')?.textContent).toBe(model);
    expect(container.querySelector('script, img, b')).toBeNull();
    expect(container.querySelector('.paper-finish-cost')?.getAttribute('title'))
      .toContain('"><script>alert(2)</script>');
  });

  it('keeps unchanged replay nodes and reconciles changes without rebuilding the article', () => {
    const { container, article } = articleFixture();
    const details = article.querySelector('details');
    const input = article.querySelector('input')!;
    input.value = 'unsaved user edit';
    const meta = { model: 'm', costCny: 0.01 };
    expect(syncReportFinishTag(container, meta, ports)).toBe(true);
    const tag = article.querySelector('.paper-report-finish-tag');
    for (let repeat = 0; repeat < 30; repeat++) {
      expect(syncReportFinishTag(container, { ...meta }, ports)).toBe(false);
      expect(article.querySelector('.paper-report-finish-tag')).toBe(tag);
    }
    expect(syncReportFinishTag(container, { ...meta, costCny: 0.02 }, ports)).toBe(true);
    expect(container.querySelector('article')).toBe(article);
    expect(article.querySelector('details')).toBe(details);
    expect(details?.open).toBe(true);
    expect(input.value).toBe('unsaved user edit');
    expect(article.querySelector('.paper-finish-cost')?.textContent).toBe('¥0.0200');
    expect(syncReportFinishTag(container, null, ports)).toBe(true);
    expect(article.querySelector('.paper-report-finish-tag')).toBeNull();
    expect(syncReportFinishTag(container, { model: 'later' }, ports)).toBe(true);
    expect(article.querySelectorAll('.paper-report-finish-tag')).toHaveLength(1);
  });

  it('uses only its supplied article and re-evaluates locale and currency presentation', () => {
    const { document, container, article } = articleFixture();
    const other = document.getElementById('other')!;
    expect(syncReportFinishTag(other, { model: 'm' }, ports)).toBe(false);
    const meta = { model: 'm', costCny: 1 };
    syncReportFinishTag(article, meta, ports);
    syncReportFinishTag(container, meta, {
      translate: key => `新语言 ${key}`, formatCny: value => `新币值 ${value}`,
    });
    expect(article.querySelector('.paper-finish-label')?.textContent).toContain('新语言');
    expect(article.querySelector('.paper-finish-cost')?.textContent).toBe('新币值 1');
    expect(other.childNodes).toHaveLength(0);
  });
});
