# Model registration contract

Every new model must enter the application through
`lib.model_registration.register_model()`. Provider templates, Settings saves,
model discovery, and the public BYO-provider API use the same model shape:

```json
{
  "model_id": "example-model",
  "capabilities": ["text", "thinking"],
  "rpm": 60,
  "context_window": 1000000,
  "pricing": {
    "input": 3.45,
    "output": 13.81,
    "currency": "USD",
    "unit": "per_million_tokens"
  }
}
```

- `pricing.input` and `pricing.output` are billable rates per million tokens
  and must be supplied together. `currency` is `USD` or `CNY`.
- Settings localizes these rates for display only. The server-config field
  `model_price_display` supplies one bounded USD-pivot card; Chinese displays
  CNY, English USD, and future Japanese/Korean language packs select JPY/KRW.
  Opening or saving the editor never changes an existing row's authority
  currency: edited local values convert back before registration. A missing
  pivot falls back to USD/source currency instead of relabeling unconverted
  digits. The currently selectable UI languages remain Chinese and English;
  adding a locale must reuse this policy rather than fork pricing state.
- `context_window` is a positive token count. Omit it only when the upstream
  limit is genuinely unknown; Settings will label that state explicitly.
- `request_ids` contains provider wire IDs when they differ from `model_id`.
  Registration applies the same pricing to the logical ID and every wire ID.
- `cost` is not a registration field. It was a private blended routing hint,
  is rejected by the public registration API, and is stripped while loading
  historical Settings data.

The dispatcher derives any internal ordering heuristic from the real input and
output rates. Cost accounting resolves provider-scoped registered pricing first
and then the global pricing table.

For an official model, update its provider template using this shape. If the
same public model ID is also registered without a provider, call
`register_model()` without `provider_id`; that installs its global pricing
fallback through the same interface. Add a focused contract test covering the
template, pricing, context window, and logical/wire-ID projection.

## Normalized model catalog

The normalized catalog in `lib/model_catalog/` is the authored view over
those registrations. Its machine contract is
`contracts/model_catalog_v1.schema.json`; it maps `models` (one logical model
with a union of capabilities), `offerings` (one provider × model pair, carrying
the canonical registration fields minus `model_id`/`enabled`), and `routes`
(keyed by logical model, with ordered `offering_ids` and the `score`
strategy). Compilation and validation are pure functions — they never activate
pricing or dispatch registries.

`server_config.json.model_catalog` is the persisted authority when present;
`providers[].models` is a derived compatibility projection rebuilt from the
catalog so the dispatcher and legacy Settings surface keep working. The
`GET`/`PUT /api/v1/model-catalog` endpoints read and replace the catalog with
an integer revision compare-and-swap. `_catalog_revision` is the projection
marker used to detect a stale provider row so an older saved snapshot cannot
overwrite a newer catalog `enabled` value. Logical `enabled` is an aggregate
of its offerings: toggling a logical model cascades to its offerings, and
toggling an offering recomputes its logical model.
