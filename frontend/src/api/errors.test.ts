/** Transient means an identical repeat can outlive the failure; every definitive rejection stays visible. */
import { describe, expect, it } from 'vitest';
import { isTransientApiFailure } from './errors';

const failure = (fields: Record<string, unknown>) => Object.assign(new Error('failure'), fields);

describe('isTransientApiFailure', () => {
  it.each([
    ['a transport network failure', { status: 0, code: 'network' }],
    ['a transport timeout', { status: 0, code: 'timeout' }],
    ['408', { status: 408 }],
    ['429', { status: 429 }],
    ['502', { status: 502 }],
    ['503', { status: 503 }],
    ['504', { status: 504 }],
    ['a 500 no backend request owns', { status: 500 }],
    ['a retryable storage code', { status: 409, body: { error: { storageCode: 'database_busy' } } }],
  ])('repeats %s', (_name, fields) => {
    expect(isTransientApiFailure(failure(fields))).toBe(true);
  });

  it.each([
    ['a backend-attributed 500', { status: 500, serverRequestId: 'server-rid' }],
    ['a 500 carrying a backend error body', { status: 500, body: { error: { message: 'Internal server error' } } }],
    ['a validation rejection', { status: 400 }],
    ['an authentication rejection', { status: 401 }],
    ['a busy lane', { status: 409, body: { error: { code: 'lane_busy' } } }],
    ['an aborted transport request', { status: 0, code: 'aborted' }],
    ['an unclassified error', {}],
  ])('surfaces %s', (_name, fields) => {
    expect(isTransientApiFailure(failure(fields))).toBe(false);
  });

  it('never repeats a cancellation', () => {
    expect(isTransientApiFailure(new DOMException('stopped', 'AbortError'))).toBe(false);
  });
});
