# Search package guidance

## Scope

This directory owns the independently buildable `tofu-search` distribution:
multi-engine search, bounded fetching/extraction, vertical lookups, citation
verification, and optional MCP delivery. Its public API and provider seams are
the only host integration boundary.

## Editing rules

- Do not import Tofu host `lib`, `routes`, storage, browser, identity, or model
  modules. Host credentials and browser authority arrive through request-owned
  provider interfaces and remain no-ops for standalone callers.
- Keep response bytes, retries, caches, crawl depth,
  browser processes, and optional native dependencies explicitly owned/bounded.
  Independent eligible calls use demand workers; local count gates grant no
  endpoint/billing authority. Explicit request policies keep their owners.
- Public API changes update the package facade, member tests, host adapter,
  compatibility gate, changelog, and version range together.
- The package builds from this directory. Never restore sibling-checkout or
  editable-install assumptions outside `packages/tofu-search/`.

## Verification

`tofu_search/lexical.py` owns subject parsing/tokenization shared by fetch
admission, ranking and excerpts; `passages.py` preserves original source spans.
`fetch/html_fragments.py` selects literal anchors from the sanitized DOM;
`fetch/html_extract.py` owns all text extraction and fidelity rules.
Quality contracts and the offline measurement command live in
`docs/relevance-quality.md`; regression fixtures do not prove live superiority.
Free source admission and metasearch recovery are owned by `docs/free-search.md`.

Run `uv run --package tofu-search pytest packages/tofu-search/tests`, then the
root search bridge/runtime and monorepo contract tests for integration changes.

`tofu_search/_worker_executor.py` is generated from `tofu_workers/executor.py`
by `scripts/gen_packaged_demand_workers.py`; edit the kernel and regenerate,
never the private copy. Check generation, demand/startup/cancel/close tests,
then build a wheel and import it in a fresh process outside the checkout.
MCP uses this same kernel rather than AnyIO's count limiter; its CLI teardown
joins published calls. No runtime import reaches outside this distribution.
