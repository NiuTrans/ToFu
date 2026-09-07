import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { getRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';
import '../runtime/paper-reader-presenters.generated.js';
import '../runtime/paper-media-presenters.generated.js';

// The retained Paper renderer still owns a small set of presentation seams.
// Install only that explicit surface before native owners evaluate: several
// owners wire lifecycle hooks at module load and must see these functions then.
const compatibilityTarget = featureRegistry as Window & Record<string, unknown>;
for (const name of [
  '_applyReportEventRaw',
  '_applyResolvedTitle',
  '_attachReportPush',
  '_detachReportPush',
  '_ensurePaperText',
  '_handlePaperKeyDown',
  '_loadOrGenerateReport',
  '_paintReportFromState',
  '_persistGeneratedReviewVenue',
  '_populatePaperReportModelDropdown',
  '_populateReviewVenueDropdown',
  '_renderFinalReport',
  '_renderReportSkeleton',
  '_reportView',
  '_resolveReviewVenue',
  '_restoreRebuttalPanel',
  '_showPaperLanding',
  '_syncReviewSegState',
  '_syncReportToolbar',
  '_teardownReadingTracker',
  '_updatePaperTitles',
] as const) {
  const service = getRuntimeService(name);
  if (typeof service === 'function' && typeof compatibilityTarget[name] !== 'function') {
    compatibilityTarget[name] = service;
  }
}

// The manifest-owned media presenter imports its model UI and task owners
// statically, so those ports are ready before the remaining Paper domains.
const nativeReady = import('./paper/panel-owners');

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  await nativeReady;
  return invokeFeatureEntry('paper', name, args, stub);
}
