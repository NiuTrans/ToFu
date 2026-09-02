# Conversation and Project Brain guidance

## Scope

This package owns owner-scoped project coordination, board/dispatch policy,
summaries, feeds, status, and watches. Turn synchronization lives in
`lib/conversation_sync/`; Git publication lives in `lib/integration_control.py`.
Read `docs/modules/conversations_project_brain.md`.

## Editing rules

- Scope every query, mutation, lease, dependency, write set, summary, and feed
  by owner plus normalized project identity.
- Keep board state and dispatch eligibility in their named policy owners. UI,
  scheduler, and workers project those decisions rather than adding filters.
- Queueing is atomic/idempotent with provenance and deduplication. Lease expiry,
  cooldown, dependency completion, recovery, and stranded work use one state
  vocabulary.
- Cross-conversation awareness is bounded, excludes the current conversation,
  and fails soft without injecting stale or foreign-owner data.
- Storage/isolation failures fail closed for mutations and dispatch. Presence and
  push are ephemeral hints, never durable ownership.
- Git/ref behavior delegates to integration control and its repository contract.

## Verification

Run the smallest board/dispatch/summary/watch test row in the domain map. Add
Sidecar, isolation-fail-closed, integration-control, and API/frontend tests when
the change crosses those boundaries.
