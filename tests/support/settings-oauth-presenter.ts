/** Classic OAuth fragment fixtures mount one real Settings presentation scope.
 * Production opens it through core-panel; isolated exchange/formatting fixtures
 * omit that entry. Export the same typed owners, never a second auth policy.
 */
export {captureOAuthPanel} from '../../frontend/src/features/settings/oauth-panel-lifetime';
export {renderCodexSharedLogin} from '../../frontend/src/features/settings/codex-shared-login';
export {mountSettingsPresenterFixture as openOAuthFixture} from './settings-presenter-scope';
