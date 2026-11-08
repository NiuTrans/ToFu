# Site packs: site internalization and release tiering

**Status:** P1–P4 landed. The declarative engine, three-layer loading, the
yourprovider release bundle, user-tier management (HTTP API + Settings UI), and
mutation pipelines all run today; the xiaohongshu core pack and the
bundles/yourprovider packs are L1 references. Remaining roadmap items say "will".

**Owners:** `lib/site_packs/` (loader + pipeline interpreter),
[`contracts/site_pack_v1.schema.json`](../contracts/site_pack_v1.schema.json)
(manifest contract), `bundles/` (release-tiered content).

A **site pack** internalizes one site into Tofu: how to authenticate, how to
read, how to search, and — once mutation steps land — how to edit. One pack
file is the single source of truth for a site. Auth-source catalog entries,
browser adapter registrations, model-facing tool descriptions, and Settings UI
rows are all projections of the pack registry; none of them keeps a hand-written
copy.

## 1. Problem

Site- and company-specific logic historically lived in four unrelated shapes:

- `lib/tools/browser.py` hardcoded per-site hints in the `browser_site` tool
  description, leaking them into every user's every agent prompt.
- `lib/auth_sources.py` hardcoded `DEFAULT_SOURCES` in Python.
- `lib/browser/adapters.py` hardcoded built-in adapters (URLs, login flows,
  extraction JS) behind `_register_builtins()`.
- `static/provider_templates/yourprovider.json` shipped company models to everyone.

The opensource export scrubbed these file-by-file (`export_pkg/_sanitize.py`),
which treated symptoms: any new hardcoded reference silently became a scrub
obligation. Meanwhile external users had no supported surface to internalize
their own sites — including intranet systems reachable only from their browser
— without editing source.

## 2. Model: an operation is a bounded step pipeline

Every built-in adapter operation decomposes into a closed set of typed steps.
A pack operation declares `input_schema`, `output_schema`, and `steps: []`; the
single interpreter in `lib/site_packs/` executes them over the existing
`BrowserPage` primitives, inheriting owner/device addressing, grant checks,
redaction, and boundedness for free.

| Step | Semantics | Intrinsic bounds |
|---|---|---|
| `navigate` | URL template with param interpolation | Host must match the pack's `domains` allowlist |
| `ensure_auth` | Login-session guarantee | References an auth source; private hosts gain no SSRF exemption (§6) |
| `fill` / `press` / `click` | Interaction | Selector candidate list; mutation requires a live grant |
| `wait` | Condition wait | `timeout` capped in the schema |
| `paginate` | Scroll / next-page traversal | `max_pages` / `max_scrolls` capped in the schema |
| `research` | Deep-read traversal feeding the payload slot | `max_chars` / `max_scrolls` / `max_pages` capped in the schema |
| `extract` | JS snippet with named args, executed browser-side | `trusted_read` boundary; output size capped |
| `normalize` | Acceptance tail: `search_records`, `detail`, or `json_payload` | Per-field length caps; serialized output budget |
| `try` | Best-effort step group | Aborts the group to a debug log; typed adapter failures always propagate |

URL templates interpolate `{param}` URL-quoted, `{param|raw}` verbatim, and
`{param|floor:N}` as an integer with a lower bound. `fill` values interpolate
verbatim. The normalize kinds share the read pipeline's acceptance owner:
`search_records` and `detail` go through content acceptance with optional
extract candidates; `json_payload` goes through `read_pipeline.read_json_payload`
for source admission, live read policy, the shared work-budget cleaner and the
serialized output cap. That cap counts the normalized JSON result's UTF-8 bytes,
including multibyte text; a character count is not a byte budget.

### Specialized JSON source receipts

Problem: a successful extract could hide a cross-origin fetch redirect, and
normalization accepted `ok` without observed HTTP status or the declared data
field. It could fabricate an empty successful result. Sanitization alone does
not establish where data came from.

Decision: successful `json_payload` inputs contain literal `ok: true`, an integer
2xx `status`, the declared data field, and a `source` object governed by
`content_contract.JSON_READ_SOURCE_SCHEMA` / `JsonReadSource`:

```json
{
  "ok": true,
  "status": 200,
  "source": {
    "document_url": "https://site.example/models",
    "request_url": "https://site.example/api/attributes?name=example",
    "response_url": "https://site.example/api/attributes/current"
  },
  "data": [{"name": "example", "price": 0}]
}
```

