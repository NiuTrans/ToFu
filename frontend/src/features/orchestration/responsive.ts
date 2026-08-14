import { orchestrationRegistry } from './registry';
export const ORCHESTRATION_LAYOUT_BREAKPOINTS = Object.freeze({
  sheetMax: 800,
  landscapeSheetMax: 1000,
  landscapeSheetMaxHeight: 500,
  landscapeFitMinScale: 0.55,
  taskCompactMax: 900,
  compactMax: 1100,
});
export const orchestrationShortLandscapeMediaQuery = (): string =>
  `(max-width:${ORCHESTRATION_LAYOUT_BREAKPOINTS.landscapeSheetMax}px) and `
  + `(max-height:${ORCHESTRATION_LAYOUT_BREAKPOINTS.landscapeSheetMaxHeight}px) `
  + 'and (pointer:coarse)';
export const orchestrationSheetMediaQuery = (): string =>
  `(max-width:${ORCHESTRATION_LAYOUT_BREAKPOINTS.sheetMax}px), `
  + orchestrationShortLandscapeMediaQuery();
const media = (view: Window | null | undefined, query: string): MediaQueryList | null => {
  const target = view ?? window;
  return target && typeof target.matchMedia === 'function'
    ? target.matchMedia(query) : null;
};
export const orchestrationSheetMedia = (view?: Window | null) =>
  media(view, orchestrationSheetMediaQuery());
export const orchestrationShortLandscapeMedia = (view?: Window | null) =>
  media(view, orchestrationShortLandscapeMediaQuery());
export const orchestrationFitMinScale = (view?: Window | null): number | null =>
  orchestrationShortLandscapeMedia(view)?.matches
    ? ORCHESTRATION_LAYOUT_BREAKPOINTS.landscapeFitMinScale : null;
export const orchestrationCompactMedia = (view?: Window | null) =>
  media(view, `(max-width:${ORCHESTRATION_LAYOUT_BREAKPOINTS.compactMax}px)`);
export const taskModeCompactMedia = (view?: Window | null) =>
  media(view, `(max-width:${ORCHESTRATION_LAYOUT_BREAKPOINTS.taskCompactMax}px)`);

Object.assign(orchestrationRegistry, {
  ORCHESTRATION_LAYOUT_BREAKPOINTS,
  orchestrationSheetMediaQuery,
  orchestrationSheetMedia,
  orchestrationShortLandscapeMediaQuery,
  orchestrationShortLandscapeMedia,
  orchestrationFitMinScale,
  orchestrationCompactMedia,
  taskModeCompactMedia,
});
