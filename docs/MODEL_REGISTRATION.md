# Model-routing v2 contract

The machine authority for models, service providers, access, and runtime route
selection is
[`contracts/model_routing_v2.schema.json`](../contracts/model_routing_v2.schema.json).
`lib/model_routing/` validates and persists one revisioned
`tofu.model-routing/v2` aggregate per owner. There is no configured `Route`,
provider model array, alias pool, or separate BYO routing authority.

## Entity meanings

```text
Creator ── Model
              ↑
Provider ── ProviderAccess ── Offering
                 ├─ Connection └─ Deployment → wire_model_id
                 └─ Credential ── authorizes Connection / Model
```

| Entity | Identity and responsibility |
|---|---|
| `Creator` | Public creator identity and display metadata. |
| `Model` | One official model, unique by `(creator_id, model_id)`. Owns official capabilities, list price, context limit, lifecycle, and quality rank. Preview and dated snapshots remain distinct models. |
| `Provider` | A service provider. Public rows are shared metadata; owner-scoped rows represent private services without weakening owner isolation. |
| `ProviderAccess` | The owner's one access pool for one Provider. Owns enablement and account-level quota/balance policy. |
| `Connection` | One protocol endpoint inside a ProviderAccess. Region and gateway namespace are connection metadata, not model identity. |
| `CredentialMetadata` | Redacted credential kind, hint, quota policy, and authorization over Connections and official Models. Plaintext is stored only by the repository's encrypted secret operation. |
| `Offering` | What one ProviderAccess actually supplies: confirmed or pending identity, actual capability subset, actual context, transaction price, priority, stale state, and enablement. |
| `Deployment` | Exactly one Offering on one Connection under one real upstream `wire_model_id`, with independent probe and identity-confidence state. |

One provider-scoped wire ID may identify only one Deployment. Every distinct
wire ID is a distinct Deployment; `aliases` and `request_ids` are rejected in
v2 documents. A pending identity is reachable only by an explicit
`{provider_id, offering_id}` selection and is never a cross-provider route.

## Prices, capabilities, and limits

Official `Model` facts and actual `Offering` facts are deliberately separate.
An Offering's capabilities must be a subset of its confirmed Model's
capabilities, and its effective context must not exceed the official limit.
Prices use `{input, output, currency, unit}` with optional cache rates;
`unit` is `per_million_tokens`. A task price budget is a hard filter—missing or
currency-mismatched actual pricing does not pass a requested limit.

Account RPM, TPM, daily token allowance, and balance belong to
ProviderAccess/Credential quota policy. They are not copied into a Model or
used as model identity.

`lib/model_info/capability_taxonomy.py` remains the vocabulary authority for
capability tags and publishes the taxonomy through `/api/v1/capabilities`.
Adding a tag updates that one taxonomy and its parity test; routing consumes
the Offering subset and does not maintain another list.

## Selection and computed routes

Native chat and agent requests use a structured model reference:

```json
{
  "model": {"creator_id": "openai", "model_id": "gpt-x"},
  "routing": {
    "preferred_provider_id": "provider-a",
    "required_context": 131072,
    "required_capabilities": ["text", "thinking"],
    "price_budget": {
      "max_input": 2.0,
      "max_output": 8.0,
      "currency": "USD"
    }
  }
}
```

`preferred_provider_id` is a stable preference, not a hard Connection or
Deployment pin. Pending identity selection instead uses
`{"provider_id":"provider-a","offering_id":"offering-x"}` and cannot
escape that Provider.

OpenAI/Anthropic-compatible requests retain their standard string `model`.
They may add `tofu.creator_id` and `tofu.preferred_provider_id` (or the
documented top-level extension aliases). A non-unique string returns
`model_selector_ambiguous` with candidates. `model@provider` always returns
`legacy_model_selector_removed`; it is never guessed.

The candidate compiler applies owner authorization, enabled/stale/pending
state, capabilities, context, protocol, cooldown, quota, and price gates. It
then orders by preferred Provider, operator priority, health, cache affinity,
connection/deployment priority, latency, and actual price. Cross-provider
failover occurs only after the preferred Provider is unavailable or fails.
After all routes for an official Model fail, a compatible higher-quality
fallback Model may be considered under the same hard request policy, preferring
the original Provider.

