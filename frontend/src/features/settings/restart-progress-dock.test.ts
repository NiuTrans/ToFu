// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { dockRestartProgress, offerRestartReload, recordRestartDiagnostic } from './restart-progress-dock';

describe('restart progress dock', () => {
  it('leaves the settings modal and keeps diagnostics bounded and copyable', () => {
    document.body.innerHTML = '<div id="updateModal" class="open"><div id="card"></div></div><textarea id="draft">keep me</textarea>';
    const card = document.getElementById('card')!;
    dockRestartProgress(card, 'Debug logs');
    expect(document.getElementById('updateModal')!.classList.contains('open')).toBe(false);
    expect(card.parentElement!.id).toBe('restart-progress-dock');
    for (let index = 0; index < 300; index++) recordRestartDiagnostic(card, `phase ${index} ${'x'.repeat(2100)}`);
    const logs = card.querySelector('pre')!;
    expect(logs.textContent!.split('\n')).toHaveLength(32);
    expect(logs.textContent!.length).toBeLessThan(65000);
    expect(card.querySelectorAll('pre')).toHaveLength(1);
    const textNode = logs.firstChild;
    recordRestartDiagnostic(card, `phase 299 ${'x'.repeat(2100)}`);
    expect(logs.firstChild).toBe(textNode);
    card.querySelector('button')!.click();
    expect(logs.hidden).toBe(false);
    offerRestartReload(card, 'Reload', 'Dismiss');
    expect((document.getElementById('draft') as HTMLTextAreaElement).value).toBe('keep me');
    const buttons = card.querySelectorAll('button');
    buttons[buttons.length - 1].click();
    expect(document.getElementById('restart-progress-dock')).toBeNull();
  });
});
