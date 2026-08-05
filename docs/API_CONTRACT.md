# API Contract — The Frontend ↔ Backend Interface Constitution

> **This is the single source of truth for how the frontend and backend talk.**
> Consumer-facing endpoint documentation lives in [`HEADLESS_API.md`](HEADLESS_API.md)
> (auth, scopes, per-endpoint reference). THIS document defines the *engineering
> contract*: the response envelope, the error taxonomy, the carve-outs, the
> guard tests that enforce the contract, and the checklist for adding an
> endpoint. When the two disagree about an endpoint's *shape*, this file wins.

---

## 1. The five-layer map

Every frontend → backend call passes through five layers. Each layer has ONE
canonical implementation; drift from it is caught by a guard test (§5).

| # | Layer | Canonical implementation | Guard |
|---|-------|--------------------------|-------|
| 1 | Frontend HTTP seam | `static/js/api.js` (`window.Api`, domain-grouped) | `tests/test_frontend_api_isolation.py` |
| 2 | Transport + correlation | `X-Request-ID` minted in `api.js`, honoured by `server.py::_assign_req_id_and_log`, echoed on the response | `test_frontend_api_isolation` + server request-log |
| 3 | Request parsing | `lib/request_parser.py` (`parse_body` / `require_str` / `optional_int` / …; raises `BadRequest` → auto-400) | `tests/test_request_parser.py` |
| 4 | Response envelope | `lib/api_response.py` (`api_ok` / `api_error` / `api_not_found` / …, `sse_response`) | `tests/test_api_contract_drift.py` + `tests/test_api_response_route_conversions.py` |
| 5 | Framework error boundary | `server.py` `@app.errorhandler(404/413/405/500/Exception)` — API paths always get the JSON envelope, never an HTML error page | `tests/test_api_response.py` |

The rule that makes this a *contract* rather than a pile of helpers:
**no layer may be bypassed.** A raw `fetch('/api/...')` outside `api.js`, a
hand-rolled `request.get_json()` dig, or a bare `return jsonify({...})` in a
route is a contract violation — the ratchets in §5 exist to reject it.

---

## 2. The envelope

### 2.1 Success

```json
{ "ok": true, "...payload fields merged at top level...": "..." }
```

Emitted by `api_ok(data, **extras)` (200), `api_created(...)` (201),
`api_no_content()` (204). Payload keys are merged **top-level** (not nested
under `data`), because ~200 existing frontend call sites read named fields.

### 2.2 Error

```json
{ "ok": false, "error": "human readable" , "request_id": "ab12cd-34" }
```

or, for typed errors, an envelope object:

```json
{ "ok": false,
  "error": { "kind": "rate_limited", "message": "…", "detail": {…}, "retryable": true },
  "request_id": "ab12cd-34" }
```

Emitted by `api_error(err, status=…)` and its named wrappers:
`api_bad_request` (400), `api_unauthorized` (401), `api_forbidden` (403),
`api_not_found` (404), `api_method_not_allowed` (405), `api_conflict` (409),
`api_payload_too_large` (413), `api_internal_error` (500, auto-logs traceback),
`api_service_unavailable` (503, sets `Retry-After`).

**Result passthrough:** when a lib-layer function already returned
`{ok, error, ...}` and the route only chooses the HTTP status, use
`api_payload(result, status)` — it preserves the result's top-level shape
(`api_error` would WRONGLY nest it under a single `error` key), keeps a
present `ok`, defaults `ok = status < 400` when absent, and attaches
`request_id` on 4xx/5xx. This is the idiom behind the Project Brain routes'
`if not result.get('ok'): return api_payload(result, 409|400)`.

`error` is a **string** for legacy-compatible sites (the frontend reads
`data.error` as a string at >80 places) and an **envelope dict** when the route
passes one or when the boundary converts an exception via
`lib/error_envelope.from_exception`. The frontend (`api.js::ApiError`)
duck-types both: an object with `kind` + `message` is surfaced as
`err.envelope`, otherwise `err.message` carries the string. New code SHOULD
prefer envelopes for errors the user must *act* on (quota, conflict,
approval-needed) and MAY keep strings for simple validation messages.

### 2.3 Status-code mapping