The interpreter carries the actual `BrowserPage` envelope's `page.url` alongside
the payload. All source URLs must share that captured document's HTTP(S) origin,
including scheme and effective port, and contain no URL credentials. This also
supports a research-owned temporary tab without borrowing the caller tab's URL.
`BrowserPage.authorize_read_source` rechecks each distinct URL under the live
owner/device/profile, lease, cancellation and declared adapter scope before
cleaning data. Missing or mismatched provenance fails as `source_unverified`;
invalid success/status fails as `fetch_failed`; an absent data field fails as
`invalid_json_payload`. An explicitly present empty value keeps its meaning.
The accepted `{status, data, coverage}` output format and byte budget are unchanged.

Extract implementations must populate URLs from the actual document, request
and `Response.url`, and refuse a foreign response before reading its body.
ModelPlaza pack version 2 implements this with streamed-body cleanup; same-origin
redirects remain supported. Custom `json_payload` producers must emit the receipt
above; the server does not synthesize missing provenance from the requested URL.

Alternatives considered: trusting only `ok` or a guessed URL recreates the gap;
refusing every redirect loses valid same-origin flows; projecting attributes as
generic search cards would discard dedicated fields. Keep one specialized data
admission port at the shared read owner, with the existing cleaner and policies.
Verify the JSON source, byte-budget and domain-scope contracts plus ModelPlaza's
shipped-expression and real-browser redirect tests.

The step set is **closed on purpose**: it is auditable, schema-validatable,
projectable into UI, and every step carries its budget in the contract. A
capability that cannot be expressed in these steps is not a reason to widen the
set ad hoc — it is the documented trigger for a code handler (§3).

### Input admission and execution

Problem: parameter references in research steps and explicit `floor` templates
were not all checked during loading. Runtime integer clamps could also accept a
request and silently reduce it; the xiaohongshu input schema allowed 40 results
while its normalization step supported 30. A valid zero-scroll setting became
four scrolls through a truth-value fallback. Mutable schema/step dictionaries
could change after validation or escape through the public adapter description.

Decision: `_contract.py` reads the machine contract; `_templates.py` shares one
grammar between admission and rendering. `_inputs.py` prepares its existing
defaults and projects every referenced numeric step budget into the operation's
input schema using `allOf`. Authored constraints remain intact, including their
required fields, stricter bounds and additional-property policy. Multiple steps
using one parameter receive the intersection of their declared bounds, including
nested `try` groups. The ordinary adapter input validator refuses invalid values
before acquiring a browser lease, creating a write grant or executing any step.
There is no runtime integer clamp or additional validation loop in the handler.

Pagination admits page counts up to `max_times + 1`; result counts follow the
step's `limit_max`; research and detail budgets follow the existing contract
fields. An omitted parameter retains its established step default. Explicit zero
scrolls remains zero. Explicit URL `{param|floor:N}` retains its documented
transform and now participates in reference validation. No budget is increased,
and no new product tuning setting is introduced. Xiaohongshu's redundant authored
40-result ceiling is removed; its effective limit is derived from the existing
30-result step declaration.

Manifest admission validates inline JSON-schema syntax and the root shapes
accepted by the shared adapter. An incompatible drop-in is rejected during load
instead of breaking later registry projection. Parsed documents, compiled step
handlers, adapter construction and public schema projection own their snapshots.
The loader's current-generation cache and registry replacement still own lifetime;
no cross-generation input cache is added.

Alternatives rejected: interpreter-only refusal can occur after earlier writes;
rewriting supplied values changes the requested operation; independently copied
numeric limits drift between tool descriptions and execution; a new preflight
hook would duplicate the adapter's existing JSON-schema admission boundary.
Verify `tests/site_packs/`, `test_browser_adapter_schema_ownership.py`, the public
site-pack API, native adapter/auth contracts and shipped bundle pipelines.

### Invocation scope and terminal failures

Problem: checking only an input named `url` left rendered navigation/research
templates outside the pack's domain restriction. `ensure_auth` was optional,
and a later redirect could reach a trusted form or extraction step. Optional
waits and `try` groups also treated revoked access and task cancellation as an
ordinary missing element, allowing an earlier payload to be returned as success.

Decision: `invoke_adapter` binds the declared domains through
`BrowserPage.site_scope`. `lib/browser/site_scope.py` owns domain matching for
input admission, page authorization and the shared read pipeline. Scope checks
run before a requested navigation/research target, before each page action on
its refreshed URL, and on returned page/research URLs. They grant no access:
the existing live owner/device policy and exact write-grant checks still apply.
A declared login URL preserves the `login_required` recovery for an off-site
redirect; other scope violations use `access_denied`. No out-of-scope write
grant is created by a page action.

Scopes nest by intersection and restore on every exit, including control-flow
exceptions. A borrowed page remains owned by its workflow; an owned page still
releases through its existing lease cleanup. Cleanup is executable after scope
failure, cancellation or deadline expiry. Authorization, protocol, adapter and
`BrowserOperationStopped` failures propagate through optional steps; ordinary
element/wait failures retain their best-effort behavior and original diagnostics.
If the transport wait ends with an error, the page re-checks its task abort event
and existing workflow deadline before returning that error. Cancellation during
the wait therefore has the same terminal meaning as cancellation before dispatch;
successful action receipts remain available for tab binding and cleanup.

