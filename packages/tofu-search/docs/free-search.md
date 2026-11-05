# Free search reliability

Tofu-search's search capability must work without paid search APIs, subscription
credentials or a hosted service purchase. Public engines and local extraction
remain the core. A caller may configure a self-hosted SearXNG instance; this is
optional, and deployment resources still belong to that caller. Provider
documentation can inform local algorithms without making that provider a
runtime dependency.

## Pipeline elapsed-time authority

Problem: wall-clock corrections could exhaust a search pipeline budget early
or give later ranking more time than the original request allowed. Decision:
pipeline phase durations and remaining budgets use `time.monotonic()` throughout;
the existing configured deadlines and partial-evidence policy keep their owners.
`tests/test_pipeline_clock.py` verifies forward/backward clock corrections and
real elapsed exhaustion through the public search entry point. Alternatives:
clamping negative durations hides the display symptom but leaves admission
wrong; restarting each phase's timeout grants a second allowance. The separate
HTTP `deadline_ts` boundary still accepts its documented epoch-based input.

Problem: HTTP retry/socket phases already used a monotonic context, but body
reads and the URL fallback chain compared independent epoch clocks. A wall-clock
correction could skip useful fallbacks or admit work beyond the URL allowance.
Decision: `fetch/request_budget.py` also owns the page scope. HTTP attempts take
the earliest page, enclosing request and local request deadline. An external
epoch `deadline_ts` is converted once at entry; internal page calls never round
trip their allowance through wall time. Body reads use that same request clock.
Scopes reset on success, failure and interruption. A nonpositive URL allowance
disables its own cap and cannot extend an enclosing page's deadline. Page scopes
alone do not change direct shared-session retry behavior.

Fallback admission remains cooperative: an active host/browser provider keeps
its own timeout and is not forcibly interrupted. Alternatives rejected:
recomputing epoch deadlines between hops leaves a correction race; independent
per-phase budgets restart the allowance; mutating shared sessions leaks one
request's deadline into concurrent calls. `test_fetch_elapsed_budget.py` covers
both clock directions, HTTP/body/decoder paths, fallback admission, independent
concurrent pages, disabled caps and scope release after interruption.
Admission checks also run between public readers, live-browser/cookie identity
paths and anonymous HTTP. An exhausted hop cannot initiate another identity
attempt, and a `deadline` diagnosis explains that stop. Existing browser-first
and cookie-first order, received authentication-refusal evidence and successful
late receipts remain intact; deadlines do not grant or broaden access authority.

Problem: ordinary and decompression-recovery responses duplicated body handling.
Malformed length headers or a control interruption could bypass closure, a
failed close could replace the read failure, and the retry skipped declared-size
validation. Decision: `fetch/http_response.py` owns admission, byte/time checks,
buffer release and one close attempt for every received response. The HTTP owner
keeps proxy selection, the single decoding retry and the public `HttpError`
identity. Received HTTP refusal evidence keeps precedence over a body timeout.

The consumer drops incomplete buffers before propagating errors or retrying,
including when a diagnostic receipt retains the exception traceback. A close
failure after a successful read still fails the request. An ordinary close error
is logged without replacing the read failure; an interruption takes precedence
over an ordinary failure, and the first control signal is preserved.
Alternatives rejected: duplicating `finally` blocks leaves validation drift;
catching only `Exception` misses interruption; retaining chunks in an extracted
helper's traceback keeps failed transport data alive. The response-lifecycle
suite verifies both ordinary and recovery paths, original error identity,
response closure and payload release while error receipts remain live.
In a controlled four-chunk failure (64 KiB each), retained consumer payload fell
from 262,144 bytes to zero while both old/new error receipts remained live.
Successful responses retained byte-for-byte output and one close attempt.
This measures consumer-owned transport buffers, not process RSS or payloads
that a separate transport implementation may retain in its own traceback.

## Search phase ownership

Problem: engine authority, optional scoring and result diagnostics were embedded
in the worker orchestration loop. Decision: `search/engine_selection.py` builds
an explicit plan from borrowed request-owned providers and engine functions;
`ranking_phase.py` applies scoring without upgrading uncertain evidence;
`diagnostics.py` projects the final received facts. The orchestrator retains
phase order, worker lifetime and the public result class. Alternatives rejected:
separate pipelines per engine duplicate retrieval/settlement, while a generic
pipeline framework hides the existing early refusals and failure precedence.

