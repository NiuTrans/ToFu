/* ===== migrated source: voice.js ===== */
/* Voice boot/action bridge. Behavior and disposal live in features/voice-input.
 * Delete this section when retained boot/action composition migrates. */
runtimeScope.initVoiceInput = initVoiceInput;
runtimeScope.toggleVoiceInput = toggleVoiceInput;
retainedCompositionLifecycle.add(destroyVoiceInput);
