/** Notify diagnostics that optional background owners are available. */
export async function preload(): Promise<void> {
  document.dispatchEvent(new CustomEvent('tofu:feature-domain-loaded', {
    detail: { domain: 'background' },
  }));
}
