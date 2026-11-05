/** Present catalog availability from one settled load outcome.
 * Every load path, including background revalidation, clears the warning after
 * success. A failed refresh keeps an existing warning; it never claims that a
 * later background load is still reconnecting after its bounded retry ended.
 */

export interface ConversationCatalogAvailabilityPorts {
  show(): void;
  clear(): void;
}

export function createConversationCatalogAvailability(
  ports: ConversationCatalogAvailabilityPorts,
) {
  let retrying = false;
  let visible = false;
  return Object.freeze({
    retryStarted(): void {
      retrying = true;
      if (visible) return;
      visible = true;
      ports.show();
    },
    settled(succeeded: boolean): void {
      if (succeeded) {
        retrying = false;
        visible = false;
        ports.clear();
      } else if (retrying && !visible) {
        visible = true;
        ports.show();
      }
    },
  });
}