The same selection owner plans configured gateway sources and deferred free
engine waves. Request configuration, gateway function and tag prefix are
explicit inputs, preserving caller patches and avoiding an import cycle through
the engine package. The orchestrator retains gateway fallback/parallel/off
policy and provider-body Race-to-N behavior; diagnostics count engines actually
started. The gateway policy and concurrent-import suites verify these seams.

Candidate accumulation has one call-local canonical URL index in
`search/candidates.py`. Problem: separate seen-URL and result maps could diverge
when malformed source metadata failed validation; in-place duplicate merging
could also expose half-accepted corroboration. Decision: publish a new URL only
after its evidence fields validate, and merge duplicates into a temporary row
before updating the original identity held by fetch receipts. Invalid source
contributions remain visible as engine errors and do not poison independent
evidence. Alternatives rejected: reserving a URL before validation loses later
sources; replacing the published object disconnects in-flight fetch receipts;
swallowing malformed fields silently invents source evidence. Provider-owned
rows remain unchanged. The candidate accumulation and race suites verify these
boundaries through the public search entry point.
Provider-crawled bodies pass through the same candidate owner: snippets-only
requests remove them, full requests clip them to the page allowance, and valid
duplicates can enrich a representative without starting another fetch.

## Waiting only for useful work

Problem: an engine wave waited for its slowest engine. On a host whose only
route out is an HTTP proxy, Marginalia's shared public API failed after its
10 s request timeout (the direct fallback fails name resolution at once), Brave
answered HTTP 429 and DDG-API HTTP 202, while Bing and DDG-HTML answered within
1-2 s. Every wave waited 11-13 s. The breaker did not bound this in a
long-running host: after its 120 s cooldown it reset to closed, so the next
three searches each paid the full failure again. Over two days of production
logs (66 searches) the median engine phase was 11.9 s, and only Bing and
DDG-HTML ever returned rows.

Decision: an engine whose most recent request failed runs as a probe. The wave
calls it but stops waiting once the other engines have answered. This covers a
cooled-down breaker, which is half-open (one failed probe re-opens it for
another cooldown, any success closes it), and an engine that failed once before
tripping, so after a restart only the first search pays for a dead engine. A
probe that answers in time contributes normally; an abandoned probe is reported
as `circuit_open` coverage and still settles its breaker in the background.
When every engine in a wave is probing, the wave waits as before, so a fully
recovering host still gets results. A healthy engine that fails once loses at
most the late results of the next search.

Race-to-N previously waited for twice the requested result count of any fetched
page, adjusted by a pool-size heuristic. Fast off-topic pages could satisfy it
while the relevant pages were still downloading, ending the search with nothing
usable to deliver. It now waits for the requested number of pages that
`quality.is_established_evidence` would deliver, the same predicate that later
separates delivered results from uncertain candidates; the wave still ends at
the search deadline when fewer exist.

Alternatives rejected: a shorter engine timeout is a tuned value that also cuts
off slow healthy engines; removing failing engines from the default set loses
them when they recover or on hosts where they work; a fixed fraction of answered
engines is another threshold. `tests/test_recovery_probe_wave.py`, the breaker
cases in `tests/test_engines.py` and `tests/test_orchestrator_race.py` verify the
probe, recovery and evidence-counting boundaries.

## Query length admission

Problem: a slide-research caller sent its whole 3,510-character brief as a
query. As 15.9-16.4 KB request URLs it was refused by every engine:
HTTP 414 from DDG-API, Brave, Marginalia and SearXNG, HTTP 400 from Bing and a
DDG-HTML transport failure. Three refusals in a row opened each engine's circuit
breaker for 120 s, so concurrent valid searches lost those engines as well.

Decision: `perform_web_search` refuses a query longer than `MAX_QUERY_CHARS`
(400 characters, surrounding whitespace excluded) before engine selection. The
empty result carries `_search_diag.reason == 'query_too_long'` with a detail
naming the length and the limit, and the formatter asks for keywords. No engine,
provider or circuit-breaker state is touched. Brave documents `q` as 1-400
characters in its web-search skill reference. Preparing every engine's request
URL offline (the same parameters, `requests` encoding) gives at most 475 bytes
for 400 ASCII characters, 3,680 for CJK and 4,875 for 4-byte characters, well
below the 8 KB request-line defaults of nginx and Apache.

