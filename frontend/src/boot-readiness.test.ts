import { expect, it } from 'vitest';
import { isBootSendBlocked, setBootSendBlocked, subscribeBootReadiness } from './boot-readiness';
it('keeps overlapping boot and restart owners fenced until both release', () => {
  const values: boolean[] = [];
  const release = subscribeBootReadiness(() => values.push(isBootSendBlocked()));
  setBootSendBlocked('boot', true);
  setBootSendBlocked('restart', true);
  setBootSendBlocked('boot', false);
  expect(isBootSendBlocked()).toBe(true);
  setBootSendBlocked('restart', false);
  expect(values).toEqual([true, false]);
  release();
  setBootSendBlocked('boot', true);
  setBootSendBlocked('boot', false);
  expect(values).toEqual([true, false]);
});
