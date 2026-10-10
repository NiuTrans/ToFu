# Environment provider SDK

This optional distribution owns provider contracts, immutable plans, external
operation checkpoints and the Tofu tool adapter. Read README.md first.

The SDK and public providers contain no vendor hosts or credentials. Provider
side effects happen only after authorization and a successful repository CAS.
Never retry an uncertain external write. Every owner/project is explicit.
The host adapter alone imports Tofu application ports. Other modules remain
standalone; repository implementations must preserve CAS and durability.

Verify with pytest packages/tofu-environments/tests, then plugin/storage and
export boundaries. No import may start a worker, daemon, login or network call.
