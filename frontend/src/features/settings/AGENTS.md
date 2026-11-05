# Settings feature map

- `core-panel.ts` owns panel assembly and publishes the retained Settings
  bindings. `panel-read-session.ts` owns each opening's read cancellation and
  initial readiness; close retires reads while accepted writes keep settling.
  `tab-activation.ts` binds independent panels to their first visit and cleanup
  to that opening; Database and Knowledge refresh on each visit. `knowledge-tab.ts`
  demand-loads the existing Knowledge Workbench and retires its polling when
  the tab or Settings closes. Core Save configuration
  continues loading when Settings opens.
  `server-config-reader.ts` coalesces same-opening config reads and guards cache
  publication; export uses its raw reader without mutating the editor.
  `client-downloads.ts` owns desktop/mobile release-card projection.
  `model-routing-authority.ts` owns routing reads, draft/CAS state and
  serialized persistence through those existing live bindings.
  Credential replacements use the same queue and settle each acknowledged key
  before refreshing the complete authority; Save never clears the whole draft.
- `save-export.ts` coordinates the Settings save. `save-input.ts` captures
  detached server payloads and feature choices before awaited writes;
  `save-feedback.ts` owns the common save latch and original-view feedback.
  Speech captures its form/scope before preceding feature writes. Keep accepted
  writes settling; never close, clear secrets or paint errors into a newer view.
  Composer publication goes to
  `../model-catalog/model-picker-startup.ts`; do not add another catalog or
  reasoning-control state owner in Settings.
- `config-import.ts` shares the Save latch and feedback. File reads retire with
  the opening; accepted imports settle and refresh only their unchanged editor.
- `mcp-settings-reads.ts` owns catalog/update/tool reads and visible breaker
  timers. Retained MCP markup supplies projection ports. Ordinary reads retire
  on close; accepted commands refresh the common tool summary after close and
  paint only their captured current opening. Preserve command and error policy.
  Start with `test_settings_mcp_reads.py` and `test_settings_tab_activation.py`,
  then the retained MCP contracts and `test_settings_tab_activation_browser.py`.
- `mcp-commands.ts` captures MCP command inputs and owns pending/acknowledgement
  flow; `mcp-command-view.ts` owns disposable install/custom-server dialogs and
  feedback. Accepted commands settle after close; confirmation and UI callbacks
  require their original view. Existing install observation policy is unchanged.
  `mcp-tool-selection.ts` orders full replacements per server and rebases later
  optimistic intents on acknowledged policy; projection failures do not stall
  accepted edits. Start with `test_settings_mcp_commands.py`, then the MCP read,
  optimistic/error and real-browser command suites.
- `oauth-panel-lifetime.ts` owns coalesced subscription status reads and existing
  status/device/popup timers within `panel-read-session.ts`. Retained OAuth
  commands capture this opening before awaiting. Accepted auth writes survive
  close; committed routing refreshes preserve current drafts and credentials.
  `codex-shared-login.ts` captures separate commit and presentation callbacks.
- `speech-provider.ts` owns pure STT projection, activation and bundle policy;
  `speech.ts` owns form intent and view lifetime. Save commands enter the
  shared routing queue with explicit draft-persistence policy; no private
  reload assigns editor state. Form changes and plaintext use the same panel
  lifetime as status presentation. `core-panel.ts` starts the speech load
  lifetime before exposing controls; only its current completion enables them.
  Activation preserves the existing access graph and uses the common complete
  response adopter; it is not Provider deletion or a global microphone switch.
- `catpaw-connection.ts` owns Catpaw connection intent, coalesced reads/commands
  and bounded login polling. `catpaw-settings.ts` adapts the existing controls
  and API facade; panel close retires read/login/confirmation callbacks while
  accepted routing writes settle. Initial local-import authorization remains
  in the backend policy; do not add a browser credential or policy store.
- `provider-settings-projection.ts` owns render-scoped Provider card, supply
  matrix and picker reads. Keep source row identity and indices; create a fresh
  projection after draft edits or awaited work. It owns no routing writes.
