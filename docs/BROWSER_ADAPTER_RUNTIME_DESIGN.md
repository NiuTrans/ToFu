# Browser adapter runtime

> Status: implemented, 2026-08-13. This document supersedes the deferred
> browser-permission notes in `SITE_KNOWLEDGE_LAYER_DESIGN.md`.

## Placement decision

Browser automation belongs to Tofu, not `tofu-search`.

- Tofu owns user identity, extension authentication, browser-client routing,
  durable consent, task leases, audit records, and site adapters.
- `tofu-search` stays standalone. It defines an optional, read-only
  `SiteSearchProvider` seam and merges provider results into its normal
  deduplication, fetching, filtering, and reranking pipeline.
- The checked-out OpenCLI project is a design reference only. Tofu does not
  embed its Node runtime or adopt its daemon protocol.

This keeps the authority boundary in the application that knows which user
and browser are involved, while keeping search usable without ChatUI or a
browser extension.

## Runtime contract

`lib.browser.BrowserPage` is the high-level API over Browser Bridge protocol
v2. The extension advertises capabilities on every poll, and Tofu fails before
enqueueing an unsupported operation. Protocol v1 remains usable for legacy
read commands.

The native page surface includes tab lifecycle and navigation, page-state
receipts, stable element references, DOM reads and snapshots, click/fill/key/
select/scroll/wait actions, iframe targeting, structured JavaScript arguments,
network-response metadata observation, upload injection, download waiting,
and screenshots.

Each adapter invocation acquires a browser lease bound to
`user + client + profile + task`. Ephemeral leases close their owned tab;
persistent leases leave user tabs open. Release and timeout always stop active
network captures. A request-bound provider is propagated into tofu-search
worker threads; an unbound provider is inert and can never select the globally
freshest browser.

## Access and consent

The browser policy is deliberately smaller than the old per-site switch UI:

| Operation | Default | Scope |
|---|---|---|
| Read/navigation | allowed | user, with parent-domain deny rules |
| Adapter search/detail | allowed | adapter-declared domains, audited |
| Page mutation | denied until approved once | exact user + domain + browser client + profile |
| Cleanup (close/release) | allowed | the owned lease/tab only |

A manual approval promotes that exact browser/domain pair to a durable write
grant. Later writes, including high-impact actions, run without another final
confirmation until the grant is revoked. Grants do not carry to subdomains,
redirect destinations, another profile, another browser, or another user.

Before every page action Tofu refreshes the current tab URL. The extension
also receives `expectedDomain` and checks it immediately before executing, so
a redirect or page race cannot reuse authority from the preceding origin.
Audit summaries redact cookies, credentials, page bodies, upload data, and
other secret-bearing fields.

Policy API:

- `GET/PUT /api/v1/browser/access`
- `GET /api/v1/browser/adapters`
- `GET /api/v1/browser/status` (protocol, capabilities, profile, leases)

New write grants are accepted only for a browser currently connected to the
authenticated user. The Settings page can add read denials and revoke grants;
it does not offer a blind write-grant button.

## Adapter contract

A `SiteAdapter` declares identity, domains, aliases, login URL, version, risk
notice, and commands. Each `AdapterCommand` declares:

- read or write access;
- JSON-shaped input and output schemas;
- required extension capabilities and timeout;
- one native handler.

Inputs and outputs are validated at the boundary. Detail URLs must stay inside
the adapter manifest domains. Invocation results distinguish invalid input,
extension upgrade, unavailable browser, execution failure, and invalid output.

The first built-ins are:

- Xiaohongshu: authenticated search, paced pagination, and note detail reads.
- ModelPlaza: authenticated search and detail reads from
  `https://api.openai.com/ml/modelPlaza/modelInfo` in the user's SSO session.

Both prefer rendered DOM over reimplementing site request signing. Additional
adapters register with the same contract; future write commands automatically
inherit the durable-grant boundary.

## Search-library seam

`tofu_search.SiteSearchProvider` exposes only `list_sources()`, `bind()`, and
`search()`. It has no tab, cookie, approval, or extension API. Tofu registers a
provider that lists healthy read adapters and invokes them with the request's
bound user/browser identity. Provider results enter the existing URL
normalization and ranking pipeline. Xiaohongshu keeps its pacing, cache, and
risk-control guard while using the same provider first.

Server HTTP/Playwright access remains independent. The private-host allowlist
is an SSRF exception for server fetches only; it neither grants nor blocks a
user browser that can already reach an intranet page. Cookie replay remains a
collapsed compatibility fallback, not the primary setup path.

## Settings model

The primary Search page has three backend-resolved profiles:

- Fast: 3 pages, 30,000 characters per page, no LLM filter or deepening.
- Balanced: 6 pages, 60,000 characters, LLM filtering.
- Deep: 10 pages, 100,000 characters, filtering plus one-hop deepening.

Advanced settings are collapsed. Enabling custom overrides exposes only the
preset-owned page count, character cap, filter, and deepening values; timeout,
download limits, and direct/PDF caps remain advanced common limits. Legacy
concrete values migrate into explicit overrides and keep working. Saving a
plain profile omits preset-owned concrete keys, so later profile changes are
not shadowed by stale values.

The same page shows browser connection/protocol, adapter health, read-deny
exceptions, and durable grants. Offline cookie replay and server private-host
exceptions are nested and explicitly labelled as separate compatibility paths.

## Verification

The contract is covered by browser policy/API, protocol negotiation, lease
cleanup, redirect isolation, adapter schema/audit, request binding, approval
promotion, extension capability parity, search-profile migration, Settings
i18n, and tofu-search provider integration tests. Real-browser acceptance still
requires a logged-in Xiaohongshu session and an internal ModelPlaza SSO session.
