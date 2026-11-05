/** Geometric residency planning for windowed Turns and blocks. */
import { describe, expect, it } from 'vitest';
import {
  CONVERSATION_HISTORY_HYSTERESIS_VIEWPORTS,
  CONVERSATION_HISTORY_PREFETCH_VIEWPORTS,
  CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS,
  CONVERSATION_HISTORY_RETAIN_VIEWPORTS,
  olderHistoryDue,
  planResidentRange,
  type ResidentRange,
} from './surface-windowing';

const HEIGHT = 600;
const PREFETCH = HEIGHT * CONVERSATION_HISTORY_PREFETCH_VIEWPORTS;
const BAND = HEIGHT * CONVERSATION_HISTORY_HYSTERESIS_VIEWPORTS;
const RETAIN = HEIGHT * CONVERSATION_HISTORY_RETAIN_VIEWPORTS;
const limits = { viewportHeight: HEIGHT, cap: 80, batch: 20, edgeLimit: 240 };

/** Items of `size` px; mounted offset k starts at `firstTop + k * size`. */
const uniform = (range: ResidentRange, firstTop: number, size = 100) => (offset: number) => {
  if (offset < 0 || offset >= range.end - range.start) return null;
  const top = firstTop + offset * size;
  return { top, bottom: top + size };
};

describe('planResidentRange', () => {
  it('mounts earlier history while prefetch distance remains, before the edge is reached', () => {
    const range = { start: 40, end: 60 };
    // 500px mounted above the viewport: refill to prefetch + band (1500px).
    const plan = planResidentRange(range, 100, 'earlier', uniform(range, -500), limits);
    expect(plan.nearEdge).toBe(true);
    expect(plan.range).toEqual({ start: 30, end: 60 });
  });

  it('sizes each refill from the mounted items and bounds it per frame', () => {
    const range = { start: 40, end: 60 };
    const tall = planResidentRange(range, 100, 'earlier', uniform(range, -100, 500), limits);
    // ceil((1500 - 100) / 500) items refill; items starting more than three
    // viewports below the viewport retire because content extends past the band.
    expect(tall.range).toEqual({ start: 37, end: 46 });
    const short = planResidentRange(range, 100, 'earlier', uniform(range, -100, 20),
      { ...limits, batch: 8 });
    expect(short.range).toEqual({ start: 32, end: 60 });
  });

  it('leaves a satisfied range untouched', () => {
    const range = { start: 40, end: 60 };
    const plan = planResidentRange(range, 100, 'earlier', uniform(range, -PREFETCH), limits);
    expect(plan).toEqual({ range: null, nearEdge: false });
  });

  it('retires only trailing items beyond the retention distance, after the band', () => {
    const range = { start: 0, end: 60 };
    // Item k starts at -1000 + 100k; the last starts at 4900px, past the band.
    const plan = planResidentRange(range, 60, 'earlier', uniform(range, -1000), limits);
    const firstRetired = Math.floor((HEIGHT + RETAIN + 1000) / 100) + 1;
    expect(plan.range).toEqual({ start: 0, end: firstRetired });
    // Content ending inside the band is kept: nothing commits per Turn.
    const settled = { start: 0, end: 40 };
    expect(HEIGHT + RETAIN + BAND).toBeGreaterThan(-1000 + 39 * 100);
    expect(planResidentRange(settled, 40, 'earlier', uniform(settled, -1000), limits))
      .toEqual({ range: null, nearEdge: false });
  });

  it('mirrors growth and retirement toward later content', () => {
    const range = { start: 0, end: 40 };
    // Item k spans [-3100 + 100k, -3000 + 100k]: the last ends 300px below.
    const plan = planResidentRange(range, 100, 'later', uniform(range, -3100), limits);
    expect(plan.nearEdge).toBe(true);
    // ceil((1500 - 300) / 100) items mount; items ending more than three
    // viewports (1800px) above the viewport top retire: k < 12.
    expect(plan.range).toEqual({ start: 12, end: 52 });
  });

  it('never retires visible content to honour the cap and grows only into free capacity', () => {
    const range = { start: 10, end: 20 };
    const plan = planResidentRange(range, 100, 'earlier', uniform(range, -100),
      { ...limits, cap: 12 });
    // No trailing item lies beyond the prefetch distance, so only the free
    // capacity of two items may grow.
    expect(plan.range).toEqual({ start: 8, end: 20 });
    const full = planResidentRange(range, 100, 'earlier', uniform(range, -100),
      { ...limits, cap: 10 });
    expect(full).toEqual({ range: null, nearEdge: true });
  });

  it('ignores a lane edge low in the viewport, where mounting would move visible content', () => {
    const range = { start: 5, end: 10 };
    const plan = planResidentRange(range, 20, 'earlier', uniform(range, 300), limits);
    expect(plan).toEqual({ range: null, nearEdge: false });
  });

  it('measures past items without a box and is inert without viewport geometry', () => {
    const range = { start: 10, end: 20 };
    const sparse = (offset: number) => (offset < 2 ? null
      : { top: -300 + offset * 100, bottom: -200 + offset * 100 });
    expect(planResidentRange(range, 30, 'earlier', sparse, limits).range)
      .toEqual({ start: 0, end: 20 });
    expect(planResidentRange(range, 30, 'earlier', uniform(range, -100),
      { ...limits, viewportHeight: 0 })).toEqual({ range: null, nearEdge: false });
    expect(planResidentRange(range, 30, 'earlier', () => null, limits))
      .toEqual({ range: null, nearEdge: false });
  });

  it('reports the loaded edge so remote history can be requested ahead of the reader', () => {
    const range = { start: 0, end: 10 };
    const plan = planResidentRange(range, 10, 'earlier', uniform(range, -200), limits);
    expect(plan).toEqual({ range: null, nearEdge: true });
  });
});

