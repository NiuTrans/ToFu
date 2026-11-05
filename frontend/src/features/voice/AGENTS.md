# Voice input ownership

`../voice-input.ts` owns composer registration, projection and app disposal.
`audio-capabilities.ts` shares backend capability reads with Speech Settings;
model routing commits/push/reconnect only invalidate that read authority.
`recording.ts` and `wave-encoder.ts` load on the first microphone action and
own one recording's resources. Keep media construction out of module evaluation.

Behavior and alternatives: `docs/FRONTEND_ARCHITECTURE.md`, “Composer voice
capabilities and media lifetime”. The retained `sections/voice.js` is a binding
bridge; delete it with retained boot/action composition, never grow behavior there.

Verify `tests/voice_input.test.cjs`, `tests/frontend/test_frontend_voice_input.py`,
the neighboring Speech Settings lifecycle tests, and the real speech browser
journeys. Regenerate runtime/actions through their canonical scripts after source
checks; never hand-edit generated runtime output.