Alternatives rejected: checks in every interpreter branch would duplicate the
browser boundary and omit native code adapters; requiring `ensure_auth` would
still miss later redirects; permanently narrowing a borrowed page would leak
one site's restriction into the next workflow operation. Verify the real-page
domain and terminal-failure tests in `tests/site_packs/` and native/workflow tests
in `tests/browser/`.

### Search result relevance

Problem: `search_records` filtered a pack's extracted cards by the verbatim
query and appended them to the page's generic link inventory. Sites rank their
own results, and a card rarely repeats a compound or CJK query verbatim
(`玉溪 理想家 民宿`), so real results were dropped and the operation failed
with `extraction_incomplete`, while navigation links or incidental hydration
records that happened to contain the query were returned instead.

Decision: `read_pipeline.page_content` treats extracted candidates as the
site's ranked results only when the accepted page URL still carries the query
(percent- or plus-decoded). Then the candidates replace the generic snapshot
records and the captured API/hydration rows, and acceptance runs without the
verbatim filter; the shared cleaner, instruction filter and URL policy still
apply. A page that left its results (tourist feed, login, home) keeps the
verbatim filter, so unrelated feed cards cannot pass as results.

Alternatives rejected: term-splitting the query cannot segment CJK compounds
and still second-guesses the site's ranking; dropping the filter everywhere
would let a redirected feed masquerade as results; a per-pack relevance flag
would be an authored knob for a fact the page URL already states. Verify
`tests/browser/test_browser_search_records.py` and the pack pipeline tests.