describe('olderHistoryDue', () => {
  const READ_AHEAD = HEIGHT * CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS;
  const geometry = { viewportHeight: HEIGHT, unmountedTurns: 0, averageTurnHeight: 400 };

  it('requests the next page while the reader is still several viewports below the loaded top', () => {
    // Far beyond the residency prefetch distance, but inside the read-ahead.
    expect(PREFETCH * 2).toBeLessThan(READ_AHEAD);
    expect(olderHistoryDue({ ...geometry, mountedTop: -PREFETCH * 2 })).toBe(true);
    expect(olderHistoryDue({ ...geometry, mountedTop: 120 })).toBe(true);
    expect(olderHistoryDue({ ...geometry, mountedTop: -READ_AHEAD - 1 })).toBe(false);
  });

  it('counts loaded Turns that are not mounted yet at the measured mean height', () => {
    const top = -READ_AHEAD / 2;
    expect(olderHistoryDue({ ...geometry, mountedTop: top, unmountedTurns: 3 })).toBe(true);
    // Enough loaded history remains to read without a round trip.
    expect(olderHistoryDue({ ...geometry, mountedTop: top,
      unmountedTurns: Math.ceil(READ_AHEAD / 2 / 400) })).toBe(false);
  });

  it('extends the distance by the travel the next frame may cover', () => {
    const top = -READ_AHEAD - 100;
    expect(olderHistoryDue({ ...geometry, mountedTop: top })).toBe(false);
    expect(olderHistoryDue({ ...geometry, mountedTop: top, lookahead: 200 })).toBe(true);
  });

  it('narrows to a caller-supplied distance', () => {
    const top = -PREFETCH - 100;
    expect(olderHistoryDue({ ...geometry, mountedTop: top })).toBe(true);
    expect(olderHistoryDue({ ...geometry, mountedTop: top,
      viewports: CONVERSATION_HISTORY_PREFETCH_VIEWPORTS })).toBe(false);
  });

  it('is inert without viewport geometry', () => {
    expect(olderHistoryDue({ ...geometry, viewportHeight: 0, mountedTop: 0 })).toBe(false);
    expect(olderHistoryDue({ ...geometry, mountedTop: Number.NaN })).toBe(false);
  });
});
