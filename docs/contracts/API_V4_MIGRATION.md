# API v4 mounting and client boundary

The production release serves `/api/v4`. `/api/v1` and `/api/v3` requests
receive HTTP 426 before body parsing, authentication or storage access. Release
policy and wire DTOs come from `contracts/api_v4.yaml`; engineering behavior
belongs to [API_CONTRACT.md](API_CONTRACT.md).

## Handler ownership and mounting

`routes/api_v1/` is the shared handler carrier namespace. It remains live code:
`routes/api_v4/__init__.py` projects every blueprint in `ALL_V1_BLUEPRINTS`
through `lib.api_v4.copy_blueprint_to_v4`. The projection changes the mount
prefix and response boundary, preserving the same view functions, decorators,
services, owner checks and domain policy. Some handler carriers already declare
v4 paths; projection preserves them. Factory-registered and multi-rule handlers
are included, with unique projected endpoint names.

`routes.register_all` omits the legacy blueprint mount on the release major.
Discovered native plugin blueprints pass through
`product_blueprints_for_policy`: legacy product paths use the same projection,
while raw pages and already-native contracts retain their owners.
Route registration is import-safe
with `start_workers=False` and requires neither storage startup nor model calls.
Conversation Sync mounts natively on v4 with its declared generated wire
contract; it preserves domain-specific snapshot, replay and conflict receipts.

Problem: mounting discovered product plugins unchanged exposed retired paths;
copying only deferred routes also lost blueprint-local feature gates and hooks.
Decision: projection preserves registration metadata, guard/error/response/
teardown callbacks and their original policy owners in independent containers.
Alternatives rejected: plugin-specific v4 route copies would duplicate domain
logic; deep-copying callbacks can clone a bound policy object and detach its
live state. Tests exercise native requests, live gates, request owners,
unapproved proposals and legacy/v4 mounting policy.

## Response boundary

`attach_v4_envelope` translates successful JSON into `{data, meta}`.
`meta` carries the correlated request ID and server time. Shared error handling
renders generic v4 failures as `application/problem+json`; explicitly declared
native contract blueprints keep their domain error receipts. SSE and binary
responses retain their streams and content types.

**Problem:** `meta` differs on every response, so no v4 read could be
revalidated and polled reads were re-sent in full when nothing changed. A
2026-10-23 access-log sample spent 47% of its bytes (56.8 MB) on 514
project-board reads, and model-routing re-sent about 190 KB per read.
**Decision:** for a 200 `GET`/`HEAD` JSON success, `translate_v4_envelope`
derives a weak `ETag` from the handler's own representation (before `meta`)
and sets `Cache-Control: private, no-cache`. A matching `If-None-Match` (weak
comparison, `*` included) returns an empty 304, and the browser HTTP cache
supplies the stored body; the transport keeps the default fetch cache mode, so
no client code is involved. Handlers that set their own `ETag`, `Cache-Control`
or cookie keep their semantics; writes, other statuses, raw-success, SSE and
native contract blueprints are untouched. The validator saves transfer,
compression and client parsing, not the handler's read. **Rejected:** hashing
the final body never matches because of `meta`; a transport response cache
would duplicate the browser cache and is excluded by the endpoint-registry
rules; dropping `meta` would break the published envelope. Spec:
`tests/api/test_api_v4_conditional_reads.py`.

**Problem:** helper responses already own their JSON value, but the generic v4
boundary parsed the framework's serialized bytes again, including on a matching
304. **Decision:** `lib/http_transport/json_response.py` retains response-local
provenance for unchanged stock Quart JSON output. The v4 boundary consumes it
only after checking the actual response bytes. A matching validator can return
304 without decoding; a helper-owned flat dict of exact immutable JSON scalars
also supplies its semantic value directly for a body-bearing success. Provider
key ordering and Unicode normalization checks run only when that body is needed.
Borrowed nested graphs, dates/UUIDs, non-string keys, explicit surrogate code
units, custom providers/response classes and arbitrary raw responses retain the
existing decode path. Responses changed by another hook
invalidate provenance. No global cache, codec, envelope field, validator or
admission policy changes. Native/raw-success/error contracts remain separate.

ABBA measurements through real Quart requests and the helper/after-request
pipeline (24 requests per large leg, 128 per small leg) measured CPU median per
request: a 3,920,025-byte string representation's 200 response changed from
37.71/38.12 ms to 29.92/30.11 ms, and its matching 304 from 26.41/26.44 ms to
17.88/17.86 ms. A 20,000-row nested result's matching 304 changed from
36.80/36.78 ms to 21.30/21.22 ms; its 200 remained in the 50–53 ms range.
A 20,000-field scalar result's 304 changed from 10.23/10.13 ms to 7.34/7.30 ms;
its 200 stayed around 13–14 ms. Small responses stayed around 0.38 ms. These are
local response-pipeline measurements, not browser or proxy latency claims.
**Rejected:** retaining borrowed mutable graphs can disagree with already
serialized bytes; copying every graph first increased a nested-result prototype
from 50.4–51.0 ms to 76.8–79.7 ms. Building the final envelope in each helper
would precede the existing status/raw/cache-policy boundary and still require
the original representation for its validator. Specs:
`tests/api/test_api_v4_json_provenance.py`, alongside the conditional-read and
product-boundary tests.

