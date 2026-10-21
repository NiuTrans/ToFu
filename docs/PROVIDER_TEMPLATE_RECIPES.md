# Provider offering recipes

**Owner:** `lib/provider/provider_template_recipes.py`
**Machine contract:** `contracts/provider_offering_recipe_v1.schema.json`
**Bundled sources:** `lib/model_info/data/*.json` (package catalog), then
`static/provider_templates/*.json` (deployment overrides), then
`bundles/*/provider_templates/*.json` (release-tiered bundles), then
`<config_path>/provider_templates/*.json` (user tier) — later layers win by
key (docs/SITE_PACKS.md §4)

The catalog is hot-reloaded on every read: editing or dropping in a template
file takes effect on the next picker/API call without a restart. Python
loader/route changes still require a server restart; frontend changes require
source generation, a build and a page reload. A transiently invalid file (mid-edit, malformed drop-in) is skipped with a
warning and never takes the rest of the catalog down.

Provider templates are first-run hints for provider accounts. They are not an
authority for logical models or runnable routes. The owner-scoped
`tofu.model-routing/v2` aggregate owns Providers, ProviderAccess resources,
Offerings, Deployments, and credentials.

Mainstream recipe IDs resolve against the owner's Models registry, with
developer catalog seeds filling identities not installed yet. Missing recipe
capabilities, context and inference settings inherit that Model; explicit
provider differences remain scoped to its Offering/Connection/Deployment.
Compiled offerings retain `inherited_model_fields` for absent capability and
context declarations, so later Model edits update the inherited values too.
Relay metadata never expands an existing public Model. A real upstream name
belongs in `request_ids`, which compiles to Deployment `wire_model_id`.
Catalog discovery binds unique exact registered names immediately while
retaining `unprobed`; unknown or ambiguous identities remain provider scoped.
World catalog sync also repairs existing pending identities and missing public
profiles. See [Model inheritance](MODEL_REGISTRATION.md#declared-inference-profiles).

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


### Inference contract inheritance

`inference_profile` is optional bounded metadata referencing the canonical v2 schema. Provider-level fields are inherited by faces/endpoints and overridden per section, not replaced by a shallow dictionary merge. Explicit `unknown`/`unsupported` reasoning clears inherited enable details. Runtime precedence is Model → Offering → Connection → Deployment; owner-authored declarations remain authoritative.

A template's authored `contract.reasoning_efforts` and `default_reasoning_effort` apply only to exact IDs listed in `contract.models`. An individual `contract.models[id]` may override these defaults: Astra declares `ultra` there while GPT-5.6 retains `max`. Normalization imports those facts into the recipes' executable profile, without hardcoded model families or guessed future generations. Recipe declarations remain legitimate provider-scoped overrides of the model contract. An adjacent unlisted ID remains unknown. Installation uses the existing non-writing compiler and owner-scoped save path; this does not silently replace profiles in already-saved routes.


The same declared profile follows pooled and request-owned slots across Chat Completions, Responses and Messages. `responses.feature_profile` selects a dialect, not every optional feature: installed feature bindings and protocol/subscription limits still apply. Unconfigured reasoning keeps the isolated compatibility path; this does not remove model-name policy from unrelated image/context subsystems. Offline specifications: `test_route_inference_contract.py`, `test_discovery_inference_profiles.py` and `test_reasoning_contract_compiler.py`.

## Categories and inventory evidence

The recipe schema owns `category`: `official` is a model developer's own API,
`relay` is an independent inference host, aggregator or gateway, and `local`
is a local endpoint. This classifies the endpoint operator, not the creator
of every model it hosts or an endorsement. Deployment overrides retain the
same semantics. Defaulting an unclassified import to `relay` is conservative.

`catalog_mode: curated` carries a reviewed seed set. `discovery` can carry no
models when a public inventory is unavailable or account-specific. Optional
`catalog_source` records the source HTTPS URL and an actual ISO check date;
reads never manufacture fresh dates. An inaccessible inventory is not proof
that a model is unavailable, and a released weight checkpoint is not proof
that a vendor API serves that checkpoint. Remove unverified seed pairings
instead of inventing hosted wire IDs or globally retiring the model.

The typed Settings picker groups/badges official, third-party and local
entries, searches provider names, endpoint hosts and all seed wire IDs, and
shows seed counts, examples and evidence links. An empty remote discovery
entry opens a password input and lists models with that provider's credential
before creating an access. It sends no generation request; empty/failing
results create nothing and remain retryable. Closing before discovery finishes
fences off the add, clears the input and never echoes upstream credential
errors. An empty local endpoint belongs to the separate Local deployment flow.
Keyboard focus, Escape/outside dismissal, language changes, responsive layout
and detached-settings teardown are executable picker/browser specifications.

Fresh compilation and `/models` discovery create `unprobed` deployments:
neither is an inference-success verdict. Request-name access is per-wire
within a credential's grant (`excluded_wire_model_ids`); visible model/access
switches and credential grants control
routing. Matrix probes provide diagnostics, not a prerequisite for saving or
using an authorized route. Template-supply synchronization may record its own
measured `ok` evidence. Installed user-disabled Offerings and revoked grants
remain unchanged.

## Explicit inference discovery

Ordinary owner-scoped catalog sync lists both `openai` and
`openai_responses` Connections at their API base `/models` path. Native
`anthropic` discovery uses `/v1/models`, `x-api-key`, and the declared
`anthropic-version`, not an automatically substituted OpenAI Bearer header.
Owner Connection headers remain explicit overrides. Shared HTTP and egress
validation apply; redirects are disabled. Native cursor pagination is bounded
at ten pages and 2,000 rows, while ordinary sync retains its stricter 200-model
catalog / 32-addition-per-pass limits. An incomplete native listing fails
closed rather than presenting a truncated inventory as complete.

Explicit capability lists are not unioned with model-name guesses. Missing
reasoning metadata remains unknown: neither a new version number nor a basic
chat probe proves reasoning support or enables thinking. Structured
`inference_profile` declarations and upstream ordered reasoning levels/defaults
flow into endpoint Offerings, not official logical Model facts. Native
capability `supported` booleans, thinking `types`, and effort `levels` become
structured support/dialect/effort facts; unknown future model names are inert.
The native shapes are covered by offline fixtures, not a live-provider claim.

Local engine declarations and Codex catalog efforts survive import. Ordinary
sync remains add-only: it never re-enables a disabled offering, changes owner
grants, or replaces an existing profile. OAuth refresh retains owner switches, credential grants, context overrides
and owner-authored profile overrides. Managed Codex capabilities follow only
a recorded last-admitted catalog baseline (`catalog_capabilities`); legacy
unmarked capability lists remain explicit, even when the reasoning profile
has catalog provenance. A fetched Codex catalog is private to the admission
thread until owner-scoped model-routing validation and CAS succeed; failed
admission retains the last good bounded cache and retries a pending candidate
without leaking it to normal readers. Cache age remains visible rather than
fabricating freshness.

Problem: a client-version upgrade discarded a valid subscription catalog and
network failure then replaced its capabilities with cold-start defaults.
Decision: retain the admitted same-account catalog as stale across upgrades,
refresh even within its TTL, and scope ETag/304 reuse to the same client
version. Account changes and schema incompatibility still invalidate it.
Alternative rejected: expanding static capabilities by model name, which
invents provider evidence and can overwrite explicit owner restrictions.
If the catalog file is absent or corrupt, static cold-start supply cannot
replace an already installed managed Codex ProviderAccess either.

## Dynamic feed

**Owner:** `lib/provider/provider_template_feed.py`
**Cache:** `data/config/provider_template_feed.json` (machine-written; holds
no secrets and inherits the `data/` export exclusion)
**Spec:** `tests/providers/test_provider_template_feed.py`

`PUBLIC_CATALOGS` is the allowlisted endpoint authority: OpenRouter,
Cerebras, SambaNova and OVHcloud update only their OWN templates, and only
when the template base URL matches. OpenRouter membership never implies
availability, wire spelling or pricing at another provider. A deployment
URL override does not inherit the public operator's inventory. Any valid
`static/provider_templates` override also bypasses the feed, even at the
same endpoint, preserving deployment-authored models, wire IDs and prices.

`load_provider_templates()` reads the bounded cache and schedules one
short-lived daemon refresh when any source is missing or past its TTL
(default 24 h, `TOFU_TEMPLATE_FEED_TTL_S`, clamped 1 h–7 days). The daily
model-intelligence task also refreshes these sources before world-catalog
sync. No HTTP runs in the picker read. Version-1 cross-provider caches are
ignored; each source carries its own fetched timestamp and allowlisted URL.

Safety and resource bounds:

- A usable source replaces its provider's seed set; absent seeds and obsolete
  wire aliases do not survive. Only matching authored rpm/capability hints
  are retained. The source owns exact `request_ids`, price and context;
  missing metadata stays unknown rather than falling back to stale values.
- One pass has a 60 s cancellable deadline, an 8 s HTTP timeout, no redirects,
  4 MiB response/cache limits, 4096 inspected rows and 256 candidate seeds
  per source. The most recent dated entries fit the seed budget first; this
  is a bounded onboarding selection, not the account's complete catalog.
- Partial failures preserve each source's last-good data. After 7 days it
  stops contributing and the authored seed list is used. Failed attempts
  back off 5 minutes; only one scheduled or active refresh exists at once.
- Non-chat, deprecated, redirect and colon billing variants are excluded;
  dated model identities remain exact. Multiple channel spellings resolving
  to one ModelRef select one deterministic seed (shortest advertised ID),
  not a mixed wire pool with one misleading price. Cache reads apply the
  same rule, so older cached channel collisions cannot break compilation.
- Catalog USD token prices convert to per-million Offering `actual_pricing`.
  Neither relay recipes nor feed quotes become official Model `list_pricing`.
  An official operator hosting another creator's model also cannot set that
  creator's list price. Invalid/missing/all-zero feed prices stay unknown,
  not falsely free.
- `retired_model_ids` remains the authored kill-switch. Cross-template
  authored retirement consistency and world-catalog pruning are pinned by
  `tests/model_routing/test_world_catalog_sync.py`; removing a stale provider seed is not
  itself a global retirement declaration.
- `TOFU_TEMPLATE_FEED=0` disables both refresh and merging (the test default).
  Bootstrap stays stdlib-only and uses authored seeds without this feed.
  Refresh never writes owner ProviderAccess, credential or deployment state.

Do not confuse this layer with provider catalog sync (Lifecycle, step 6):
the feed keeps the ONBOARDING catalog current from public data before any
credential exists; catalog sync keeps a CONFIGURED provider current from its
authenticated `/models`.

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
bundle. Compilation is non-writing. Selecting a curated template creates a
keyless access under the current revision; the card then accepts credentials.
For discovery-only templates, the explicit “Discover models & add” action
lists the account inventory first and submits a secret-free bundle plus the
credential-secret envelope through the existing owner-scoped create API.
Credential material is used for the requested discovery and encrypted at the
repository boundary; it is never included in the public template or bundle.

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
6. After setup, `lib/llm_dispatch/provider_catalog_sync.py` keeps the
   configured provider current: on a bounded cadence it re-lists the
   provider's authenticated `/models` and CAS-appends NEW wire identities as
   pending offerings. The pass is strictly add-only (existing rows —
   including user-disabled offerings — are never modified or retired),
   remembers user-deleted synced models in a per-owner dismissed ledger so
   they never resurrect, skips aggregator-scale catalogs (>200 ids)
   wholesale, and caps one pass at 32 imports per provider. A disabled
   provider access is still synced — its switch gates routing, not
   catalog freshness, so re-enabling never reveals a stale list (a
   disabled connection or credential, a per-component revocation, stays
   untouched). It rides the shared local-endpoint monitor thread;
   `TOFU_MODEL_CATALOG_SYNC=0` opts out.
7. Independently, `lib/model_routing/template_supply.py` converges every
   INSTALLED access that fingerprint-matches a template toward a fresh
   install of that template, so a card added last month picks up recipes
   shipped in a later template revision (e.g. gemini-3.8-flash on the
   YourProvider gateway, whose empty `/models` listing gives the catalog sync
   nothing to discover). Fingerprint match requires every template base
   URL (top-level + protocol faces) among the access's connections AND an
   existing relation (provider brand match or ≥1 shared recipe identity),
   so a hand-built minimal access to the same host is never flooded. A
   missing recipe lands only after a live probe on the access's own
   credential proves the gateway serves it (`ok` / `rate_limited`, routed
   per recipe through `build_probe_work` so Claude candidates probe the
   Anthropic face); `ok` deployments record passed evidence and rate-limited
   ones remain unprobed. Neither has a separate enablement state, and dead
   ids never resurrect. New rows reuse
   the compiled bundle verbatim with ids re-derived for the target access
   and credential authorization extended to the new confirmed models.
   Insert-only for ADDITIONS; a converged access makes later passes
   no-ops. One authored exception: when a recipe renames a logical id by
   taking over the SAME wires (`deepseek-v4-flash` → `deepseek-v4.1-flash`
   in `yourprovider.json`, whose `request_ids` still list the v4-flash mirrors),
   the earlier offering's whole wire pool is authored by a different recipe
   of that template, so it is marked `stale` — the existing not-supplied
   state the picker hides and dispatch rejects. Nothing is deleted and the
   Model row stays in the world registry; partial wire overlap is left
   alone because it may be genuine shared capacity.
   It rides the same shared monitor; `TOFU_TEMPLATE_SUPPLY_SYNC=0` opts
   out.

   Two narrower entry points share that owner instead of waiting for the
   cadence (`TOFU_TEMPLATE_SUPPLY_SYNC_INTERVAL`, 6h default, 240s boot
   delay):

   * `sync_template_supply(provider_access_ids=[...], backfill_only=True)`
     converges ONE card with deterministic additions only — no probe, no
     verdicts consumed — and never touches another provider. This is what
     makes "the template gained an alias" reach an installed card
     immediately;
   * `POST /api/v1/providers/<id>/probe-cells/start` runs exactly that
     scoped backfill before it plans the matrix. A wire id added to a
     recipe after this card was installed therefore HAS a cell to probe;
     otherwise the matrix would show only the pre-existing cells and a row
     whose public id is rate-limited would read as "this model does not
     work" while the working wire was invisible. The convergence is a
     bounded template-derived write (Connections/Deployments only), and a
     card that gained a wire or retargeted an existing Deployment reloads
     its bundle and dispatcher and skips completed memory/disk snapshots.
     Evidence from the former endpoint is not evidence about the repaired cell.

   Problem: repeated face convergence mistook an absent grant for missing
   initialization and restored owner-revoked access. Decision: initialize
   grants only for faces created in the current CAS, and only for credentials
   already authorizing that Model and non-excluded wire on an existing
   transport for the Offering. Existing-face grants, including newly added
   credentials, remain exactly as submitted by the credential creation/save
   boundary. Empty transport/model grants and `excluded_wire_model_ids` are
   never widened by backfill. Alternative rejected: periodically granting
   every face cannot distinguish initialization from explicit revocation.

   Supply probes merge per-cell face headers with credential-envelope headers,
   rather than choosing one dictionary. Credential material wins collisions,
   matching canonical dispatch's Connection → Deployment → credential order;
   selector headers and envelope authentication therefore both reach the
   synthetic transport boundary in the offline specifications.

## CatPaw Credit-Hub gateway (catpaw.json)

`catpaw.json` onboards the CatPaw CLI's `mcli.internal.example.com` gateway, which
bills the employee's daily Credit Hub quota (not a Friday AppID). The API key
is the 32-char `AUTHORIZATION` token from the CatPaw CLI config
(`~/.config/mcopilot-cli/.config.yaml`). The gateway speaks the Anthropic
Messages protocol but rejects any request whose `system` does not start with
the Claude Code identity line, or whose `X-Working-Dir` header is
missing/empty (HTTP 400 "Request is not allowed"); `X-Client-Token` /
`X-Timestamp` and the other CLI headers are NOT validated. Those two
fingerprints are applied at the wire boundary by
`lib/llm/catpaw_gateway.py`, keyed on the base-url host — no per-template
`extra_headers` are needed. The roster mirrors the CatPaw whitelist from the
internal CatPaw doc; probes verified 2026-09-09 for kimi-k3, glm-5.3 and
deepseek-v4-pro over both streaming and non-streaming paths
(`tests/llm/test_catpaw_gateway.py`).
## Migration guardrails

- Keep `recipe_version` explicit.
- Do not author both `offering_recipes` and `models`.
- Do not use `cost`; register `pricing.input` and `pricing.output` per million
  tokens with an explicit USD/CNY authority currency.
- Preserve the provider protocol and exact wire IDs; they belong to v2
  Connections and Deployments, not the logical model.
- Verify changes with `tests/providers/test_provider_template_recipes.py`,
  `tests/model_routing/test_model_routing_contract.py`, `tests/model_routing/test_model_routing_bootstrap.py`
  and the focused provider-feed and browser-picker specifications.

Provider model addition keeps identity and request names separate. An exact
endpoint/recipe match supplies the declared `request_ids`; Settings renders each
as an independent editable row under the Model header. The YourProvider V4.1 Flash
recipe retains its provider-declared V4 Flash wire names. Registration readiness
is checked by the shared repository before any imported Model can route; see
[MODEL_REGISTRATION.md](MODEL_REGISTRATION.md#registration-admission-and-evidence).