Deep research additionally requires the common `research_origin_guard` browser
capability. The extension enforces the requested origin throughout collection;
an origin-stop receipt is terminal even when its final host is another allowed
subdomain. See [research origin continuity](modules/browser_automation.md#research-origin-continuity).

Reads and writes are the same pipeline. `fill`/`click` steps that mutate
remote state carry a side-effect class and pass through the existing approval
mechanism; editing an intranet page is a pipeline with mutation steps and a
read-back verify, not a second system.

## 3. Expression tiers

- **L1 — declarative pack (pure JSON).** Covers every built-in adapter
  operation shipped so far (selector candidates, entry URL templates,
  extraction snippets, pagination, deep research, same-origin JSON APIs).
  Validated against
  `contracts/site_pack_v1.schema.json`.
- **L2 — code handler (will).** The same manifest, but an operation names
  `handler: "module:function"`, discovered through Python entry points (the
  `plugins/` mechanism). Reserved for operations that need arbitrary
  computation: request signing, private protocols, multi-step state machines.
  Not yet implemented — no shipped pack has required it.

L1 and L2 share the manifest schema, the auth-source projection, health
checks, and UI projection. They are one path with two operation
implementations, not two systems. An L2 pack is trusted exactly like an
installed package: installation is authorization. L1 extraction JS runs
browser-side under the existing `trusted_read` boundary and never on the
server.

## 4. Sources and release tiering

Three source layers, later layers overriding by pack id, all hot-reloaded on
read (the `provider_templates` semantics: a malformed drop-in is skipped with
a warning and never takes the registry down):

```
lib/site_packs/data/            core public packs, shipped with the package
bundles/<name>/site_packs/      release-tiered bundles, present per export mode
<config_path>/site_packs/       user-tier drop-ins, no source checkout needed
```

A **bundle** is a release content pack under the top-level `bundles/`
directory — data, tests, and docs for one distribution audience, not a Python
distribution (so it stays out of `packages/` and the workspace):

```
bundles/yourprovider/
  site_packs/{modelplaza,friday}.json
  provider_templates/yourprovider.json
  tests/                        # intranet acceptance tests moved out of tests/
```

`bundles/yourprovider/` is included by the `internal`/`personal` export modes and
excluded as one whole-directory rule by the `opensource` mode
(`export_pkg/_lists.py`). The core tree — code, tests, fixtures, tool
descriptions, docs examples — keeps zero references to company hosts, so the
opensource sanitizer no longer has company references to scrub.

Adjacent loaders adopt the same three-layer shape:

- **Auth sources.** A pack's `auth` section carries `login_url`, cookie
  fields with importance, and risk-note key; `auth.domain` overrides the
  projected catalog domain (an SSO parent domain serving several packs) and
  `auth.project: false` keeps a section ensure-auth-only. The `auth_sources`
  catalog is projected from the pack registry (`DEFAULT_SOURCES` is empty),
  and custom user-declared domains store the same spec shape on their
  `auth_sources.json` row — `source_spec()` remains the single accessor.
- **Provider templates.** `lib/model_info/data/` ← `static/provider_templates/`
  ← `bundles/*/provider_templates/` ← `<config_path>/provider_templates/`
  (new user tier). `yourprovider.json` moves into the bundle.
- **Search gateways.** `bundles/*/search_gateways/*.json` ←
  `<config_path>/search_gateways/*.json`, merged by gateway `id` in
  `lib/search/search_gateway.py`. The core tier ships none, so the Settings →
  搜索 API card stays hidden until a bundle or drop-in describes a gateway;
  its key lives in the credentials vault entry `search_gateway_key`.
- **Tool descriptions.** Model-facing descriptions (e.g. `browser_site`) are
  generated from the registry: a static preamble plus one capability sentence
  per registered pack. An unregistered pack leaves zero trace in prompts, UI,
  and generated docs.

## 5. Bundled classification

- **Core public pack: `xiaohongshu`.** A public site with bounded,
  useful-where-reachable capabilities. Migrated from Python to
  `lib/site_packs/data/xiaohongshu.json` and kept in every distribution as the
  reference L1 pack. "Kept" means kept as clean data — no Python hardcoding
  survives the migration.
- **Core public pack: `douyin`.** Reads the logged-in browser's first page of
  general search cards (author, date, likes, duration, video/note URL) and
  video/note detail (description, counts, hashtags, platform chapter points,
  visible comments). Extraction is scoped to named `data-e2e` regions; it never
  reads messaging/IM regions or exposes signed media URLs. Scrolling did not
  load further cards in an unfocused tab during live validation, so the search
  operation declares no pagination.
- **YourProvider bundle (landed).** ModelPlaza and Friday packs, the example-corp SSO
  auth source (projected by the ModelPlaza pack's `auth.domain`), the
  `yourprovider` provider template, the yourprovider template test suites, and the
  intranet acceptance scripts live under `bundles/yourprovider/`
  (`make test-bundles`). The SSO cookie evidence notes ride along in the
  bundle README.

## 6. Security invariants

- **Default deny.** A pack's `domains` is the navigation allowlist for every
  `navigate` step; redirects re-resolve against it.
- **Two gates stay separate.** Connecting an account never silently grants an
  SSRF exemption: a pack on a private host still requires the explicit
  private-host allowlist entry (today `tofu-search`'s `allow_private_hosts`).
- **Mutation authority.** Mutation steps require an exact live grant and pass
  the approval boundary with their declared side-effect class.
- **Schema-enforced budgets.** Every `max_*` is a hard cap in the contract;
  the interpreter rejects out-of-range values rather than clamping silently.
- **Owner dimension.** Packs and operations carry `owner_user_id`; user-tier
  packs are isolated per owner (enterprise seam, not a single-user shortcut).
- **Trust tiers.** L1 data is untrusted-but-bounded (schema-validated,
  browser-side extraction only); L2 code is trusted-by-installation and says
  so in its manifest.

## 7. Roadmap

- **P1 — contract and engine (landed).** The v1 manifest contract; the
  `lib/site_packs/` loader (three layers, hot reload, skip-with-warning) and
  pipeline interpreter; pack-to-`SiteAdapter` projection so `invoke_adapter`,
  health, and `site_tool.py` keep working unchanged; registry-generated tool
  descriptions; the auth-source catalog projection.
- **P2 — migration (landed).** Xiaohongshu runs as the core L1 pack;
  ModelPlaza/Friday, the example-corp SSO auth source, the yourprovider provider
  template and the intranet acceptance tests live under `bundles/yourprovider/`;
  the opensource export excludes `bundles/` whole and the core tree carries
  no company-host references in the site-pack domain. `DEFAULT_SOURCES` is
  empty; the auth catalog is fully pack-projected.
- **P3 — user tier (landed).** `<config_path>/site_packs/` and
  `<config_path>/provider_templates/` load as the top override layers;
  `routes/api_v1/site_packs.py` validates/installs/removes user packs over
  HTTP; the Settings UI renders the pack registry, a manifest import form,
  and a custom-site connection form (domain + login URL + cookie fields).
- **P4 — mutation steps (landed).** `access: "write"` operations run
  `fill`/`press`/`click` without `trusted_read`, gated by the existing
  `require_access(access='write')` boundary in `invoke_adapter`; `click` is
  rejected in read operations by manifest validation; read-back verify is an
  extract/normalize sequence after the mutation, proven by pipeline tests.
- **L2 — code handlers (open).** Entry-point discovery for operations that
  outgrow the closed step set.
- **Out of scope so far.** Company references in the LLM gateway layer
  (catpaw), Credit-Hub console integration in the browser extension, and
  pricing/model-info tables are separate domains with their own owners —
  they were never part of the site-pack migration.
