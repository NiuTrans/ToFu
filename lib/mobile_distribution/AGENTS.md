# Mobile distribution owner

## Scope and authority

This package owns credential-free persisted/shared server-address admission and
local mobile download metadata. `__init__.py` keeps the public
`lib.mobile_distribution` import surface. It depends only on the standard library
and never imports HTTP routes, starts a worker, contacts release services or
writes user state.

`android/published-release.json` is the reviewed artifact authority; source
versions never promote or relabel that receipt. The read-only receipt path must
resolve correctly in both a source tree and the frozen desktop bundle. Download
configuration belongs in `contracts/config_knobs_v1.json`. Behavioral policy and
consumers are mapped by `docs/contracts/API_CONTRACT.md` → Native mobile setup
and publication, and `android/README.md` → Distribution.

## Verification

Run the mobile credential/link, health publication and frozen-receipt tests:
`tests/api/test_mobile_connect_security.py`,
`tests/server/test_mobile_release_metadata.py` and
`tests/mobile/test_mobile_distribution_frozen.py`. Then run the config inventory
and architecture checks. Transient native SSO navigation is a separate policy;
this package must never weaken saved/QR URL admission to accept it.
