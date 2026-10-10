/** Retryable, lazy PDF.js/worker service shared by independent reader instances. */
import { featureRegistry } from '../../feature-registry';

export type PdfJsModule = typeof import('pdfjs-dist/legacy/build/pdf.mjs');
let loaded: PdfJsModule | undefined;
let loading: Promise<PdfJsModule> | undefined;

export function loadedPdfJs(): PdfJsModule | undefined {
  return loaded ?? (featureRegistry as unknown as { pdfjsLib?: PdfJsModule }).pdfjsLib;
}

export function ensurePdfJs(): Promise<PdfJsModule> {
  const available = loadedPdfJs();
  if (available) return Promise.resolve(available);
  if (loading) return loading;
  loading = Promise.all([
    import('pdfjs-dist/legacy/build/pdf.mjs'),
    import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'),
  ]).then(([module, worker]) => {
    module.GlobalWorkerOptions.workerSrc = worker.default;
    loaded = module;
    // Existing Paper islands use the private service registry; no window state.
    (featureRegistry as unknown as { pdfjsLib?: PdfJsModule }).pdfjsLib = module;
    return module;
  }).catch((error: unknown) => {
    loading = undefined;
    throw error;
  });
  return loading;
}
