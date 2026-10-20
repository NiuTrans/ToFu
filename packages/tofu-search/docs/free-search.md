# Free search reliability

Tofu-search's search capability must work without paid search APIs, subscription
credentials or a hosted service purchase. Public engines and local extraction
remain the core. A caller may configure a self-hosted SearXNG instance; this is
optional, and deployment resources still belong to that caller. Provider
documentation can inform local algorithms without making that provider a
runtime dependency.

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