| Situation | Status | Helper |
|---|---|---|
| Read / success | 200 | `api_ok` |
| Created | 201 | `api_created` |
| Deleted / no body | 204 | `api_no_content` |
| Validation (missing/typed field) | 400 | `api_bad_request` — or raise `request_parser.BadRequest`, auto-converted |
| Authn missing | 401 | `api_unauthorized` |
| Authz refused | 403 | `api_forbidden` |
| Missing resource | 404 | `api_not_found` |
| Version/rev conflict, already-exists, already-running | 409 | `api_conflict` |
| Body too large | 413 | `api_payload_too_large` |
| Uncaught server fault | 500 | `api_internal_error` (or let it propagate — the §5 boundary converts) |
| Transient overload (pool saturated, shed load) | 503 + `Retry-After` | `api_service_unavailable` |

**Do not invent new statuses** for situations the table covers; a 200 with
`{ok:false}` is a contract violation (one deliberate legacy exception:
`api_v1/translate.py` mt-test reports logical failure with 200 —
frozen in the drift baseline, do not copy it).

### 2.4 Correlation

`api.js` mints `X-Request-ID: <page>-<seq>` on every request; the server
prefers the inbound id, stamps it on every log line, echoes it on the
response header, and `api_error` also embeds it as `request_id` in the body.
**When reporting a bug, quote the request_id** — it joins frontend console,
`logs/app.log`, and `logs/error.log` in one grep.

---

## 3. Request parsing

* JSON body → `parse_body()` (sync handlers) / `await async_parse_body()`
  (async handlers). Never raw `request.get_json()` — the shim semantics and
  the empty-body→`{}` contract live in one place.
* Fields → `require_str` / `optional_int` / `require_list` / … — these raise
  `BadRequest(field=…)`, which `@safe_route` and the global boundary convert
  to a 400 carrying the field name. Hand-rolled `"x is required"` returns are
  a violation.
* Query-string **path** args → `decode_proxy_path_arg('path')` (the VS Code
  proxy double-encodes; this seam undoes it, bounded).

---

## 4. Carve-out registry

Some endpoints are **deliberately outside the envelope**. A carve-out is
legal only if it appears here AND in `tests/test_api_contract_drift.py`'s
`CARVE_OUT_FILES` / bare-payload list with a reason.

| What | Where | Why |
|---|---|---|
| OpenAI compat | `routes/compat_openai.py` | Emulates the OpenAI wire protocol; an `ok` key corrupts protocol fidelity for third-party SDKs |
| Anthropic compat | `routes/compat_anthropic.py` | Same — Anthropic protocol shape |
| Desktop-agent bridge | `routes/browser.py`, `routes/_bridge_caller.py`, `routes/desktop.py` | Long-poll protocol parsed by an external binary (the desktop client); shape is locked outside this repo |
| SSE streams | chat stream, agent-run, translate stream, compat streams | `text/event-stream` framing; use `sse_response()` for the canonical headers, never hand-set them |
| Binary / raw payloads | artifact raw/view/export, paper PDF serving, image bytes, podcast audio | Typed bytes with `Content-Disposition` / Range; JSON envelope impossible |
| Multipart uploads | `/api/images/upload`, `/api/paper/upload`, `/api/pdf/parse`, … | FormData in, but the *response* still follows the envelope |
| Bare-array legacy payloads | ALL known instances migrated 2026-08-01 (orchestrations / conversations list / providers-templates / chat active / translate poll-batch / folders ×2 / conv search / chat queue) | Enveloping an array (`{ok, items}`) changes the top-level type — never additive. The retirement path is the **coordinated front+back migration**: backend `api_ok({'items': …})` + the `Api.<domain>` seam unwraps `.items` with an `Array.isArray(d)` fallback for rolling-deploy skew. Unwrap semantics follow the CONSUMER: `|| []` for list UIs (orchestrations/folders/queue/search), **null-preserving** for probes that must distinguish “zero results” from “probe failed” (chat active, translate poll-batch), caller-side unwrap for Response-returning seams (config templates, chat activeResponse). Sites that genuinely cannot migrate register in the drift suite's `CARVE_OUT_SITES` with a reason — never a silent baseline remainder. New endpoints MUST wrap arrays in `api_ok({'items': …})` |

Adding a carve-out requires: (1) a row in this table, (2) an entry in the
drift test with the same reason, (3) a commit message explaining why the
envelope is impossible (not merely inconvenient).

---

## 5. Enforcement (the ratchets)

