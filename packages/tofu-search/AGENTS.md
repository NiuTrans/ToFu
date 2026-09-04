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
- Keep network concurrency, response bytes, retries, caches, crawl depth,
  browser processes, and optional native dependencies explicitly bounded.
- Public API changes update the package facade, member tests, host adapter,
  compatibility gate, changelog, and version range together.
- The package builds from this directory. Never restore sibling-checkout or
  editable-install assumptions outside `packages/tofu-search/`.

## Verification

Run `uv run --package tofu-search pytest packages/tofu-search/tests`, then the
root search bridge/runtime and monorepo contract tests for integration changes.