Browser transport and public SDKs unwrap one recognized success envelope for
existing domain consumers. This projection does not rewrite nested domain
`data`/`meta` fields. Raw Response consumers retain body ownership and receive
lazy JSON projection; cancellation and timeouts for parsed bodies last through
body consumption. `/api/v4/openapi.json` remains the raw canonical bootstrap contract.
`/api/openapi.json` describes the complete registered mount, including native
Conversation Sync types and WebSocket upgrades. Embedded schema definitions
retain their constraints and resolve within the OpenAPI document.

## Release configuration

`configure_api_version_policy` validates and freezes application-local policy.
Defaults are `ACTIVE_API_MAJOR=4` and `API_V4_ENABLED=True`. Explicit
`ACTIVE_API_MAJOR=1` or `TOFU_API_MAJOR=1` still selects the legacy assembly lane;
this is supported runtime configuration, not the production test default.
The v4 product projection has its own assembly latch; native Conversation Sync
is independent. Tests of retired-major fencing, configuration and projection
use small explicitly configured apps in `tests/api/test_api_v4_contract.py`.

Auth's v4 public-path policy is derived from the shared handler allowlist.
No browser flag grants authority or changes server response shaping.

## Client paths and verification

Authored browser clients address the released v4 mount directly. Generated
orchestration clients project internal handler paths to the generated release
major in `lib/orchestration/browser_endpoint_contract.py`, before TypeScript
emission. They do not require the transport's stale-link rewrite to operate.
The rewrite remains for old links and external callers; it is not permission
for new browser clients to introduce retired paths.

`tests/frontend/test_frontend_api_contract.py` validates an AST inventory of
retained and typed clients against production route assembly. The matcher uses
Werkzeug converters, not normalized path equality, so generic collections and
slide page suffixes are checked accurately. Dynamic caller/server-owned URLs
are reported separately and need their owning feature's behavioral coverage.
Generated contract drift and response/retirement tests remain separate gates.

Problem: keeping browser DTOs on internal v1 paths made correct behavior depend
on a mutable transport flag and obscured route-audit coverage.
Decision: project the released mount at the browser contract boundary and
validate resolved calls against production registration without starting storage.
Alternatives considered: renaming all handler carriers creates broad churn;
keeping v1 DTOs and teaching tests to rewrite them preserves the migration tax.

Problem: literal v1 source scanning omitted native/factory routes, while default
response documentation described handler payloads rather than the v4 wire.
Decision: derive the delivery index from first-party production assembly and
project metadata through the owning blueprint's declared response boundary.
Native parameter names map to their canonical generated paths. Runtime specs
fail on construction errors; configuration registry duplicate keys fail closed.
Alternatives considered: a second hand-maintained route list drifts; mutating
handler schemas for v4 breaks the explicitly supported legacy assembly lane.

Problem: request aliases and forwarding functions could disappear from the
browser inventory, while native list adapters still admitted retired bare-array
responses after the handlers had switched to item objects.
Decision: bind HTTP forwarders by AST symbol and caller arguments, distinguish
forwarders from unresolved URL consumers, and admit the current list shape.
Arrays returned by domain clients and each client's empty/null/error policy stay
stable, including Orchestration direct domain arrays. Normalized HTTP lists
require item objects. Consumer tests execute retained sources.
Alternatives considered: name-only wrapper matching conflates unrelated helpers;
keeping rolling-deploy parsing indefinitely hides a malformed current response.

Problem: default JSON success metadata hid streams and file media. Generic v4
projection also buffered and rewrote JSON downloads, invalidating their bytes,
Range headers and leased streaming lifecycle.
Decision: handlers declare success media through `media_responses`; file/text
handlers declare `raw_success_response`, read by runtime and OpenAPI. Raw
successes keep their bytes and status; generic failures still become problems.
Mixed JSON/SSE endpoints project only their JSON product result. arXiv progress
uses the shared SSE response seam with its existing proxy/compression headers.
Alternatives considered: MIME or attachment heuristics cannot distinguish every
JSON file from a product result; exempting whole blueprints would also remove
unrelated product envelopes and native failure guarantees.

Problem: JSON error projection discarded a simultaneously declared text media;
Alipay acknowledgement documentation omitted its externally required literals.
Decision: project the JSON error alternative into Problem while preserving
explicit non-JSON alternatives. Alipay declares `success`/`fail`; public shell
HTML declares its actual media and security. Runtime replies remain unchanged.
Alternatives: a whole billing blueprint exemption weakens unrelated product
contracts; replacing the provider acknowledgement breaks its retry protocol.
