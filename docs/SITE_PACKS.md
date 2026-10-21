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
extract candidates; `json_payload` accepts a same-origin JSON API payload
(extract/research slot) through the work-budget cleaner with a serialized
output cap.

The step set is **closed on purpose**: it is auditable, schema-validatable,
projectable into UI, and every step carries its budget in the contract. A
capability that cannot be expressed in these steps is not a reason to widen the
set ad hoc — it is the documented trigger for a code handler (§3).

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
