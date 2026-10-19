# Model-routing v2 contract

The machine authority for models, service providers, access, and runtime route
selection is
[`contracts/model_routing_v2.schema.json`](../contracts/model_routing_v2.schema.json).
`lib/model_routing/` validates and persists one revisioned
`tofu.model-routing/v2` aggregate per owner. There is no configured `Route`,
provider model array, alias pool, or separate BYO routing authority.

Runtime execution Slots carry the effective Model/Offering `context_window`.
Managed Codex Offerings keep `catalog_context_window` as their last admitted
subscription-directory baseline. Refresh updates the window only while its
value still matches that baseline; differing owner overrides and unmarked
legacy limits survive. A matching unmarked value may establish provenance.
Model context remains the official creator specification, independently of
subscription supply. Repairing ambiguous legacy values uses the registration
CAS boundary with explicit evidence, never a catalog guess or runtime write.
When a managed Codex catalog first declares a context window, it replaces the
Offering's inherited Model fallback and establishes the admitted catalog
baseline. Explicit owner limits remain overrides. Problem: preserving the old
inheritance marker forever ignored newly available subscription limits, so a
stale 128K Model could keep forcing premature compaction even after catalog
refresh. Decision: transition only inherited fallback metadata to declared
supply, bounded by the current Model; repair incorrect official Model facts
separately through registration. Alternatives rejected: rewriting Model facts
from subscription defaults conflates two authorities; retaining inheritance
ignores supplier evidence and can also exceed a smaller subscription window.
Specifications: `tests/oauth/test_oauth_outbound.py`.
Compaction resolves it inside the task's owner/pin boundary; exact selected-slot
admission and output clamping recheck it after fallback. Legacy model-name
estimates are fallback knowledge only, never a replacement for configured route
metadata. Provider-observed shrink remains bounded by the declared Offering.
See `docs/CONTEXT_COMPACTION.md` for budget and cache-rewrite policy.

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
| `Deployment` | Exactly one Offering on one Connection under one real upstream `wire_model_id`. Probe status and identity confidence are diagnostic evidence, not enablement gates; optional `max_output_tokens` records the concrete route's verified output ceiling. |

Problem: an OpenRouter `x-ai/grok-*` wire was attributed to Creator `xai`
but retained `x-ai/` inside `Model.model_id`, creating duplicate identities.
Decision: treat `x-ai/` as an xAI publisher namespace; Models use the bare
`grok-*` ID. Normalization rekeys legacy prefixed rows and repoints Offerings
and Credential grants, preserving exact `Deployment.wire_model_id`. The prefix
does not mean distinct variants (`grok-4.20`, `grok-4.20-multi-agent`) are the
same model. Alternative rejected: hiding the prefix in the browser, which
leaves duplicate identities and broken matching in storage.

An Offering's optional `inherited_model_fields` lists `capabilities` and/or
`context_window` inherited from its Model. Authority normalization refreshes
the materialized values on every read/save; changing the Model therefore
updates inherited provider metadata and runtime eligibility together. Removing
a field from that list makes its value an explicit provider override. Legacy
unmarked values are preserved on ordinary reads/writes; the startup provenance
migration described below classifies them once. Pending identities can carry
inheritance
intent with conservative local fallback values until a Model is bound.
Fallbacks (including the historical 32,768-token discovery budget) are not
provider measurements. `/models` preserves explicit positive context limits
from `context_window`/`context_length`, and marks missing context/capability
declarations for inheritance. A successful small chat probe cannot establish
context capacity. Specifications: `tests/model_routing/test_offering_metadata_inheritance.py`.
The authority GET advertises `metadata_inheritance_supported`; new browser
model additions omit the optional field when talking to an older server.

Provider rows display Model list prices as **reference prices** when no
provider price is configured. The tooltip states their Model source and
per-million-token unit. Inherited context similarly identifies its Model
source and is not presented as a capacity-probe result.

Provider install, OAuth refresh and interrupted-cutover recovery share
`catalog_merge.merge_missing_catalog_entities`: they may add an absent identity,
but cannot union provider capabilities or maximum context back into an existing
Model, replace its inference profile, display name, price or quality rank.
Only Model settings own those changes. Templates validate against that Model;
they no longer apply a second name-based `context_profile` ceiling afterward.
Discovery/local/desktop adapters use `offering_metadata` to preserve declared
context evidence; an unbound listing gets one 32,768-token admission budget,
not a guessed model specification. Subscription catalogs retain real context
fields and inherit when absent. Refresh preserves confirmed bindings, scoped
metadata and credential exclusions. No metadata migration claims a live probe.


Managed Codex Offerings additionally retain `catalog_capabilities`, the last
admitted catalog-derived capability list. During refresh, `capabilities` follows
the new catalog only if it still equals that per-field baseline; a narrower
owner-authored list stays explicit. The source of an `inference_profile` alone
is *not* evidence that capabilities have the same author. Legacy unmarked
Offerings remain explicit rather than being silently expanded; a one-time
owner-authorized CAS repair may update a verified legacy row through the
ordinary registration/admission boundary. Disabled ProviderAccesses/Offerings,
credential grants, context overrides and owner-authored profiles remain intact.
Neither the catalog nor this baseline may claim capabilities absent from the
official Model. This is deliberately narrower than inferring `thinking` from a
model ID or an isolated successful response.

Problem: custom OAuth entries can share the `oauth: codex` protocol but do not
own the managed `oauth_codex` catalog baseline. Decision: refresh creates
`catalog_capabilities` only for the actual `oauth_codex` Provider, not every
Codex-protocol entry. The domain rejects that field on other Providers;
loosening the domain rule would incorrectly grant catalog authorship to a
custom endpoint. A custom entry still inherits missing context without
rewriting the Model.

