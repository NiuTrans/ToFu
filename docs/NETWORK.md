# Outbound network authority

Tofu-owned outbound requests enter the shared network policy automatically.
Application code uses `lib/http_transport/client.py`; libraries that need a
session use the managed client factories in `lib/http_transport/`. Provider
selection, credentials, message retries and business authorization remain with
their domain owners.

## Routing and delivery

`lib/proxy.py` owns configured proxies, bypass rules and eligible route
construction. `lib/network_routes.py` owns reachability, concurrent probe sharing,
preferred routes and recovery. Health belongs to an origin (scheme, host,
effective port) and one concrete route, never to every destination behind a
proxy. An enabled global proxy serves any external origin. Subscription scope
is an explicit request policy and includes subscription plus global entries;
new subscription integrations do not need a new routing implementation.
Existing provider host recognition supplies the default for legacy callers.
Desktop delegation keeps its independent owner/host authorization boundary.

Loopback, registered direct hosts and configured/environment bypasses remain
direct. An explicit per-request proxy pins that request's route. Probe traffic
is the lower transport boundary: it never recursively requests route admission,
forwards application credentials or invokes an arbitrary business POST.
Generic HTTP probes use HEAD on the credential-free origin; the documented
subscription health probe retains its provider-specific policy.

Cold routes share a concurrent probe race and return the first usable path.
Healthy cached routes return immediately while due alternatives recover.
Configuration changes invalidate measurements; unchanged settings preserve
them. Probe results that started before a real request or topology change
cannot overwrite fresher evidence.

Requests and synchronous/asynchronous HTTPX (including MCP httpx2) route at their adapter/transport boundary, after request
preparation and before response-body consumption. This preserves multipart
bodies, redirects, streaming and client hooks. Only proven connection-setup
failures advance to another eligible route. Response status, response read
timeout, write failure and ambiguous disconnect never replay the request.
Each route is attempted once in that request's route plan; existing domain
retry contracts remain outside this layer.

Problem: only known subscription endpoints received route selection and
connection failover, while generic and async requests could repeatedly select
an unavailable proxy. Decision: common route admission at the HTTP transport
boundary, with request-scoped policy and typed failures. Alternatives rejected:
per-channel retries; changing timeout numbers; probing with business requests;
global monkeypatching of requests/httpx; treating every timeout as replay-safe.

## Verification

The executable contract is `tests/proxy/test_unified_network.py`, plus route,
proxy, HTTP-client, subscription-stream and channel suites. Fault injection
covers unavailable routes, one delivered body, multipart preparation, ambiguous
delivery, bypass rules, origin isolation and both async response modes.


## Integration and lifecycle

OAuth helpers in `lib/oauth/http.py` and model request preparation carry explicit
subscription scope, including future provider hosts. The public
`lib.http_transport` facade serves optional plugins. Search 0.12.27 exposes
prepared-Requests, HTTPX-client and anonymous-browser proxy ports; its host
binding uses this same authority, without an independent HTTP failover chain.
Browser contexts select their initial destination's route; their subresources
and redirects retain the browser's context-wide proxy semantics. Command
transport probes reuse the route specifications with their credential policy.
Standalone SDKs and pre-dependency bootstrap retain their own transport lifetime;
they must receive policy through their explicit environment/client composition
ports, rather than importing a running application's mutable state.

Connection pools retain idle connections without limiting active requests.
Configuration generations retire idle proxy pools; active response streams
release theirs after closure. Async cancellation joins route-wait and cleanup
ownership. A cancelled synchronous model attempt cannot send after route probing.
HTTPX CONNECT phase is observed through httpcore tracing: its read timeout is
connection setup, while application-header/body read timeouts remain ambiguous.

Health retention reuses the existing netpath cache policy: 64 idle origins,
24-hour expiry, with in-flight probes protected. No request or probe is refused
to fit that cache. The independent netpath background poller is retired; Settings
projects the common route health. A controlled 1,000-origin replay retained
64 origins / 77,888 traced bytes versus 1,000 / 411,898 before the cache bound.
1,000 warm route selections issued zero probes (median 6.187 microseconds on
this host). These measurements cover routing overhead, not Internet latency.
