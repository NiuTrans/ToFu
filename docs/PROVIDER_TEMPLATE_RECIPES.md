# Provider offering recipes

**Owner:** `lib/provider_template_recipes.py`
**Machine contract:** `contracts/provider_offering_recipe_v1.schema.json`
**Bundled sources:** `static/provider_templates/*.json`

Provider templates are first-run hints for provider accounts. They are not an
authority for logical models or runnable routes. The owner-scoped
`tofu.model-routing/v2` aggregate owns Providers, ProviderAccess resources,
Offerings, Deployments, and credentials.

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

`model_id` is the requested identity suggested during setup. A
provider-specific spelling belongs in `request_ids`. The authenticated probe
remains authoritative for what the account actually serves; unconfirmed names
enter v2 as provider-scoped pending identities and cannot be fuzzy-merged into
an official logical model.

A provider can have at most one offering recipe for a logical `model_id`.
Multiple wire deployments for that offering belong in its ordered
`request_ids` pool.

## Bootstrap projection

`normalize_provider_template()` reads the v1 recipe and accepts old top-level
`models` only at the import boundary. New bundled files author
`offering_recipes` exclusively. The stdlib-only repair launcher derives a
temporary in-memory `models` view because it cannot import application code.
It never persists that view as `server_config.providers`.

After the user saves setup, bootstrap writes the credential to the existing
`.env` boundary and atomically stages a secret-free
`tofu.bootstrap-provider-stage/v1` draft. Once the storage sidecar is ready,
startup imports the draft into the explicit personal-owner v2 repository,
stores the credential through the repository secret channel, and consumes the
draft. This works both before and after v2 has already been activated.

The recipe compiler uses `lib.model_registration.normalize_model_entry`, so it
removes the obsolete blended `cost` hint, validates capabilities, context and
billable pricing, preserves unknown canonical registration fields, and never
activates process-global pricing or dispatch registries.

The settings UI reads the same catalog through
`GET /api/v1/providers/templates` and asks
`POST /api/v1/providers/templates/compile` for a selected, secret-free v2
bundle. Compilation is non-writing. The client stages the bundle under the
current revision and sends API-key material only through the encrypted
credential-secret envelope when the user explicitly saves.

## Lifecycle

1. The stdlib loader projects a bounded recipe list and performs authenticated
   model discovery when possible.
2. Bootstrap atomically stages transport facts without credential material.
3. Startup activates an empty owner v2 authority when no legacy route exists,
   or completes the one-way legacy import when it does.
4. The staged provider replaces only its deterministic Provider-owned v2
   resources through bounded CAS; user policy fields survive refresh.
5. Successful import deletes the stage. A missing credential leaves it intact
   for an idempotent retry.

## Migration guardrails

- Keep `recipe_version` explicit.
- Do not author both `offering_recipes` and `models`.
- Do not use `cost`; register `pricing.input` and `pricing.output` per million
  tokens with an explicit USD/CNY authority currency.
- Preserve the provider protocol and exact wire IDs; they belong to v2
  Connections and Deployments, not the logical model.
- Verify changes with `tests/test_provider_template_recipes.py` and the model
  routing bootstrap/strict-failover tests.
