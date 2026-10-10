/** Conversation model identity: draft-safe restore fencing and explicit commits. */
export function createConversationModelController(ports: {
  hasDraft(): boolean; selection(): unknown;
}) {
  let generation = 0;
  return {
    begin() {
      const token = ++generation;
      const selection = JSON.stringify(ports.selection());
      const blocked = ports.hasDraft();
      return () => token === generation && !blocked && !ports.hasDraft()
        && selection === JSON.stringify(ports.selection());
    },
  };
}
export function captureComposerModel(config: any) {
  return {
    model: config._modelIsProvisional ? undefined : config.model,
    modelRef: config.modelRef ? { ...config.modelRef } : null,
    preferredProviderId: config.preferredProviderId || '',
    thinkingDepth: config.thinkingDepth,
  };
}
export function commitComposerModel(conv: any, selection: ReturnType<typeof captureComposerModel>) {
  if (!conv) return;
  if (selection.model) Object.assign(conv, selection);
}
