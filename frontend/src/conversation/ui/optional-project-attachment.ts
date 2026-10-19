/**
 * Paint legacy shell attachment affordances as optional project actions.
 * Delete this adapter when the shell template directly exposes project actions.
 * No model, execution strategy, Flow, or saved project authority is changed.
 */
export function paintOptionalProjectAttachment(
  document: Document,
  translate: (key: string) => string,
): void {
  const labelKey = 'conversationBar.attachProject';
  const label = document.getElementById('chatModeLabel');
  if (label) {
    label.dataset.i18n = labelKey;
    label.textContent = translate(labelKey);
  }
  const trigger = document.getElementById('chatModeToggle');
  if (trigger) {
    trigger.setAttribute('aria-label', translate(labelKey));

    trigger.dataset.i18nTitle = labelKey;
    trigger.title = translate(labelKey);
    trigger.removeAttribute('aria-haspopup');
    trigger.removeAttribute('aria-expanded');
    trigger.removeAttribute('data-mode');
    trigger.querySelector('.submenu-arrow')?.remove();
  }
  document.getElementById('chatModeMenu')?.remove();
  const mobileItems = document.querySelectorAll<HTMLElement>('.mobile-mode-item');
  mobileItems.forEach(item => {
    if (item.dataset.mode !== 'studio') {
      item.remove();
      return;
    }
    item.classList.remove('active');

    item.classList.remove('mobile-mode-item');
    item.querySelector('.mobile-sheet-item-check')?.remove();
    const heading = item.parentElement?.querySelector<HTMLElement>('.mobile-sheet-section-title');
    if (heading) {
      heading.dataset.i18n = labelKey;
      heading.textContent = translate(labelKey);
    }
    item.removeAttribute('role');
    item.removeAttribute('aria-checked');
    item.removeAttribute('data-mode');
    const title = item.querySelector<HTMLElement>('.mobile-sheet-item-name');
    if (title) {
      title.dataset.i18n = labelKey;
      title.textContent = translate(labelKey);
    }
    const description = item.querySelector<HTMLElement>('[data-i18n="mobile.modeStudioDesc"]');
    if (description) {
      description.dataset.i18n = 'mobile.projectAssistantDesc';
      description.textContent = translate('mobile.projectAssistantDesc');
    }
  });
}
