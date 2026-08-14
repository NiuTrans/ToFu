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
