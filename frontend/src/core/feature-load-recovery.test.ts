/** Asset-shaped recovery stays bounded; module evaluation errors remain actionable. */
import { describe, expect, it } from 'vitest';
import { createFeatureLoadRecovery, installFeatureLoadRecovery, recoverFeatureLoad, FEATURE_LOAD_RECOVERY_POLICY } from './feature-load-recovery';

function controller() {
  const values = new Map<string, string>();
  let reloads = 0;
  const recovery = createFeatureLoadRecovery({
    now: () => 100_000, readValue: key => values.get(key) ?? null,
    writeValue: (key, value) => { values.set(key, value); },
    removeValue: key => { values.delete(key); }, reload: () => { reloads += 1; },
  });
  return { recovery, values, reloads: () => reloads };
}

describe('lazy graph asset recovery', () => {
  it('nested pages use the installed shell authority and share its reload bound', () => {
    const { recovery, reloads } = controller();
    const uninstall = installFeatureLoadRecovery(recovery);
    try {
      const error = new Error('Unable to preload CSS for /assets/studio.css');
      expect(recoverFeatureLoad('openEvolution', error)).toBe(true);
      expect(recovery.attemptRecovery('openSettings', error)).toBe(false);
      expect(reloads()).toBe(1);
      expect(recovery.consumePendingFeature()).toBe('openEvolution');
    } finally { uninstall(); }
    expect(recoverFeatureLoad('openEvolution', new Error('Chunk failure'))).toBe(false);
  });

  it('a superseded bootstrap disposer cannot clear a newer authority', () => {
    const first = controller(), second = controller();
    const removeFirst = installFeatureLoadRecovery(first.recovery);
    const removeSecond = installFeatureLoadRecovery(second.recovery);
    try {
      removeFirst();
      expect(recoverFeatureLoad('openEvolution', new TypeError('Failed to fetch dynamically imported module'))).toBe(true);
      expect(first.reloads()).toBe(0); expect(second.reloads()).toBe(1);
    } finally { removeSecond(); }
  });
  it('recognizes Vite CSS preload failures and browser module load failures', () => {
    const { recovery } = controller();
    expect(recovery.isModuleLoadFailure(new Error('Unable to preload CSS for /assets/settings.css'))).toBe(true);
    expect(recovery.isModuleLoadFailure(new TypeError('Failed to fetch dynamically imported module'))).toBe(true);
    expect(recovery.isModuleLoadFailure(new TypeError('Importing a module script failed.'))).toBe(true);
    for (const error of [new ReferenceError('Unknown feature binding'), new Error('Feature API failed'),
      new TypeError('Cannot read properties of undefined'), { message: 'Unable to preload CSS for /assets/settings.css' }]) {
      expect(recovery.isModuleLoadFailure(error)).toBe(false);
    }
  });

  it('reloads a CSS failure once and consumes the exact pending intent once', () => {
    const { recovery, reloads, values } = controller();
    const error = new Error('Unable to preload CSS for /assets/settings.css');
    expect(recovery.attemptRecovery('openSettings', error)).toBe(true);
    expect(values.get(FEATURE_LOAD_RECOVERY_POLICY.pendingFeatureKey)).toBe('openSettings');
    expect(recovery.attemptRecovery('openSettings', error)).toBe(false);
    expect(reloads()).toBe(1);
    expect(recovery.consumePendingFeature()).toBe('openSettings');
    expect(recovery.consumePendingFeature()).toBeNull();
    expect(recovery.attemptRecovery('openSettings', new ReferenceError('Broken view'))).toBe(false);
    expect(reloads()).toBe(1);
  });

  it('does not reload when session storage cannot preserve the guard', () => {
    let reloads = 0;
    const recovery = createFeatureLoadRecovery({
      now: () => 100_000, readValue: () => null, writeValue: () => { throw new Error('Storage disabled'); },
      removeValue: () => {}, reload: () => { reloads += 1; },
    });
    expect(recovery.attemptRecovery('openSettings', new Error('Unable to preload CSS for /assets/settings.css'))).toBe(false);
    expect(reloads).toBe(0);
  });
});
