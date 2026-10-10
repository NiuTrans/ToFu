/** Lazy chat paper preview; lifecycle comes from canonical conversation state. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry } from '../feature-registry';
import { createPaperPreview } from './paper/preview-controller';
const preview = createPaperPreview();
Object.assign(featureRegistry, {
  _paperPreviewReceipts: preview.observeChanges,

  _paperPreviewDiscover: preview.discover,
  _paperPreviewOpen: preview.openDocument,
  _paperPreviewActivate: preview.activateConversation,
});
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('paper-preview', name, args, stub);
}
