/** Diagnostic copy action shared by the main and project coordinator cards.
 * Reads the caller's current binding only; never opens or provisions a chat.
 * Clipboard refusal leaves the exact identifier selectable in a persistent toast.
 */
import { createButton, toast } from '../ui';
import { t } from '../i18n';

export function createCoordinatorConversationId(getConversationId: () => string): HTMLButtonElement {
  const button = createButton({ label: 'ID', size: 'sm', variant: 'ghost',
    title: t('sidebar.copyConvId'), onClick: async event => {
      event.stopPropagation();
      const conversationId = getConversationId();
      if (!button.isConnected || !conversationId || button.disabled) return;
      button.disabled = true;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(conversationId);
        if (button.isConnected && getConversationId() === conversationId)
          toast(t('projectCoordination.referenceCopied'), { variant: 'success' });
      } catch {
        if (button.isConnected && getConversationId() === conversationId)
          toast(conversationId, { title: t('projectCoordination.referenceCopyFailed'),
            variant: 'warning', duration: 0 });
      } finally { button.disabled = false; }
    } });
  button.setAttribute('aria-label', t('sidebar.copyConvId'));
  button.dataset.i18nTitle = 'sidebar.copyConvId';
  button.dataset.i18nAriaLabel = 'sidebar.copyConvId';
  button.dataset.coordinationFocus = 'copy-coordinator-id';
  return button;
}
