# Free search reliability

Tofu-search's search capability must work without paid search APIs, subscription
credentials or a hosted service purchase. Public engines and local extraction
remain the core. A caller may configure a self-hosted SearXNG instance; this is
optional, and deployment resources still belong to that caller. Provider
documentation can inform local algorithms without making that provider a
runtime dependency.

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
`CURL_CA_BUNDLE` trust roots. Redirects are not followed. Only HTTP 200 can
complete a search; 202, 204, partial responses and redirects remain failures.
Transport errors retain the common connection/timeout distinctions without
exposing private exception text.

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