Startup classifies legacy unmarked Offerings once, within the owner CAS:
matching Model copies inherit, known provider-catalog limits remain overrides,
and generated catalog/subscription defaults require exact stable-ID/value
fingerprints. Redacted cutover receipts distinguish omitted fields from explicit
provider limits, even when both materialized as 128,000. Different values and
explicit markers (including `[]`) are retained. This fingerprint migration
cannot recover unrecorded edits that exactly matched an old generated value;
ambiguous differing limits remain scoped rather than being silently raised.
Pending identities remain unknown. The migration adds no persistent history,
worker, cache or credentials and is idempotent; its report includes identifiers
and reasons only. See `tests/model_routing/test_model_metadata_authority.py` and the read-only
`scripts/audit_model_configuration.py --authority export.json
--plan-metadata-migration`. The audit checks declared control/compiler agreement,
not provider availability or maximum context capacity.

One provider-scoped wire ID may identify only one Deployment. Every distinct
wire ID is a distinct Deployment; `aliases` and `request_ids` are rejected in
v2 documents. A pending identity remains inspectable through an explicit
`{provider_id, offering_id}` selection but cannot execute until identity and
registration are complete.
Output ceilings are applied after slot selection. Provider-error learning is
scoped by Provider, Offering, Deployment, and protocol rather than globally by
model name; exclusive upper bounds normalize to the largest accepted integer
before one same-route retry.

Request-name access is per-wire within a credential's grant. The visible
ProviderAccess, Connection, Credential and Offering switches and credential
ModelRef grants determine configured access; a credential grant carries an
`excluded_wire_model_ids` list that withholds individual request names of
that model, so two request names sharing a ModelRef can be enabled
independently. A probe failure or missing successful probe does not
override those controls. Confirmed identity remains an Offering mapping;
pending Offerings still require an explicit provider-scoped selection.

For existing v2 documents and older clients, Deployment `enabled` is accepted
only as a deprecated boolean input and discarded by `normalize_document`.
Canonical reads and saves omit it. True, false and missing values converge
without modifying visible switches, grants, probe evidence or revisions on
read; the next normal CAS save stores the normalized document. Consequently,
a previously hidden-disabled request name is eligible whenever the retained
controls permit it. No background migration rewrites user settings.
The chat picker, Settings model defaults and compatibility model listings also
consume these request names without an enablement/probe filter; removing the
legacy field must not make configured models disappear from those selectors.

## Prices, capabilities, and limits

Official `Model` facts and actual `Offering` facts are deliberately separate.
An Offering's capabilities must be a subset of its confirmed Model's
capabilities, and its effective context must not exceed the official limit.

Managed pricing-tier tags (`cheap`, owned by
`lib/llm_dispatch/config/_pricing.py`) are exempt from the subset check:
they derive from each provider's own price book and are re-synced from live
prices, so a reseller undercutting the creator's list price legitimately
carries a tier the official Model row lacks.
Prices use `{input, output, currency, unit}` with optional cache rates;
`unit` is `per_million_tokens`. A task price budget is a hard filter—missing or
currency-mismatched actual pricing does not pass a requested limit.

Account RPM, TPM, daily token allowance, and balance belong to
ProviderAccess/Credential quota policy. They are not copied into a Model or
used as model identity.

`lib/model_info/capability_taxonomy.py` remains the vocabulary authority for
capability tags and publishes the taxonomy through `/api/v4/capabilities`.
Adding a tag updates that one taxonomy and its parity test; routing consumes
the Offering subset and does not maintain another list.

## Registration admission and evidence

Problem: parallel import paths accepted incomplete Models and deferred failures
to chat. Decision: both production and in-memory repository CAS operations call
`registration.prepare_registration_document`; UI, templates, discovery, daily
refresh and internal callers cannot publish a new usable Model around it.
New incomplete Models are saved as `pending_metadata`; an uncompileable declared
reasoning wire becomes `pending_validation`. Only complete, locally validated
Models become `ready`; `disabled` is explicit and research never enables it.
Client-supplied readiness and issue lists are checked/recomputed, not trusted.
An invalid edit of a ready Model is rejected atomically, preserving the last
working revision, unless the owner explicitly moves it back to a draft state.

Problem: provider templates/public feeds contain supply candidates but do not
establish first-party release, pricing, context, capability, and reasoning
facts. World-catalog sync used to insert every candidate, so Settings showed
automatically generated `pending_metadata`/`pending_validation` registrations.
Decision: auto-publication checks the same projected facts as admission and
inserts only complete, ready Models. Missing-fact candidates stay outside the
global Model registry. An explicitly saved incomplete row or legacy import
remains a **non-callable draft** through shared admission, preserving research,
owner work, and migration compatibility. Sync does not prune existing drafts
solely because their facts are missing. Alternative rejected: reject all
incomplete repository writes, which would block legacy/provider recovery and
safe draft research. Specifications: `tests/model_routing/test_world_catalog_sync.py`,
`tests/model_routing/test_model_routing_contract.py`.