## Health and traceability

Configured enablement and runtime health are separate. Route-missing affects a
Deployment; network failures affect a Connection; 401/402 affects a
Credential; 403 affects the Credential×Deployment authorization. Health rows,
probe work, candidate slots, and snapshots are bounded and expire through
their owning lifecycle.

The concrete personal-computer budget is: one owner aggregate is at most
8 MiB with per-entity ceilings (256 Providers/ProviderAccesses, 512
Connections, 1,024 Credentials, 4,096 Models/Offerings, and 8,192
Deployments); one request mints at most 64 ephemeral route slots and disposes
them at request teardown. Runtime health retains at most 4,096 rows for one
hour (the diagnostic projection returns at most 256). The shared optional
probe lane admits at most 4,096 cells per task, 4–32 queued tasks, at most
eight live cell workers, a 120-second timeout, and five attempts. A persisted
RouteSnapshot is at most 16 KiB, with at most 32 transitions and 16 degradation
reasons; oldest trace detail is trimmed before persistence, never durable user
state.

Every new turn persists a redacted `RouteSnapshot`: requested structured
identity, Provider preference, actual ProviderAccess/Offering/Deployment/
Connection, credential metadata, wire ID, and transition reasons. Provider or
model failover is appended to the activity timeline without changing the
conversation's saved preference. Old turns are projected with a legacy
snapshot at read time and are not rewritten.

## Repository and migration

All reads and writes pass an explicit owner/tenant boundary to the repository.
The ProviderAccess aggregate is replaced with revision compare-and-swap;
credential secret writes use a separate encrypted operation. SQLite details,
filesystem paths, and single-user globals do not cross that layer.

The one-way importer converts legacy faces/endpoints, API keys, OAuth/local
identity, provider models, key access, and every alias/request ID into v2
entities. It first emits a redacted plan and backup, validates counts,
references, secrets, and candidate routes, then switches the authority in one
CAS commit. A rejected or failed plan stores a recovery receipt and leaves v2
inactive. After activation, legacy rows are migration input only: there is no
dual write or projection back to `providers[].models`.

If an interrupted first cutover left an active v2 aggregate without a
migration marker (for example, OAuth reconciliation committed before the
legacy aggregate could fit in a storage receipt), startup compares exact
legacy Provider identities with the active authority and imports only the
missing ProviderAccess bundles. The merge uses the same validation, encrypted
secret channel, and revision CAS, so resources already written to v2 are
preserved and the repair is idempotent.

## Browser projection

The browser projects the v2 authority along the same semantic boundary as the
dispatcher:

- The chat picker lists each confirmed `(creator_id, model_id)` once. Its
  secondary control is `Auto Provider` or a preferred Provider; choosing a
  Provider never changes the Model identity. Pending identities remain in a
  separate Provider-scoped section.
- Settings has separate **Models** and **Providers** pages. Models is a
  read-only Creator/Model catalog for official models: it groups Models by
  Creator and may project Model-owned quality rank and list pricing, but it
  never reads ProviderAccess, Offering, Deployment, wire identifier, alias, or
  route state. The adjacent Artificial Analysis source is external benchmark
  enrichment keyed by that exact Creator/Model identity; its encrypted
  owner-scoped key, cache status, and score attribution do not create or alter
  a Provider. A future owner-created custom Model uses an explicit editable
  branch. Providers owns the supply projection: each Provider card names its
  canonical Models and may show that Provider's differing wire identifiers as
  aliases. The same exact Model may therefore appear under several Providers.
- A Provider can own multiple Credentials. Credential rotation is attempted
  inside the Provider before the dispatcher crosses to another Provider that
  supplies the same Model.
- Connection and wire request identifiers are operational details. They are
  bounded and lazy-rendered only in the Provider manager's connection and route
  diagnostics views; ordinary chat cannot pin either resource.