Alternatives rejected: truncation silently changes what is searched; a
per-engine byte budget still sends paragraph-sized queries to some engines and
returns nothing actionable. Brave's further 50-word cap is not enforced: CJK
text has no word delimiters, and the observed failure was length.
`tests/test_query_length_admission.py` verifies refusal before any engine, an
at-limit query reaching the engines, and the formatter's guidance.

## Admitting a free source

**Problem:** a successful HTTP response can contain a structurally valid result
list for another topic. The page title and search field can still echo the
original query. Transport success, parsed result count and query echo therefore
cannot establish useful search coverage.

**Decision:** inspect received result content and preserve failed attempts when
qualifying a new source, endpoint, region or transport. Compare representative
queries on the same declared route and retain the original response evidence.
A host alias that redirects to the same service is not an independent source;
a different serialization of the same listings does not improve coverage.
Keep transport outcomes separate from lexical uncertainty and factual support.
Missing a benchmark's reference URL does not prove a result is irrelevant.

**Alternatives rejected:** changing defaults from one successful request,
treating query echo as a relevance check, or adding retry paths solely because
another implementation lists them. Community implementations inform hypotheses;
their availability claims still need verification on the intended route.

## Source URL fidelity

Problem: Bing wraps organic result addresses in URL-safe Base64, while an
ordinary Base64 decoder can discard `-` and `_` or fail to decode them. Matching
`/ck/a` anywhere in a path also mistakes unrelated source links for redirects.

Decision: unwrap only the exact `/ck/a` path on the known Bing web hosts or a
relative link to that endpoint. Decode one `a1`-prefixed payload with the URL-safe
alphabet and strict UTF-8, once. Preserve the decoded destination's path, query,
fragment, Unicode and percent escapes. Ambiguous parameters, invalid encodings
and non-HTTP destinations are dropped. Ordinary source links remain unchanged;
the fetch layer still owns address admission and redirect checks.

