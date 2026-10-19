/** Demand-loaded native dialog; its caller closes the mount on conversation changes. */
import { createDialog } from '../../ui';
import { mountWorkingFacts } from './panel';
import type { WorkingFactsPorts, Text } from './ports';
import '../../styles/application/34-working-facts.css';

export function openWorkingFacts(conversationId: string, ports: WorkingFactsPorts, text: Text): () => void {
  const body = document.createElement('div');
  let disposed = false;
  const dispose = mountWorkingFacts(body, conversationId, ports, text);
  const dialog = createDialog({ title: text('workingFacts.title', 'Current basis'), body,
    actions: [{ id: 'close', label: text('workingFacts.close', 'Close') }],
    onClose: () => { if (!disposed) { disposed = true; dispose(); dialog.el.remove(); } },
  });
  dialog.el.dataset.workingFactsDialog = '';
  document.body.append(dialog.el); void dialog.open();
  return () => dialog.close();
}
