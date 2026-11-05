# UI localization

- `languages.json` is the only list of UI languages: picker order, BCP 47
  tags, endonyms, the default, the fallback and the source locales. Python
  reads it through `lib/http_transport/ui_languages.py`; `index.ts` renders
  the Settings language select (`#settingLanguage` in
  `static/settings_panels/general.html`) from it, so the panel holds no copy.
- Author copy only in the source locales `locales/{zh,en}.json`; they share
  one key and placeholder vocabulary. Derived locales are produced by
  `python3 scripts/translate_i18n_locales.py --model <translator>
  --review-model <reviewer>` (two strong models from different families,
  through the running application's LLM API). Run it after adding or editing
  source copy: it translates missing, stale (`translation-sources.json`) and
  invalid keys from both sources with `translation-glossary.json`
  terminology, an independent review and mechanical placeholder/markup checks.
  A small fix to a derived string may be edited by hand.
- Call `t(key, params)`. A derived string may use each placeholder at most as
  often as the sources (some screens replace only the first occurrence). `{s}`
  is the English plural suffix; other languages phrase counts so they are
  correct for every number.
- `npm run generate:i18n` writes `contract.generated.ts`,
  `languages.generated.ts` and `generated/*.generated.json`; delivery
  completes pending derived entries from the fallback locale.
  `npm run check:i18n` fails until derived locales are complete and current.
- Adding a language: add it to `languages.json`, give the translation tool a
  `LANGUAGE_POLICIES` entry (style guide and plural policy), run the tool (it
  fills that glossary column first) and rebuild.
- Derived labels must fit their fixed-width controls; settings selects show
  about 32 monospace characters beside the chevron. Shorten in the source's
  terse style rather than letting a closed select clip.
- Verify with `tests/i18n/`, `frontend/src/i18n/index.test.ts` and
  `npm run check:i18n`.
