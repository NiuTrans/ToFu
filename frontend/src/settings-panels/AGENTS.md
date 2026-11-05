# Settings panel fragments

- This directory owns migrated panel markup. `scripts/compose_settings_panels.mjs`
  publishes the matching fragments under `static/settings_panels/`; edit the
  source here and regenerate. Other legacy fragments retain their delivery
  until their authoring source is moved here.
- Behavior belongs to the typed feature owner under `../features/settings/`,
  translations to `../i18n/locales/`, and styles to `../styles/settings/`.
- Verify `node scripts/compose_settings_panels.mjs --check`, the owning browser
  journey, then `npm run check:frontend`. Current speech ownership and activation
  semantics are in `docs/FRONTEND_ARCHITECTURE.md`.
