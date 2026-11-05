/**
 * Responsibility: start locale and application downloads together, then publish
 * boot failure through the existing shell boundary. The application owner alone
 * announces readiness. Dependencies: i18n, self-hosted fonts, lazy application.
 */
import './styles/fonts.css';
import { ready as i18nReady } from './i18n';

void Promise.all([i18nReady(), import('./application')])
  .then(([, application]) => application.startApplication())
  .catch((error: unknown) => {
    console.error('[boot] application initialization failed', error);
    window.dispatchEvent(new CustomEvent('tofu:app-failed', { detail: { error } }));
  });