- `provider-model-staging.ts` owns awaited model-add preparation and atomic
  draft publication. `provider-connection-staging.ts` prepares shared face and
  connection grants without mutating inputs; alias edits reuse it. Persistence
  still goes through `model-routing-authority.ts`.
- `provider-model-picker.ts` owns one disposable picker session and its async
  completion rules. The retained Provider view supplies markup and notices;
  closing the session releases DOM/listeners without cancelling authorized adds.
- `provider-key-reveal.ts` owns temporary plaintext presentation; replacement
  drafts stay with routing. `provider-key-stats.ts` owns read lifetime and runtime
  override ordering against server namespace evidence. Neither is the general
  named-credential vault or a second dispatch policy owner.
- `provider-key-editor.ts` owns unsaved key inputs and coalesced submissions.
  `provider-mutations.ts` prepares atomic Provider endpoint commands through
  `model-routing-authority.ts`'s existing write chain. Acknowledged writes and
  subsequent view refresh failures are separate outcomes.
  `prepareProviderRemoval` captures a target before an awaited confirmation;
  both ordinary Provider deletion and Catpaw disconnect use that boundary.
- The wire schema is `frontend/src/api/api-v1-model-routing.generated.ts`;
  behavior and alternatives live in `docs/FRONTEND_ARCHITECTURE.md`,
  “Committed model-routing projection”.
- Verify `tests/frontend/test_settings_routing_projection.py`,
  `test_settings_save_feedback.py`, `tests/model_picker_startup.test.cjs`, then
  `tests/model_routing/test_model_reasoning_settings_browser.py` and the frontend
  gates named in the parent guidance.
- For initial-read lifetime, start with `tests/frontend/test_settings_load_lifecycle.py`,
  the close/save and routing projection tests, then the real Speech Settings
  close/reopen journey and `tests/server/test_devices_panel_and_platform_download.py`.
- For save intent, use `tests/frontend/test_settings_save_lifecycle.py`, the
  credential and Speech lifecycle suites, then `test_settings_save_feedback_browser.py`.
  Search/proxy editor tests invoke the typed Save entry through native modules.
- For import/OAuth lifetime, use `test_settings_import_oauth_lifecycle.py`, the
  existing OAuth callback-gate, exchange-order, reset and proxy-fallback suites,
  then `test_settings_oauth_lifecycle_browser.py` with intercepted synthetic auth.
- For Provider reads, start with `tests/provider_settings_projection.test.cjs`
  and `tests/providers/test_provider_model_add.py`; exercise request-name and
  probe browser journeys when changing their integration.
- For model-add transactions, use `tests/providers/test_provider_model_staging.py`,
  the two `provider_{connection,model}_staging.test.cjs` specifications, then
  `tests/providers/test_provider_request_names_browser.py`.
- For picker state and teardown, start with
  `tests/providers/test_provider_model_picker.py` and the Settings close/save
  specifications before the Provider browser journey.
- For credential views, use `tests/providers/test_provider_credentials_view.py`,
  the Settings close/save tests and the Provider browser credential journey.
- For immediate mutations, use `tests/providers/test_provider_mutations.py`
  and `tests/frontend/test_settings_routing_projection.py`, then the real
  Provider browser credential editor journey.
- For credential replacement saves, start with
  `tests/frontend/test_settings_credential_replacement.py` and the credential
  view tests, then the Provider browser replacement journeys.
- For STT persistence, use `tests/providers/test_speech_routing_lifecycle.py`,
  `test_speech_provider_persist.py`, `tests/frontend/test_frontend_stt_settings.py`
  and the real `test_speech_settings_browser.py` journeys.
- For Catpaw lifecycle, start with
  `tests/providers/test_catpaw_connection_lifecycle.py` and the Provider mutation
  contracts, then `tests/llm/test_catpaw_local_import.py` and
  `tests/providers/test_catpaw_settings_browser.py`.
