# Provider offering recipes

**Owner:** `lib/provider_template_recipes.py`
**Machine contract:** `contracts/provider_offering_recipe_v1.schema.json`
**Bundled sources:** `static/provider_templates/*.json`

Provider templates are onboarding recipes for provider accounts. They are not
an authority for logical-model definitions. The normalized model catalog owns
logical models, provider offerings, and routes; applying a provider template
merely contributes offerings to that catalog.

## Authored shape

```json
{
  "recipe_version": "tofu.provider-offering-recipe/v1",
  "key": "example-relay",
  "name": "Example Relay",
  "base_url": "https://relay.example/v1",
  "offering_recipes": [
    {
      "model_id": "shared-model",
      "request_ids": ["vendor/shared-model-v2"],
      "capabilities": ["text", "thinking"],
      "rpm": 60,
      "pricing": {
        "input": 1.0,
        "output": 4.0,
        "currency": "USD"
      }
    }
  ]
}
```

`model_id` is the exact logical identity. A provider-specific spelling belongs
in `request_ids`. This is what lets strict routing move from one provider to
another for the same logical model without silently changing model identity.
No name folding, alias heuristic, or case normalization may create that link.
If an operator or bundled recipe has not explicitly mapped two spellings, they
remain distinct logical models.

A provider can have at most one offering recipe for a logical `model_id`.
Multiple wire deployments for that offering belong in its ordered
`request_ids` pool.

## Compatibility projection

`normalize_provider_template()` reads the v1 authority. It also accepts the old
top-level `models` array at import boundaries so older user files remain
usable. New bundled files must author only `offering_recipes`.

For an older browser bundle, the templates endpoint may include a derived
`models` alias beside `offering_recipes`. The alias is transport compatibility,
not a second editable source. Applying a recipe projects its offerings into a
provider row's legacy `models` field; the normalized model-catalog compiler
then materializes the logical model, provider offering, and score route.

The recipe compiler uses `lib.model_registration.normalize_model_entry`, so it
removes the obsolete blended `cost` hint, validates capabilities, context and
billable pricing, preserves unknown canonical registration fields, and never
activates process-global pricing or dispatch registries.

## Lifecycle

1. The template loader validates and normalizes the authored recipe.
2. Settings creates a provider shell (URL, credentials, protocol/faces).
3. Recipe entries become provider-scoped offerings for that shell.
4. Saving passes through the model-catalog authority and derives provider
   `models` for legacy dispatcher/config consumers.
5. Discovery may add or retire provider offerings, but it must not fuzzy-merge
   logical identities or overwrite a newer catalog revision from a stale
   Settings snapshot.

## Migration guardrails

- Keep `recipe_version` explicit.
- Do not author both `offering_recipes` and `models`.
- Do not use `cost`; register `pricing.input` and `pricing.output` per million
  tokens with an explicit USD/CNY authority currency.
- Preserve provider wire faces and provider-level protocol fields; they belong
  to the provider shell, not the logical model.
- Verify changes with `tests/test_provider_template_recipes.py` and the model
  catalog round-trip/strict-failover tests.