Alternatives rejected: replacing invalid bytes, removing arbitrary payload
characters, recursively decoding nested links, or applying Bing's syntax to
other sites can silently change the cited source. The alphabet and endpoint
boundary are informed by [DDGS's Bing parser](https://github.com/deedy5/ddgs/blob/main/ddgs/engines/bing.py)
and [SearXNG's Bing parser](https://github.com/searxng/searxng/blob/master/searx/engines/bing.py).
`tests/test_bing_url_fidelity.py` checks exact source preservation and delivery
through the public search pipeline. This parser repair adds no network request.

## Anonymous HTTP protocol compatibility

Problem: an HTML search endpoint can return challenges or timeouts on one
transport while completing the same query over another. Retrying the same
HTTP/1.1 request does not necessarily recover useful evidence.

Decision: DDG HTML uses HTTPX with HTTP/2 negotiation through the existing
search envelope's transport port. The query, freshness, parser, engine
selection, throttle, circuit breaker and ordered proxy attempts keep their
existing owners. Other engines retain their current transport. The
[HTTPX protocol contract](https://www.python-httpx.org/http2/) includes HTTP/1.1
negotiation when an origin lacks HTTP/2; selecting HTTP/2 is not proof of the
protocol actually used or of search availability.

The adapter consumes the envelope's explicit proxy mapping, including its
direct-route marker. Environment proxy resolution applies only to an
environment-owned route; HTTPX cannot independently override that selection.
TLS verification stays enabled, including configured `REQUESTS_CA_BUNDLE` /
`CURL_CA_BUNDLE` trust roots for both the destination and an HTTPS proxy's outer
TLS connection. Redirects are not followed. Only HTTP 200 can
complete a search; 202, 204, partial responses and redirects remain failures.
Transport errors retain the common connection/timeout distinctions without
exposing private exception text.

Parallel proxy attempts use the shared context-copying submission owner.
Problem: bare worker submission lost bound request configuration and context.
Decision: preserve the caller's context for every alternate, including parsing
and throttling. Reading process defaults in those workers is rejected because
it can apply another request's policy. `tests/test_proxy_race_context.py`
verifies concurrent alternates against a distinct process default.

HTTPX and its HTTP/2 support load on the first applicable request. One admitted
search attempt owns one transient client and response, which close before
parsing. Decoded HTML is capped at 2 MiB; unsuccessful statuses and explicit
challenge headers do not require body download. Requests share the existing
20-second engine ceiling, shortened by the caller's search budget; no subsequent
attempt starts after that deadline. Chunk checks enforce the remaining budget
between reads. Socket-phase timeouts still are not hard cancellation of a
running network operation. No persistent client cache, cookie jar or new
background worker is created.

Alternatives considered: header-only changes did not consistently recover the
observed failures; adding a browser or native fingerprint library would add
another runtime lifecycle. The HTTP/2 transport preserves the existing engine
and parser rather than adding another search service. The investigation was
informed by [DDGS's HTTP client](https://github.com/deedy5/ddgs/blob/main/ddgs/http_client.py),
but that implementation is not a dependency.

`tests/test_search_http2.py` exercises real HTTPX response lifecycles with
controlled I/O, public search delivery, proxy/CA policy, byte limits, error
classification and cumulative attempt budgets. Live comparisons must retain
actual negotiated protocol, outcomes and failures in alternating fresh
processes. Small paired probes establish compatibility on the measured route,
not broad search-quality superiority.

### Same-engine endpoint recovery

Problem: DDG HTML and Lite can fail independently on the same configured route.
A working Lite response uses a different table layout; treating it as HTML
results loses its evidence.

Decision: the common HTTP envelope admits DDG Lite as an alternate for the
existing `DDG-HTML` engine. Read timeouts, challenges and retryable origin
statuses may advance to it. Proxy connection/authentication errors keep the
existing route policy. A completed result or genuine empty stops recovery.
The alternate replaces the repeated HTML request after HTTP 202; both endpoints
share the query, freshness, throttle, selected proxy and original deadline.
Neither endpoint creates another engine vote, worker pool, cookie session or
paid dependency. The envelope observes one final engine outcome.

The browser and HTTP paths use one parser for both layouts. Lite snippets stop
at the next result so one page cannot inherit another page's evidence. Ads are
excluded, and only DDG's redirect URLs are unwrapped, once: percent escapes and
ordinary destination query parameters must survive.

Alternatives rejected: restarting a budget for Lite, reporting it as an
independent source, repeating completed searches, or adding a separate retry
loop outside the common transport owner. DDG's two no-JS surfaces are described
in the [SearXNG engine reference](https://github.com/searxng/searxng/blob/master/searx/engines/duckduckgo.py).
`tests/test_ddg_endpoint_recovery.py` covers public delivery, source constraints,
failure accounting, deadline exhaustion and snippet/URL fidelity. Package tests
reject unmocked HTTPX network attempts even when product error handling catches
them, so a blocked live request cannot masquerade as an offline regression pass.

## Anonymous page-fetch recovery

Problem: urllib3 classifies a timeout waiting for a proxy's CONNECT response
as a read error. A separate `read=1` ceiling abandoned two failed handshakes
even when the existing total retry allowance and deadline permitted another
attempt. This can happen before origin TLS or any origin GET. Increasing the
connection wait alone did not reliably recover the observed failures. Status
retries could also sleep for an origin's arbitrarily long `Retry-After` value.

Decision: `fetch/request_budget.py` supplies the existing pooled adapter's
retry policy and request-owned socket timeout. Scoped anonymous `do_request`
GETs share the adapter's existing two retries across connection, pre-body read
and status failures: each adapter send permits at most three attempts, not a
separate allowance for each error type. `FETCH_RETRY_LIMIT` is the shared count
owner. Proxy selection, redirects and the existing decoding retry retain their
own bounded dispatches within the same deadline. Successful
responses and non-retryable statuses add no attempt;
body-transfer failures retain their existing failure/decoding behavior. The
configured proxy plan, TLS verification, redirect address checks and byte caps
keep their current owners. No engine, paid service, persistent state, pool or
background worker is added.

One monotonic request context bounds successive attempts to the lesser of
`timeout * 3` and the caller's absolute deadline. Retry clones and redirects
use the remaining time, and no later attempt starts after it expires. An
unaffordable status retry returns the received HTTP status for normal response
closure; affordable `Retry-After` delays are honored. The context resets on
every exit and does not change concurrent calls or direct shared-session users,
including the separate publication-date prefix lookup.

These remain cooperative bounds. Requests/urllib3 socket timeouts cannot
hard-cancel a running read or body drain; body chunk checks and the outer
caller's lifecycle still apply. A persistently failing route can spend more
of the original budget before returning failure, leaving less time for later
fallbacks. No global retry count or URL deadline is increased to hide that
latency tradeoff. See the official
[Requests timeout semantics](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)
and [urllib3 timeout and retry contracts](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html).

Alternatives considered: raising the fixed connect timeout prolonged observed
failures without reliable recovery. Replacing the HTTP client or enabling
HTTP/2 did not consistently recover observed proxy CONNECT failures, which
occur before the origin protocol is negotiated. Mutating shared adapter
settings would mix concurrent callers; an outer retry loop would duplicate
urllib3's counts and response ownership. Tests use owned local HTTP servers to
exercise recovery after two failures, a shared total across mixed error types,
unchanged unscoped session behavior,
status preservation/closure, expired redirects, truncated bodies and concurrent
deadlines. Alternating live probes must retain every failure and phase timing;
successful recovery on one route does not establish overall search superiority.

## Anonymous browser route and response admission

**Problem:** anonymous Playwright contexts ignored the configured search proxy,
so HTTP could load a client-rendered shell through a working route while its
browser recovery used a different route. The pool also extracted answered HTTP
error pages. The upper fetch wrapper already rejects recognized short challenge
text, including an observed Access Denied response; longer or unfamiliar error
bodies cannot rely on that vocabulary guard alone.

**Decision:** the common `ProxyModeManager.fetch_attempt_plan` owns the HTTP and
browser host identity and ordering. `fetch/browser_proxy.py` uses that plan's
first route, including its learned preference and request-bound config, for one
anonymous browser context. Requests resolves environment proxy/scheme/NO_PROXY
for the initial URL. Explicit and learned direct routes preserve their existing
meaning. Redirects/subresources then share the context route: this is not a
per-resource implementation of Requests environment matching. No route race,
retry, browser process, persistent state or timeout is added. Authenticated
fetch/search retain their source-specific proxy; PDF rendering remains separate.

The [Playwright proxy API](https://playwright.dev/python/docs/network#http-proxy)
requires HTTP(S) credentials in separate fields. URL userinfo is decoded there,
never retained in the server address. Invalid/unsupported configurations fail
before context creation with a credential-free error. Unauthenticated SOCKS5
and SOCKS5h map to Chromium SOCKS5; authenticated SOCKS5 fails closed because
[Chromium does not implement it](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md#SOCKSv5-proxy-scheme).

`fetch/document_response.py` observes only navigation for the main frame during
each read. A new request invalidates the previous receipt immediately, including
when no response arrives; a late response for an older request cannot restore it.
Source projection requires the current request's response to be successful and
to remain the same generation through extraction. A later
successful navigation within the original wait can recover an earlier error;
iframe and resource errors do not change admission. Listener retirement precedes
context closure on success, failure and cancellation. This does not replace
challenge/login/readiness checks or prove semantic relevance.

**Alternatives considered:** browser-global proxy options would mix concurrent
callers and authenticated contexts. A new failover loop would duplicate HTTP
route ownership and cost. Text-only error matching misses arbitrary HTTP error
bodies; checking only the first response would reject legitimate later recovery
and accept later failures. Additional waiting or retries would mask the existing
deadline rather than fix route propagation or response admission.

Verification: `test_browser_proxy.py`, `test_fetch_proxy.py`,
`test_rendered_page_source.py`, and opt-in `test_browser_proxy_integration.py`.
The real-browser fixture serves a reserved destination through an owned local
proxy, with/without authentication, and checks error/recovery/subresource cases.
Public availability remains separately measured: propagating a proxy did not
recover the observed public biography page, which answered 403 or timed out.
No general search-quality or site-access improvement is inferred from that run.

## SearXNG response and recovery contract

Problem: accepting the first nonempty response let generic listings prevent
recovery through another configured instance. Treating arbitrary HTTP 200
pages or failed upstream searches as empty successes concealed lost coverage.

Decision: use the existing lexical evidence classifier and site-scope policy
before ending instance recovery. Try the next eligible instance when the first
only supplies uncertain candidates; keep those candidates if no stronger
response arrives. Queries, freshness and configured engine restrictions stay
unchanged. This can spend more of the existing deadline on a weak response;
it adds no instances, worker pools, background refresh or persistent state.
Explicit Web engine selections still cannot widen to other engines.

The [SearXNG API](https://docs.searxng.org/dev/search_api.html) can disable JSON
output. A 403 or 406 without an authoritative challenge header permits one HTML
attempt. A valid JSON response, including an empty list, completes that
instance's attempt and is not repeated as HTML. Rendered search pages are
recognized from the upstream
[results template](https://github.com/searxng/searxng/blob/master/searx/templates/simple/results.html)
and [empty-state structure](https://github.com/searxng/searxng/blob/master/searx/templates/simple/messages/no_results.html),
independently of language and page length. Unknown success-shaped pages,
malformed JSON/records, HTTP 202, challenge pages and empty responses with
upstream failures remain failures.

Partial results survive upstream errors. The
[engine-message structure](https://github.com/searxng/searxng/blob/master/searx/templates/simple/elements/engines_msg.html)
and JSON `unresponsive_engines` mark `upstream_unavailable`; unsuccessful
instance attempts mark `instance_failures`. The package's existing diagnostics
and formatter carry these static warnings through host serial/streaming
previews, cached observations and retained evidence. Raw upstream error text is
not exposed. A snippet about bot challenges remains searchable content.

Alternatives rejected: accepting every HTTP 200 as success; discarding all
results when one upstream fails; changing query terms or widening sources to
manufacture hits; adding paid APIs as a reliability requirement.

## Resource ownership and verification

The configured instance list is snapshotted and deduplicated. Existing attempt
counts remain bounded to four public instances, or the preferred instance plus
two public fallbacks. Each decoded response is capped at 2 MiB and closed on
success, parse failure, oversized bodies and elapsed-budget failure. No next
request starts after the adapter's remaining budget. The outer search deadline
continues to bound caller waiting; Requests socket inactivity/retry behavior
does not constitute hard cancellation of an already running network call.

`tests/test_searxng_recovery.py` provides controlled source-order, partial
coverage, malformed-response, scope and resource specifications.
`tests/test_searxng.py` preserves genuine empty and circuit behavior.
The host's `tests/tasks_pkg/test_search_compact_previews.py` verifies diagnostic
delivery in both execution lanes and retained evidence.

These offline checks establish behavior, not live availability or superiority.
Live qualification must use the effective deployment proxy/browser configuration
and retain per-query failures. A healthy public-instance listing does not prove
that the deployment's egress can access it.

## MCP fetch failure observations

Problem: the common fetcher records why a source was unavailable, but the MCP
adapter discarded that evidence and described browser recovery as generally
successful even when its optional capabilities were absent.

Decision: pass a request-local diagnostic mapping through the existing fetch
call and project only known reason categories and valid HTTP status tokens
into the unavailable text reply. Keep raw exception details and response
headers out of the model-facing message. Unknown categories retain a generic
failure; successful extraction or browser recovery returns the original text.
Tool discovery states the optional browser/enhanced PDF layout requirements,
included local text-PDF capability and access limits.

`fetch/diagnostics.py` owns this safe projection for all delivery adapters.
The native agent diagnostic also sends it in `fetch_failure`, instead of
discarding the cause with worker timing metadata. Otherwise a model receives
the same generic failure for an HTTP refusal and a transient timeout, which
can waste its fixed call budget retrying an unchanged refused source. Known
reason tokens determine any HTTP status; inconsistent raw fields and unknown
causes cannot override that evidence. Internal details and headers stay out.

Alternatives rejected: another network probe to explain a failed request,
automatic retries based on a response header, raw provider-error forwarding,
or an unconditional browser-success promise. The change adds no requests,
workers, cache entries or capabilities. The output remains text under the
existing MCP schema. Real MCP calls in `tests/test_mcp_tool_calls.py` verify
failure propagation, private-detail exclusion and successful recovery;
`tests/test_mcp_server_smoke.py` checks both supported protocol surfaces.
`tests/test_fetch_failure_observation.py` verifies the common public boundary;
the host's native callback tests verify actual tool-result delivery and secret
exclusion. No new network call, retry policy, dependency or resident state is
introduced.
