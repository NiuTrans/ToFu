/** Demand-loaded ordinary Turn review using the shared owner-review presenter. */
import { createDialog } from '../../ui';
import { mountTurnReview, type TurnReviewPorts } from '../../rich-response/artifact-review';

export function openTurnReviewDialog(ports: TurnReviewPorts, onClosed: () => void): () => void {
  const body = document.createElement('div');
  let disposed = false;
  const dispose = mountTurnReview(body, ports);
  const dialog = createDialog({ title: ports.text('turnReview.title', 'Task acceptance'), body,
    actions: [{ id: 'close', label: ports.text('workingFacts.close', 'Close') }],
    onClose: () => { if (!disposed) { disposed = true; dispose(); dialog.el.remove(); onClosed(); } },
  });
  dialog.el.dataset.turnReviewDialog = '';
  document.body.append(dialog.el); void dialog.open();
  return () => dialog.close();
}
