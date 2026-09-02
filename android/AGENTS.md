# Android client guidance

## Scope and first reads

`android/` is a thin native WebView shell for Tofu. Read `README.md` and
`android/docs/SUPERVISOR_DESIGN.md` before editing. The browser application remains the
UI authority; Android owns only native profile, credential, cookie, WebView,
and remote-supervisor integration.

## Editing rules

- Do not reimplement SPA screens or server business rules in Kotlin.
- Keep secrets keyed by stable profile identity and stored through the vault;
  never log credentials or persist them in Room, URLs, saved state, or fixtures.
- Preserve URL-change cookie invalidation, explicit cookie flushing, bounded
  reauthentication, and the distinction between gateway and Tofu failures.
- Database changes require an explicit Room migration and migration test.
- `app/src/main/java/com/tofu/client/api/ApiV4Generated.kt` is generated from
  `contracts/api_v4.yaml`; update the contract and generator instead of editing
  the Kotlin file directly.
- Release asset names, tag filters, signing behavior, and the backend download
  URL form one tested contract. Update them together.

## Verification

Use `./test-local.sh` for the fast JDK tiers, then `./gradlew test` when an
Android SDK is available. Run `./gradlew :app:assembleDebug` for packaging
changes and the focused root tests for API generation, APK URL, and workflow
parity.