| Guard | What it rejects |
|---|---|
| `tests/test_frontend_api_isolation.py` | Any `fetch('/api/…')` outside `api.js` (incl. variable-URL bypasses); per-file count may only decrease |
| `tests/test_api_contract_drift.py` | Any ad-hoc `jsonify(` in `routes/**` outside the carve-out registry; per-file count may only decrease; stale baselines must be tightened in the same commit |
| `tests/test_api_contract_*_parity.py` (21 batch suites) | Per-batch wire parity (legacy keys byte-identical; additions only +ok/+error/+request_id) + shipped-source tripwires + front/back coordination anchors for every bare-array migration |
| `tests/test_api_response_route_conversions.py` | The 22 already-converted error sites stay converted (shipped-source tripwire + wire parity) |
| `tests/test_api_response_safe_route_rollout.py` | `@safe_route` rollout state; documents the handlers that must NOT be decorated (side-effecting except blocks) |
| `tests/test_request_parser.py` | `BadRequest` → 400 mapping, typed extractors |

`@safe_route` note: the framework boundary (`server.py`) already converts
uncaught exceptions on `/api/*` to the JSON 500 envelope, so `@safe_route` is
NOT required for correctness. Adopt it on handlers that currently hand-roll a
pure `except Exception → api_internal_error(e)` block **only when the block
has no side effects and no distinct `context=` string** (the rollout suite
pins the gate).

---

## 6. Adding a new endpoint — checklist

**Backend**
1. Route lives in `routes/api_v1/<domain>.py` (the canonical surface; legacy
   `routes/*.py` is maintained, not extended, except UI-only conveniences).
2. `parse_body()` + `require_*`/`optional_*` for input; never `get_json` digs.
3. Return via `api_ok` / `api_created` / `api_payload` / `api_error`
   family; raise `BadRequest` for validation. No bare `jsonify`, no
   hand-rolled 500.
4. Arrays wrapped: `api_ok({'items': …})`, never a bare top-level array.
5. `@api_meta(...)` so `GET /api/openapi.json` stays truthful.
6. Streaming → `sse_response(gen, …)`; binary → document the carve-out (§4).

**Frontend**
1. Add a method on the right `Api.<domain>` in `static/js/api.js`; call it via
   `Api.<domain>.<method>()`. Never `fetch('/api/…')` elsewhere.
2. `onError:'null'` only for genuinely best-effort reads; mutations and
   "user must see the reason" calls must throw `ApiError`.
3. Read typed failures from `err.envelope.kind` (e.g. branch on `409` /
   `overloaded`), not string matching on messages.

---

## 7. Migration workflow (shrinking the legacy baseline)

The drift ratchet freezes today's ad-hoc `jsonify` count per file and only
allows it to shrink. To convert a file:

1. Classify each site: envelope-able (dict payloads, `api_ok(data)` is
   additive) vs bare-array/binary/protocol (carve-out, document it).
   A lib-result passthrough (`return jsonify(result), <status>` where
   `result` came from a `lib.*` call) converts to `api_payload(result,
   <status>)`, NEVER `api_error(result, …)` — the latter nests the whole
   result under `error` and breaks every consumer.
2. Convert; add a parity test in the style of
   `tests/test_api_response_route_conversions.py` — legacy keys must survive
   byte-identical; the ONLY additions allowed are `ok` (always) and
   `request_id`/`error` (error statuses).
3. Update `tests/test_api_contract_drift.py`'s `BASELINE` in the SAME commit
   (the stale-baseline test forces this).
4. Run the ring: `test_api_response*.py`, `test_api_contract_*.py`,
   `test_request_parser.py`, `test_frontend_api_isolation.py`.

---

## 8. Why this shape (scale argument)

Centralized maintenance at ultra-large scale means: **one place to change a
cross-cutting concern, total confidence the change took.** Every cross-cutting
concern here has exactly one seam:

* Change error shape/policy → edit `lib/api_response.py` (446+ sites flow through).
* Change parsing tolerance → edit `lib/request_parser.py`.
* Add a correlation header → edit `api.js::request()` (one chokepoint; the
  `X-Request-ID` rollout touched zero call sites).
* Change overload policy → edit the `server.py` boundary.

The ratchets are what keep it true at 650 handlers and growing: they make the
wrong pattern uncommittable, so review attention is never spent re-litigating
style — only genuine carve-outs need judgment, and those are forced to leave
a written reason.