xAI evidence boundary (checked 2026-09-24): the official [Grok 4.7 API
page](https://docs.x.ai/developers/grok-4-7) establishes the bare ID,
500K context, text/image input, text output, and high-default effort ladder;
[reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning)
cannot be disabled, [model pricing](https://docs.x.ai/developers/models/grok-4.7)
and [release notes](https://docs.x.ai/developers/release-notes) establish
$2/$0.50/$6 per million base input/cache/output tokens with higher rates
above 200K prompt tokens; the [dated news index](https://x.ai/news) records
2026-09-21. Thus `grok-4.7` is a complete exact registration, not an
`x-ai/`-prefixed second Model. The official [multi-agent API
guide](https://docs.x.ai/developers/model-capabilities/text/multi-agent)
confirms the distinct unversioned `grok-4.20-multi-agent` API ID, but does
not prove its alias-specific context and tiered prices; the dated `-0309`
model card must not be copied onto that alias without an official mapping.
This variant is not auto-registered, and an existing owner draft remains
non-callable until independently evidenced.

The Models settings projection shows `ready` rows and legacy unmarked rows
awaiting migration, not persisted `pending_*` or `disabled` drafts. Drafts
remain in authority for research/admission and provider diagnostics; hiding
them only in presentation does not grant route eligibility or delete state.

Required facts are release date, prices/currency/unit, capabilities, positive
context, and explicit reasoning support. Supported reasoning declares the wire,
disable behavior, effort ladder (`[]` for toggle/budget only), and default for a
nonempty ladder. The actual compiler validates on/off/native effort controls
without a generation request. Unknown, unsupported and explicit zero pricing
are distinct. Model reference prices never become provider transaction prices.
Dates inferred from a name fragment cannot complete an unknown registration:
use exact maintained knowledge or sourced `registration_metadata`.

Admission also validates each Deployment's resolved provider inference profile.
It recomputes `registration_issues` on every write and excludes incomplete
Deployments. An invalid update of an enabled, previously valid provider supply
is rejected; explicitly disable the Offering to stage incomplete changes.
Compiler checks reuse up to 128 request-local profile results (keys capped at
16 KiB); this creates no resident cache or background worker.

Routing excludes incomplete/disabled Models, and unbound pending identities.
They remain inspectable/probeable, but a provider-scoped selection cannot bypass
registration. Request preparation does not silently discard requested reasoning.
Unchanged legacy unmarked Models remain compatible during migration. The daily
job classifies at most 32 unused legacy rows per pass; active legacy Models keep
their prior authority until a complete researched replacement passes admission.

Problem: discovered subscription models could remain incomplete until manual
repair, and supplier fallback values could be mistaken for official facts.
Decision: retain pending supply as the retry source and use one sourced admission
service. Alternative rejected: synchronous research during chat or trusting a
supplier listing as a complete first-party registration.

The owner-scoped daily intelligence job and the existing Codex catalog worker
share `model_catalog.registration_research`. Exact maintained facts run first:
a previously stored draft can become ready through admission without a search
or LLM call. An incomplete new remote Codex identity remains a pending Offering
outside the Model registry; its conservative transport fallback is never an
official context/capability fact. Research gathers release date, list price,
context, capabilities and reasoning, then atomically publishes a complete Model
and binds its exact pending supplies through the existing identity reconciler.
Existing drafts and owner-authored facts remain intact.

Research fetches only exact creator/official documentation hosts (including
OpenAI's developer and ChatGPT Learn hosts), accepts page text rather than
search snippets, and permits one bounded LLM extraction per Model. Every
proposed fact names fetched citation URLs with matching verbatim excerpts;
per-field URLs and the actual verification date persist with the facts. These
checks establish provenance, not infallible factual correctness. Unsupported
pricing schedules, missing facts and contradictions stay unresolved. A complete
Model and each actual Deployment must pass the ordinary registration compiler;
research does not invent wire controls or paid probe success. Missing reasoning
fields can be filled individually without replacing configured controls.

Research cannot rewrite existing identities, context/capabilities, provider
wires, credentials, prices or owner policy. A Model-content comparison and CAS
fence concurrent edits; removed or re-bound pending supplies cannot publish a
late proposal. Shutdown/account-change checks and the deadline apply before
extraction and before commit. Extraction excludes the target model and yields
on shared contention so an incomplete selection cannot research itself.

Resource budget: the existing process-wide nonblocking research flight, at most
two Models, two searches (15s each), four 4,000-character pages per Model, and
two LLM calls (20s and 2,048 output tokens each). No new worker, queue, cache or
history is retained; pending supply is the retry source. A 2,000-candidate scan
on the development host measured 70.6ms and 820 KiB temporary heap peak; it
adds no resident worker or persisted retry row. The daily parent keeps
its 300s deadline. Codex research uses its existing polling thread outside the
catalog refresh lock, with a 90s deadline: startup/new catalog data triggers a
pass, unchanged unresolved supply retries at most hourly. Research failure does
not terminate token/catalog polling. Catalog admission failures still preserve
the last-good catalog. Active candidates are prioritized and date-based
rotation (hourly for Codex, daily for the scheduled pass) avoids always retrying
the same first entry. Complete exact registrations precede unresolved research.

GPT-6.1 Sol exact knowledge (checked 2026-09-30): the official
[model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol) supplies
1,050,000 context, 128,000 maximum output, text/image input, text output,
$2/$0.10/$10 input/cache-read/output per million, and low/medium/high/xhigh/max
reasoning with medium default and no disable rung. The
[ChatGPT/Codex changelog](https://learn.chatgpt.com/docs/changelog) establishes
2026-09-29 availability. Its exact source recipe/defaults do not copy the older
Sol's disable behavior. The shared OpenAI price compiler derives cache-read
multipliers per Model, preserving this model's 5% cache price and the existing
long-context tiers. Specifications:
`tests/model_routing/test_subscription_registration_research.py` and
`tests/oauth/test_codex_catalog.py`.

Gemini 4 Argon evidence boundary (checked 2026-10-17): the only Gemini 4
model and the current GA flagship. The official
[model page](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon)
establishes the stable `gemini-4-argon` id, 1,048,576 unified input/output
context, 65,536 default max output (up to 1M with long decode), text/image/
video/audio/PDF input with text output, and thinking levels low/medium/high;
the [thinking doc](https://ai.google.dev/gemini-api/docs/thinking) pins the
default as On(medium) with no disable rung, and the
[pricing page](https://ai.google.dev/gemini-api/docs/pricing) gives intro
$2/$10 input/output (+$0.10 cache-read) per million tokens through
2026-11-30, then $4/$20 (+$0.20). The Meituan AIGC gateway serves it:
live probe 2026-10-17 returned HTTP 429 with a per-model RPM message (the
deployment exists; the project key holds no quota), not the 400
`不支持的模型类型` that unserved ids (e.g. `mimo-v2.6-pro`) return.

Claude Sonnet/Haiku 5.5 and Mistral Large 4 evidence boundary (checked
2026-10-08): Sonnet 5.5 (GA 2026-09-28, $2/$10, 1M ctx, adaptive thinking
default ON, API default effort high, lowest rung between_tools) and Haiku 5.5
(GA 2026-10-07, prompt-tiered $0.10/$0.50 ≤100K else $0.50/$2.50, 1M ctx,
default effort medium, disable works only ≤ high) register from the official
model pages [sonnet-5-5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview)
and [haiku-5-5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
plus the dated [news index](https://www.anthropic.com/news). Mistral Large 4
(Public Preview 2026-10-06, list $1.36/$4.18 with the 50% promo NOT used as
list price, 1M ctx, natively multimodal, `reasoning_effort` ladder
`['none','high']` where `none` is the off-rung) registers from the
[model page](https://docs.mistral.ai/models/mistral-large-4-0),
[announcement](https://mistral.ai/news/mistral-large-4/), and
[reasoning doc](https://docs.mistral.ai/studio/conversations/reasoning.md).
Mistral's official max-output and omitted-`reasoning_effort` default are not
documented, so neither is claimed. Haiku joins the 1M context line at 5.5 in
`lib/model_info/_context.py` (the 200K Claude family estimate no longer
applies at that floor).

Qwen toggle profile completion (checked 2026-10-08): the maintained Alibaba
`enable_thinking` (can_disable) and `reasoning_split` (always-on) groups in
`model_inference_defaults.json` now declare `supported_efforts: []` — the
explicit empty ladder for toggle/budget-only models required by
`registration_missing_fields`. Without it those Qwen Models stayed
non-callable drafts, which is why the Models settings listed only a couple of
Alibaba rows despite full pricing/context knowledge.

Alternatives rejected: name-based reasoning guesses; background research during
chat; API-only validation; and forcing existing active legacy configurations
through a strict read validator that can prevent startup. Specifications:
`tests/model_routing/test_registration_lifecycle.py`, `tests/model_routing/test_model_registration_admission.py`.

## Declared inference profiles

Models settings exposes the shared reasoning baseline (support, ordered native
efforts, default effort, disable capability and request representation).
Provider settings can stage differing reasoning fields on an Offering or
restore Model inheritance; other profile sections and providers remain intact.
Both forms edit the existing Settings draft and use its owner CAS save.

When a provider narrows the effort ladder, `resolve_inference_profile` prunes an
inherited `default_effort`/`disabled_effort` that no longer resolves on that
route. The same narrowing must also retire an inherited `can_disable` that the
smaller ladder can no longer honor on an effort-ladder wire: with neither a
`'none'` rung nor an in-ladder `disabled_effort`, the off intent cannot compile,
so the merged deployment profile would be stamped `registration_issues`
(`unrepresentable_reasoning`) and every candidate rejected. Problem: a Codex
catalog import supplied `supported_efforts` `[low…ultra]` (no `'none'`) while the
Model's official defaults supplied `can_disable: true`; the merge left the claim
intact and the only enabled gpt-6-sol route became unroutable. Decision: when
the offering narrows the ladder without re-declaring `can_disable`, and the new
ladder has no off-rung, narrow `can_disable` to `false` (off degrades to the
lightest rung) instead of emitting an uncompilable profile. Alternatives
rejected: synthesizing an off-rung the wire does not support (fabricates a
control), and leaving `can_disable: true` (uncompilable deployment). A narrowed
ladder that retains an off-rung keeps `can_disable`; an explicit provider/owner
`can_disable` always wins over narrowing. Specification:
`tests/model_routing/test_route_inference_contract.py::test_narrowed_ladder_drops_unrepresentable_can_disable`.
Asynchronous AA repaints retain only currently mounted reasoning forms (the
existing 240-model view budget), including typed input and focus. A changed
profile or unmounted Model releases its old form; no detached-form cache is kept.

Composer `depths` contain native `supported_efforts`, preserving declared order;
`effort_map` aliases are compatibility input only and never add slider rungs.
A requested token that is itself a native effort takes precedence over a legacy
alias of the same name. `ultra` remains distinct from `max`. Off is exposed only
when the selected route can actually disable. Auto-provider choices intersect
eligible routes; `default_depth` is reported only when their defaults agree.
The server also projects a bounded `restoration` table for unavailable saved UI
preferences after that intersection. Native rungs remain unchanged; known effort
names move downward, unavailable Off/Lowest chooses the lightest native rung,
and an unknown old token maps to Default. This compatibility table is advisory:
composer rendering never applies it or commits a replacement. An unavailable
saved depth remains visible as an unavailable rung, so switching back restores
the original choice. Only a user gesture changes the saved preference. Unknown
capabilities and absent routes omit the table. Explicit request validation
remains strict and never substitutes an unsupported saved depth.
Reasoning ability and route availability are separate: `availability=no_route`
retains declared Model/Offering controls for a visible, disabled composer with
an authorization/enablement notice. It never grants credentials or widens the
runtime candidate set. Unknown profiles show an explicit configuration notice;
non-reasoning models remain hidden. Models declaring only a thinking switch
offer Off/On rather than fabricated effort rungs; fixed thinking has one setting.
The exported-configuration audit reports these missing profiles and unavailable
routes in `requires_attention`, separately from wire-compilation failures.
The Default button is separate from the slider's native positions, and both
tick count and labels derive from these controls. Saved defaults resolve to
the declared default rung when known. Specifications:
`tests/model_routing/test_model_reasoning_controls.py`, `tests/thinking_depth_slider.test.cjs`,
`tests/model_routing/test_model_reasoning_settings_browser.py`.

The bounded exact-identity defaults catalogue accepts at most eight
`previous_profiles` per group. Corrections replace only exact prior shipped
profiles; any different owner field or `source=configured` remains authoritative.
Evidence for the maintained depth declarations: [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking),
[GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5),
[DeepSeek thinking](https://api-docs.deepseek.com/guides/thinking_mode/), and
[LongCat's thinking switch](https://longcat.chat/platform/docs/api/chat.html).

Model, Offering, Connection and Deployment accept the bounded optional
`inference_profile` defined in the v2 schema. Resolution follows that order;
reasoning and sampling subfields merge, while features and source use the
most-specific declaration. Explicit unsupported reasoning clears inherited
enabling details. Unknown models and endpoints do not imply capabilities.
When a scope narrows the ladder, inapplicable inherited defaults, disabled
efforts and aliases are removed. Explicit fields that contradict a declared
ladder are rejected at configuration validation. Partial scope overrides remain
valid; binary thinking switches may omit effort positions. Plugin dialect names
are portable metadata, with installation/executability checked by the shared
runtime/control resolver rather than requiring a plugin during import.
`lib/model_routing/inference.py` owns validation and resolution; both pooled
and request-scoped slots receive detached profiles through the same helper.

Catalog `supported_reasoning_levels` (strings or `{effort}` entries) and
`default_reasoning_level` migrate into Offering/Deployment profiles; only
official directory rows contribute Model inference metadata. Legacy model
`thinking_format` becomes Deployment `reasoning.wire_format`. Connection
`thinking_format` and `responses_profile` (`compatible`, `openai`, `codex`)
remain validated authority fields; protocol faces override provider defaults.
For a declared supported effort ladder without an explicit wire format,
Responses Connections supply `reasoning_effort`; this applies to saved
catalogs at binding time. Protocol alone grants no reasoning support and never
overrides an explicit dialect or `none`. Codex catalog imports declare that
wire format when publishing their ladder.
Managed refresh preserves saved inference configuration. Profiles contain no
arbitrary payloads or credentials: effort lists/maps, feature maps and source
strings are bounded, and reasoning budgets are positive, bounded integers.
Executable specification: `tests/model_routing/test_route_inference_contract.py`.

Request ingress preserves three distinct values: absent reasoning selection
uses the destination default; explicit off requires a representable disable
contract; explicit depth is compiled unchanged against the selected route.
Sync/async streaming and non-streaming dispatch accept `thinking_depth` and
default `thinking_enabled` to `None`. Headless task resolution must not turn
an absent switch into off/on or use a model identity as an effort token.
Capability rendering cannot rewrite saved intent while metadata is loading.
Direct SSE errors retain bounded compiler reasons without exposing arbitrary
adapter exception payloads. Specifications: `tests/chat/test_chat_stream_direct.py`,
`tests/tasks_pkg/test_reasoning_retry_lifecycle.py`, and the real-app reasoning browser tests.

The Models settings registry is the shared baseline across providers. At
catalog/install time developer recipes and the exact identities in
`lib/model_info/model_inference_defaults.json` seed missing Model profiles.
The latter migrates maintained protocol contracts out of name-based request
logic; it is packaged, limited to 256 KiB / 2,048 identities and one cached
file revision. Its Alibaba coverage enumerates each Qwen / QwQ / QVQ /
DeepSeek-R1-distill variant explicitly (hybrid `enable_thinking`, thinking-only
`reasoning_split`, or unsupported `none`) from the official 百炼 deep-thinking,
vision, omni, coder, and mt capability matrices — name-based inference stays
disabled. Runtime reads only the installed owner aggregate. A missing
v2 declaration stays `unknown`, never reactivating legacy name inference.
Provider overrides remain in Offering/Connection/Deployment. Narrowing an
effort ladder discards inherited defaults outside that ladder. Legacy direct
library calls without a bound v2 profile retain their compatibility adapter.
Composer choices and final protocol payloads derive from the same resolver;
saved unavailable depths are displayed without becoming selectable rungs.
An identical metadata refresh cannot interrupt an active depth drag.

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

The dispatcher snapshots compiled candidates into its slot pool at
initialization. Every Settings mutation that commits the authority — the
model-routing CAS replace behind the provider template card, ProviderAccess
create/replace/delete, credential secret rotation, and migration commit —
rebuilds that pool in-process, so a saved disable/enable takes effect on
the next pick without a restart; the health sweep, template supply,
catalogue sync, and local-serve registration do the same after their own
authority writes. The rebuild normalizes and indexes the aggregate once
through a single build-local `RouteCandidateCompiler` and resolves each
unique credential secret once, not per slot. In Settings, adding a model
commits the authority immediately through a serialized chain
(`_persistModelRoutingNow`), so a matrix probe on the fresh row never
scopes against a draft the server has not seen; the global Save then
skips the whole-document replace when the staged aggregate already
matches the committed one, and the server-config POST skips the
dispatcher reset when presets/model_defaults are unchanged. Grant and
exclusion toggles still stage for the global Save. Executable
specification: `tests/api/test_api_v1_model_routing_contract.py`
(`DispatcherHotReloadTest`),
`tests/llm_dispatch/test_dispatcher_model_routing_v2.py`,
`tests/api/test_api_v1_integration.py`
(`test_server_config_skips_dispatch_reset_for_unchanged_presets`).

Reload invalidates configured slots in the existing dispatcher; active
request groups keep the same Slot objects, pins, candidate order, health and
in-flight reservations until their owning request disposes them. New groups
compile from the current authority. Settings and health-triggered rebuilds
share this lifecycle, never duplicate temporary slots or seed them from
benchmarks. A failed build discards partial configured slots while retaining
request-owned handles for retry/cleanup. The existing ceiling remains 1,024
live ephemeral slots per process, with no reload history retained.
`tests/llm_dispatch/test_dispatcher_reload_lifecycle.py` covers continuity, isolation,
settlement, rebuild failure and repeated-reload resource bounds.

When admission yields no candidates, `model_route_unavailable` keeps its stable
error kind and includes fixed rejection codes from that same compiler pass.
Codes distinguish missing supply, disabled Offering/Connection/credential,
model or connection authorization, wire exclusions, capability/context/protocol
requirements, pricing policy and health. Counts describe paths rejected at the
first failed gate, not a separate inventory of account permissions. Diagnostics
contain no credential references, endpoints, secrets or upstream response bodies.

## Health and traceability

Configured enablement and runtime health are separate. Route-missing affects a
Deployment; network failures affect a Connection; 401/402 affects a
Credential; 403 affects the Credential×Deployment authorization. Health rows,
probe work, candidate slots, and snapshots are bounded and expire through
their owning lifecycle.

Owner aggregates retain every admitted entity without entity-count or document
byte quotas. Credential grants and connection/exclusion reference lists also
retain all valid identities. Schema, duplicate/foreign references, registration
admission, encrypted credential ownership and atomic revision CAS remain
mandatory. Native authority, backups and migration receipts use blob-capable
storage rather than an 8 MiB refusal; ciphertext migration keeps its separate
existing envelope bound.

Problem: valid owner authority was refused by entity counts and duplicated
8 MiB checks in the repository, SQL storage, native executor, IR validation and
blob access. Decision: keep the complete authority and migration originals,
validate shape and identity, and use the existing owner-bound blob codec.
Model-routing document bodies do not consume the IR metadata-literal quota;
tenant carriers, timestamp/slot validation and revision fences remain.
Alternatives: raising each independent number still leaves other refusal paths;
truncating entities or prior migration evidence changes durable owner state.
A traced in-memory commit of 11,000 Creator rows / 8,943,204 input UTF-8 bytes
took 7.18 seconds with 10,704,345 bytes peak on the test host; input construction
was excluded. This measures normalization/CAS with tracing, not native I/O or
model throughput. Aggregate updates still normalize the owner's complete document.
Verification: eight entity collections exceed their former ceilings; large grants
retain connection/exclusion identities. In-memory, SQLite and native authorities
preserve an 8,943,204-byte input plus the complete >8 MiB migration receipt and
prior document, reject stale CAS, isolate tenant labels and survive restart.
The embedded SDK grows its response buffer to the C ABI's required capacity
without a private byte quota. Non-growing or unrepresentable native capacity
returns, out-of-buffer writes and real allocation failures fail explicitly. It decodes
owned values before reuse and preserves joined close/abandon behavior.
Registration/native-route neighbors passed 74 checks; native model-routing
fault/replay neighbors passed 10 checks, with generated contracts checked for drift.

A request retains all eligible primary and fallback candidates by default and
disposes its handles at teardown. An explicit positive `max_candidates` selects
a caller shortlist without an upper quota. A planned route witness searches the
complete eligible primary set before resolving its credential and mints only
that exact route; it never silently changes route or adds fallback identities.
Capability-group queries share one owner revision without a group-count gate,
and reasoning controls retain every authorized group without tail truncation.
Problem: a 64-candidate default hid valid routes and could misclassify a late
planned witness as changed; separate group/control limits hid configured models.
Decision: retain complete eligible results by default with exact ownership,
registration, capability, egress, ranking and credential checks. Alternatives:
larger constants merely move the failure; changing the witness or discarding
fallbacks violates the caller's route identity. No new pool or global plaintext
cache is introduced.
Verification: 131 primary candidates retain every handle and compiler rank;
a planned witness after the former 64-route prefix matches exactly, while a
changed revision fails before mint/secret resolution. An explicit 100-route
shortlist remains valid, and 70 primary routes precede the eligible fallback.
A 34-group query reads one owner revision. For 8,193 configured unroutable
Offerings the controls retain all 8,193 offering rows plus two aggregate rows;
a traced projection took 4.41 seconds / 14,276,466 peak bytes, excluding input
construction. This measures projection, not inference or browser rendering. Runtime health retains at most 4,096 rows for one
hour (the diagnostic projection returns at most 256). The shared optional
probe lane starts tasks and cells on demand, with no matrix-size or active
worker quota; call validation retains a 120-second timeout and five attempts. A persisted
RouteSnapshot is at most 16 KiB, with at most 32 transitions and 16 degradation
reasons; oldest trace detail is trimmed before persistence, never durable user
state.

Request groups retain compiler rank independently from latency/cost jitter;
eligible sticky routes keep their existing affinity. Candidate construction
does not record failover.

Conversation affinity is exact-or-absent: ``get_preferred_key`` honours only
the entry recorded under the request's own route scope (model/capability) and
never falls back to the legacy unscoped ``conv_id`` alias. That alias records
only "the last key that served this conversation on any provider", so honouring
it let a conversation that once landed on provider X re-pick X's key on a later
turn whose preset preferred provider Y — the wrong provider became sticky (conv
mtxxaf2l). A missing scoped entry falls through to score-based selection, which
honours the preset's provider rank; the unscoped alias survives only as a
read-only diagnostic and is never read by a scoped pick. Each dispatch records only actual transport
invocations in its own bounded trace, so a skipped candidate or a previous
round cannot invent a failed Provider. Sync, async and non-streaming calls
share this ancestry rule without mutating reusable slot snapshots.

Every new turn persists a redacted `RouteSnapshot`: requested structured
identity, Provider preference, actual ProviderAccess/Offering/Deployment/
Connection, credential metadata, wire ID, and transition reasons. Provider or
model failover is appended to the activity timeline without changing the
conversation's saved preference. Old turns are projected with a legacy
snapshot at read time and are not rewritten.

## Detection matrix recommendations

Manual matrix probes are observational: starting, polling, completing, or
restoring a snapshot updates verdicts only, never the routing draft, identity,
enablement switches or credential grants. Explicit detection includes disabled
Offerings and Connections and ungranted cells. It neither requires global
**Save** nor closes Settings; completion never reloads and replaces the draft.
Automatic/scheduled probes retain their enabled-only policy.

**Problem:** the matrix exposed disabled or locally edited request names while
manual planning filtered by saved enablement, yielding an empty selection and
forcing a Save/close/reopen cycle. **Decision:** manual planning is an explicit
mode of the shared planner. The browser sends bounded temporary
`{offering_id, connection_id, wire_model_id}` targets plus ordered credential IDs;
the server validates them against the current owner/provider bundle before
resolving secrets. Only the request name can be draft data: endpoint, protocol,
headers, credentials, model identity and capabilities come from saved authority.
Temporary names are never registered or persisted. Changed credential columns,
unknown/cross-access references and conflicting duplicate names fail closed with
an actionable **Probe not started** message. Existing new-model persistence may
finish before detection, but a failed prerequisite save stops it.
**Alternatives rejected:** disabling Detect prevents testing before enabling;
automatically saving the full draft changes unrelated settings; accepting an
arbitrary endpoint or credential in a diagnostic payload bypasses ownership.
Scheduled identity reconciliation and Apply Recommendations are separate
explicit policies, not manual-probe completion effects.

The grant a credential holds for one ModelRef is
`{creator_id, model_id, excluded_wire_model_ids}`. A green dot on a
(credential × request name) cell means the model grant exists AND this
request name is not in its exclusion list — so two request names sharing a
ModelRef (`glm-5.3` vs `glm-53-meituan`) toggle independently. Toggling a
cell adds/removes that wire's exclusion; the model grant itself is minted
when the first cell is enabled and dropped when the last reachable cell is
disabled. The adjacent probe pip describes that exact credential × request
name. Neither implies the other. Manually added/catalog-listed request
names record `unprobed` instead of fabricating a successful inference.

**Apply Recommendations** is symmetric: a `recommend_disable` verdict
excludes that wire (the model grant drops only when every probed wire of
the model is flagged), and an `ok` verdict on a withheld wire mints the
grant or lifts its exclusion. It never changes probe/identity evidence or
enables an Offering. Global **Save** remains the persistence boundary.
Historical discovery snapshots marked `deployment_enabled=false` stay
non-actionable; fresh probes no longer emit this retired marker.
Executable specifications: `tests/providers/test_provider_model_add.py`,
`tests/auth/test_access_matrix.py`, `tests/model_routing/test_probe_plan_request_names.py`.

A chat probe's `ok` verdict requires protocol-valid generated output, and reasoning output counts as generated output in every dialect: OpenAI `reasoning_content`/`reasoning` message fields, Anthropic `thinking`/`redacted_thinking` content blocks, Responses `reasoning` output items (id/encrypted payload suffices when the summary never materialized), and Anthropic SSE `thinking_delta`/`signature_delta` events. A 2xx carrying neither text nor reasoning stays `invalid_response`. Probe budgets are deliberately not raised to hide this — the validator must recognize reasoning instead. Executable specification: `tests/provider_probe/test_probe_cells.py`.

A final `rate_limited` (HTTP 429) verdict recommends disabling the credential's
model grant by default, including per-minute/per-hour rate limits. Existing
saved 429 snapshots receive the same recommendation on read. Probe retries
still treat any successful attempt as healthy; this policy does not change
transient 429 handling during ordinary dispatch. Applying a 429
recommendation withholds only the flagged request name; it drops the whole
grant solely when every probed wire of the model 429s. Users control access
with the visible toggles, without a successful-probe prerequisite.
Executable specification: `tests/provider_probe/test_probe_rate_limit_defaults.py`.

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

## World catalogue sync

The Models settings page is the registry of models that EXIST, not models a
configured provider currently supplies. At startup
`lib/model_routing/world_catalog.sync_world_model_catalog` compiles every
package provider template and inserts the missing Creator/Model rows into the
owner authority, preferring developer catalog facts to relay recipes. Existing
owner fields are preserved; missing declared profiles are filled. Unique exact
pending identities bind to existing Models without changing Offering/Deployment
IDs, probe verdicts or credential exclusions. New grants preserve only previously
reachable pending wires. Ambiguous/version-suffixed names remain pending.
This repair is idempotent and also covers installed providers; saved scoped
selections retain reasoning controls after binding. Executable specifications:
`tests/model_routing/test_world_catalog_sync.py`, `tests/model_routing/test_model_inheritance.py`.

The Codex OAuth catalogue request carries a `client_version` that upstream
uses to gate newly released models. `lib/oauth/codex_version.py` resolves it
at fetch time from the npm `@openai/codex` `latest` dist-tag (bounded cache,
floor at the version pinned in `lib/oauth/outbound.py`), so new models reach
the catalogue without a Tofu release; a registry outage degrades to the floor
rather than failing the refresh. Executable specification:
`tests/oauth/test_codex_version.py`.

## Daily model intelligence update

`Model Intelligence Update` is enabled on personal scheduler startup at
`30 8 * * *` (08:30 server-local time); reconciliation preserves
user-disabled state. The `model_intelligence_daily` action delegates to
`lib/model_catalog/auto_update.run_model_intelligence_update` with the row
owner's `providers` principal and runs one bounded pass over every canonical
Model at once: (1) refreshes the bounded provider-owned public feeds described
in [provider recipes](PROVIDER_TEMPLATE_RECIPES.md#dynamic-feed), then re-runs
the world catalogue sync so template data shipped or hot-edited
since boot lands without a restart; (2) force-fetches
the public OpenRouter catalogue and CAS-writes `list_pricing` fills under the
same policy as `POST /api/v4/model-intelligence/openrouter-pricing/refresh`
(officially priced or officially covered Models are never rewritten, all-zero
rows never become a $0 list price); (3) force-refreshes the Artificial
Analysis dataset only when the owner has an effective key, warming the
24-hour cache ordinary reads serve. One failing phase is recorded in the
run summary and never masks the others; the summary is counts-only with a
truncated applied-key sample. The server must be running at 08:30; missed
cron occurrences are not backfilled. Tests:
`tests/model_routing/test_model_catalog_auto_update.py`.
## Browser projection

Provider matrices always show a Model header followed by separate request-name
rows, including when the strings are identical. Each request name can be edited
or removed without changing Model identity. Editing preserves credential wire
exclusions and clears probe status. Removing the final name retains an empty
Offering; no Model ID is synthesized into a Deployment or probe cell. Adding a
catalog Model uses an exact endpoint-matched template's declared `request_ids`,
or an explicit, editable default equal to the Model ID when no recipe exists.
The Meituan recipe binds V4.1 Flash identity to its declared V4 Flash wire names.
`tests/providers/test_provider_request_names_browser.py` covers the real Settings flow.


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
  route state. The catalog renders one row per semantic Model: provider
  namespaces (``kimi/kimi-k2.5``) and supply-channel variants
  (``Qwen3_8-27B-Meituan``, FP8 re-registrations, a trailing ``instruct`` tag
  on an otherwise identical registration) are the same Model rented through
  different supply paths, so they collapse into the canonical bare
  registration under its developer, with facts the canonical row lacks
  (pricing, benchmark score) filled from the hidden twins. Near-name twins
  that are genuinely distinct Models never merge: ``gpt-3.5-turbo-instruct``
  (own pricing, 4096 context) is pinned as separate from ``gpt-3.5-turbo``.
  The adjacent Artificial Analysis source is external benchmark
  enrichment keyed by that exact Creator/Model identity; its encrypted
  owner-scoped key, cache status, and score attribution do not create or alter
  a Provider. A future owner-created custom Model uses an explicit editable
  branch. Providers owns the supply projection: each Provider card names its
  canonical Models and may show that Provider's differing wire identifiers as
  aliases. The same exact Model may therefore appear under several Providers.
- The catalog's **Cost × AA Index** Pareto chart plots a Model only when it
  has both an external quality score and an official `list_pricing`
  (3:1 blended input/output USD cost). Scores attach by exact normalized
  identity only: a variant the external source never benchmarked (dated
  snapshot, turbo/highspeed spin, regional supply alias) carries no score
  rather than inheriting the base model's result. Registrations that resolve
  to the same benchmarked model (a bare id and a provider-namespaced id
  matched to the same external row) collapse to a single chart point, keeping
  the priced, canonical registration. Scored-but-unpriced Models are not
  points and are named in the chart note instead. A $0 official price renders
  in a dedicated $0 lane left of the log axis while its tooltip keeps the raw
  value. Markers that
  would overlap (identical or colliding positions) fan out deterministically
  inside the plot bounds; data positions, frontier computation, and tooltips
  are unaffected by the visual spread.
- A Provider can own multiple Credentials. Credential rotation is attempted
  inside the Provider before the dispatcher crosses to another Provider that
  supplies the same Model.
- Connection and wire request identifiers are operational details. They are
  bounded and lazy-rendered only in the Provider manager's connection and route
  diagnostics views; ordinary chat cannot pin either resource.

## Provider model discovery and identity

Model picker groups represent developers; provider chips identify the supplying
service. Unmatched request names appear as **Provider models**, with their
provider shown separately. They require no manual confirmation. The provider
matrix displays their request names and per-credential probe controls/results,
including row, cell and full-provider probes, before an official ModelRef exists.

Manual probes include pending Offerings regardless of enablement and only
publish diagnostic verdicts; they never confirm identity, mint a grant or reload
the settings draft. Daily probes include enabled pending Offerings. Their complete
fresh successful response plus a unique existing catalog match can confirm the
Offering through the scheduled authority transaction. Matching preserves
dated/preview variants and accepts only exact names, known publisher decoration
or confirmed names within that provider. Ambiguous/unmatched names remain
available for diagnosis and probing, but cannot execute chat until their identity
is resolved. A 429, timeout, authorization error or missing model never establishes
identity. Scheduled confirmation preserves switches and existing credential
grants/exclusions; a new ModelRef grant covers only successful names on already
authorized connections. The existing cell/time/concurrency budgets also bound
discovery.

## Daily provider detection

`Daily Provider Detection` is enabled on personal scheduler startup at `0 8 * * *`
(08:00 server-local time); reconciliation preserves user-disabled state. The
`provider_probe_daily` action delegates to `lib/model_routing/probe_automation.py`
with the row owner's `providers` principal. It probes fresh matrices of enabled Offerings, including ungranted cells,
through the existing shared lane (no additional cell pool). Providers with
no enabled Offering are skipped without changing grants.
Each known wire uses its own Deployment's enabled Connection, including its
endpoint, protocol, adapter and headers; credential headers override connection
headers exactly as in generation. Each credential retains its own OAuth mode,
including mixed API-key/subscription columns. A disabled Connection is never
probed through another Connection. Unprobed sibling wires supply no positive reachability evidence. Failure to resolve a stored secret aborts
the run instead of sending an empty key and treating the resulting 401 as
revocation evidence.
Daily runs retain a 30-minute deadline and have no cell-count quota. Cells
start on demand and check cancellation before dispatch; in-flight HTTP calls
retain their normal timeout.
Cancelled runs and worker failures publish an error terminal state with their
partial results; neither reports successful completion nor runs the completion
callback that updates model identity. Actual
probes can incur provider charges, including image generation.
Automatic maintenance consumes only positive evidence from complete fresh runs.
Negative outcomes, including 429/5xx, credential expiry, missing models and local
request/response defects, remain diagnostics; they never delete an existing
model grant or add a persistent wire exclusion. Runtime health/retry state is
separate from durable credential authorization. The matrix retains negative
recommendations for explicit manual review.
Completion counters must match actual result cells before authorization changes.
Fresh successful cells may receive grants or have wire exclusions lifted. A new
grant excludes all known failed or unprobed sibling wires, so one working request
name cannot authorize an entire unverified supply. All changes use one revision
CAS: failed/incomplete/cancelled runs and concurrent edits preserve authority.
A complete all-negative run performs no authorization write. Results remain
visible in the matrix cache and scheduler history.
The server must be running at 08:00; missed cron occurrences are not backfilled.
Tests: `tests/providers/test_provider_probe_automation.py`.
