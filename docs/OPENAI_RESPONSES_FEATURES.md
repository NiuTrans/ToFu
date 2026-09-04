# OpenAI Responses feature policy

This project keeps Chat Completions as its canonical internal request shape and
translates at the provider boundary. The features below are emitted only for a
public GPT-5.6 Responses profile; Codex subscription and generic
Responses-compatible providers stay on their proven dialects.

## Provider capability boundary

`protocol: responses` declares only the core Responses wire. It does not by
itself enable public OpenAI extensions. Each Responses provider/face has a
second capability setting:

- `responses_profile: auto` recognizes only `api.openai.com` as `openai`;
  every other host resolves to `compatible`.
- `responses_profile: compatible` sends only the core Responses request
  shape, including `text.format`, normal function tools and streaming.
- `responses_profile: openai` permits the GPT-5.6 public features below.
- Codex OAuth slots resolve to their separate `codex` dialect regardless of
  a stored setting.

The effective profile is copied onto the selected dispatch slot and stamped
on every tool-loop round. A model name can therefore never promote a generic
gateway into the public feature set. Settings → Providers exposes the profile
next to the Responses protocol, and each resolved wire-face tooltip reports
the effective value.

## Request controls

```json
{
  "tools": {
    "nativeExposure": "full",
    "programmaticCalling": "off",
    "toolSearch": "auto"
  },
  "responses": {
    "transport": "sse",
    "reasoningMode": "standard",
    "verbosity": "medium",
    "imageDetail": "auto"
  },
  "orchestration": {
    "multiAgent": "off",
    "maxConcurrentAgents": 3
  }
}
```

Allowed alternatives are:

- `tools.toolSearch`: `off | auto | native | local`
- `responses.transport`: `sse | websocket`
- `responses.reasoningMode`: `standard | pro`
- `responses.verbosity`: `low | medium | high`
- `responses.imageDetail`: `auto | original`
- `orchestration.multiAgent`: `off | auto | read_only`
- `orchestration.maxConcurrentAgents`: integer `1..8`

These controls are available in Settings → Advanced and are carried through
the same server-authoritative conversation-config resolver used by normal,
regenerate, continue and recovered turns.

`response_format` is translated to Responses `text.format`. Pro mode,
verbosity, original image detail and the privacy-preserving
`safety_identifier` are added at the same allowlisted boundary.

## Tool Search: frontend selection is authoritative

Tool Search never removes a tool from the server-owned enabled catalog. It
changes only which native schemas are presented on the wire:

1. Tools explicitly supplied by an API caller are all pinned/direct.
2. `ToolSpec.discovery_policy=eager` tools and the current task's MCP active
   set are pinned/direct. `searchable` tools remain executable from the enabled
   catalog even when their schema is deferred.
3. In local mode, the stable `search_tools` and `execute_tools` pair is direct.
   Discovered schemas are returned in the trailing search result without
   changing the tools array; the model executes the exact returned name through
   `execute_tools`. The protocol-only outer call is hidden in chat while its
   real child tools retain their normal result and approval UI.
4. A forced `tool_choice` function is always hoisted to the direct surface.
5. Only the residual catalog is marked `defer_loading` and grouped into stable
   namespaces of at most ten functions. Tool Search activates only with at
   least 16 functions and at least eight deferrable functions.

The direct/deferred policy is derived from the current request. Toolbar, MCP,
Project Brain and Swarm changes therefore update the effective Responses tools
array on the next model turn without an apply step.

Authorization never consults this direct/deferred projection or a search
receipt. It checks the immutable task-level enabled catalog.

## WebSocket mode

`responses.transport=websocket` uses a task-local persistent connection for a
sync agent tool loop. Later rounds send `previous_response_id` plus only new
application-authored input (normally `function_call_output`). Connections are
never shared across tasks, credentials, or upstream URLs.

If the socket cannot open before `response.create` is sent, the same request
falls back to SSE. A failure after send is treated as a retryable transport
failure; it is not silently replayed on SSE inside the attempt. A terminal
assistant response closes the task-local connection.

## Composable Multi-agent routing

`orchestration.multiAgent=read_only` forces the analysis lane; `auto` enables
it only for a first-round task with independent complex workstreams. On a
verified public GPT-5.6 Responses face, the lane emits the native beta
request/header and an explicit analysis-only developer constraint. Every other
tool-capable model uses Tofu's local `spawn_agents` backend when that tool is in
the task-owned authority catalog. Native and local workers share the same
read-only dispatch filter, per-wave concurrency ceiling, and root-only final
answer rule.

Multi-agent and Programmatic Tool Calling are independent. A request may use
agents to partition independent workstreams while the root or a local worker
uses bounded PTC inside one workstream. Native control-plane and local
`spawn_agents` tools are alternatives for the same lane and are never exposed
together.

Native tool-search, multi-agent, hosted-tool and unknown Responses output items
are retained when replay-safe. Unknown event/item types are logged instead of
being silently discarded. Multi-agent mode omits explicit server compaction,
which the beta API does not support.

## Server compaction and local fallback

For a public GPT-5.6 `responses_profile=openai` route, the economic working-set
threshold is emitted as Responses `context_management` and server compaction is
the primary L2 mechanism. This is the only layer that can trigger from the
provider's complete rendered token count, including opaque persisted reasoning.
Returned compaction items are stored and replayed; history before the newest
item is omitted while current developer instructions remain.

Local L1 continues every round. Local L2, manual `/compact`, pre-compaction
archives and the reactive prompt-too-long path are not removed: automatic local
L2 simply waits for the hard model-window threshold while a verified native
route owns the 128K economic trigger. A mixed/unknown provider pool stays local.
Native Multi-agent rounds also stay local because that beta rejects explicit
server compaction.

The Codex subscription profile never receives public `context_management`.
Its local L2 remains primary. For both public and subscription Responses,
provider `reasoning_tokens` are carried once into the next local count only when
the returned encrypted reasoning item is actually replayed; the small displayed
reasoning summary is not treated as the complete hidden reasoning state.

## Operational prerequisite

Native Programmatic Tool Calling, native Tool Search, Pro mode, WebSocket and
native Multi-agent require a real public Responses provider. Provider-neutral
PTC and Multi-agent semantics do not: other tool-capable faces receive the
local `execute_tools` ToolScript and `spawn_agents` backends under the same
authority and budget contracts. Codex subscription and compatible Responses
profiles remain deliberately excluded from public-only fields.

Recommended rollout: first select `protocol=responses` with
`responses_profile=compatible` and run the existing access-matrix probe. After
the core request returns verified generated text, change the profile to
`openai` only for an official endpoint or a gateway whose advanced-field
support has been independently confirmed. Probe each native capability in
isolation before rollout; after those capability probes pass, PTC and
Multi-agent may be composed by task shape. Tool Search may remain `auto`
because frontend/API-selected tools are pinned direct at the protocol boundary.
If a request-shape 400/404/422 explicitly names a rejected native PTC or
Multi-agent field, the transport retry projects only that lane onto its local
backend. Unrelated request errors are never masked by this fallback.
