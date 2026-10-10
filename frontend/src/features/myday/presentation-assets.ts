/**
 * Static trusted markup shared by the demand-loaded My Day presenter.
 * Entry point: MYDAY_PRESENTATION_ASSETS. Dependencies: none; this module
 * owns no state, copy, actions, or lifecycle.
 */

export const MYDAY_PRESENTATION_ASSETS = Object.freeze({
  emptyIllustration: `
    <svg class="myday-empty-tofu" width="56" height="56" viewBox="0 0 32 32" fill="none">
      <path d="M15.3 4.6 L6.4 9.6 L16.3 16 L26.2 10.5Z" fill="currentColor" opacity=".12"/>
      <path d="M6.4 9.6 L6.1 21.1 L17.2 27.2 L16.3 16Z" fill="currentColor" opacity=".08"/>
      <path d="M16.3 16 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" fill="currentColor" opacity=".05"/>
      <path d="M15.3 4.6 L6.4 9.6 L6.1 21.1 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" stroke="currentColor" stroke-width=".6" stroke-linejoin="round" fill="none"/>
      <rect x="7.8" y="14.2" width="2.6" height="3.3" rx=".3" fill="currentColor"/>
      <rect x="13.1" y="16.5" width="2.6" height="3.8" rx=".3" fill="currentColor"/>
      <path d="M10.1 20.1 Q12 21.6 13.9 20.1" stroke="currentColor" stroke-width=".5" fill="none" stroke-linecap="round"/>
    </svg>`,
  todoCheckIcon: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L19 7"/></svg>',
  todoDeleteIcon: '<svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"><path d="M1 1l6 6M7 1l-6 6"/></svg>',
  todoLaunchIcon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  unfinishedIcon: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="9" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3" fill="#f59e0b" fill-opacity="0.06"/>
  </svg>`,
});
