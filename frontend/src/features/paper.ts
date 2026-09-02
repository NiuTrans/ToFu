import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { getRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';

// The retained Paper renderer still owns a small set of presentation seams.
// Install only that explicit surface before native owners evaluate: several
// owners wire lifecycle hooks at module load and must see these functions then.
const compatibilityTarget = featureRegistry as Window & Record<string, unknown>;
for (const name of [
  '_applyReportEventRaw',
  '_detachReportPush',
  '_handlePaperKeyDown',
  '_loadOrGenerateReport',
  '_paintReportFromState',
  '_persistGeneratedReviewVenue',
  '_populatePaperReportModelDropdown',
  '_populateReviewVenueDropdown',
  '_renderFinalReport',
  '_reportView',
  '_restoreRebuttalPanel',
  '_showPaperLanding',
  '_syncReviewSegState',
  '_teardownReadingTracker',
  '_updatePaperTitles',
] as const) {
  const service = getRuntimeService(name);
  if (typeof service === 'function' && typeof compatibilityTarget[name] !== 'function') {
    compatibilityTarget[name] = service;
  }
}

// Renderer islands load first. The shared media UI then installs its single
// public surface before any native runtime can invoke a renderer.
const nativeReady = (async () => {
  await import('./paper/media-model-ui');
  await Promise.all([
  import('./paper/pdf-responsive'),
  import('./paper/push-transport'),
  import('./paper/reader-prefs'),
  import('./paper/babel'),
  import('./paper/notes'),
  import('./paper/deepen'),
  import('./paper/qa'),
  import('./paper/reading-xp'),
  import('./paper/pdf-viewer'),
  import('./paper/library'),
  import('./paper/lifecycle'),
  import('./paper/arxiv-search'),
  import('./paper/research-view'),
  import('./paper/recommend'),
  import('./paper/arxiv-fetch'),
  import('./paper/podcast-runtime'),
  import('./paper/video-runtime'),
  import('./paper/report-runtime'),
    import('./paper/session'),
    import('./paper/research-session'),
  ]);
})();

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  await nativeReady;
  return invokeFeatureEntry('paper', name, args, stub);
}
