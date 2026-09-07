# Decision proposal: `turn.append_settled` duplicate identity error class

**Status:** DECIDED 2026-09-05 — Path ① implemented (owner directive:
push the tofu-db launch forward at full speed; this was the recommended
path with no known consumer). Legacy `_turn_append_settled`
(`lib/storage_sidecar/operations_pkg/_turns_write.py`) now pre-checks the
claimed `turn_id` and raises `database_conflict` before the SQLite
primary key can leak `database_integrity`; the manifest entry and script
markers are removed and the gate asserts convergence. ·
**Author:** differential-test worker · **Evidence:**
`tests/test_tofudb_differential.py` script
`turn append settled ingests external history`

## Background

The semantic differential gate (a hard launch gate in `docs/STORAGE.md`)
replays identical `turn.append_settled` scripts against the legacy SQLite
sidecar and `tofu-db serve`. One pinned divergence remains on the duplicate
identity path:

* **Legacy authority** lets a duplicate `turn_id` reach the SQLite primary
  key and reports the mapped constraint violation `database_integrity`.
* **Tofu-DB** rejects the identity claim up front with
  `database_conflict`.

Everything else about the operation (idempotent success payload, fencing,
event stream) converges exactly. Only the error code on this caller-error
path differs — and error codes are wire-visible, so this needs a human
decision before tofu-db may serve the turn surface in production.

The two codes carry different promises to callers:

* `database_integrity` signals storage-level corruption — not actionable
  by the caller, alarming, and in practice page-ops-worthy.
* `database_conflict` signals a caller-correctable claim conflict
  (duplicate identity, CAS mismatch) — retry with a fresh identity.

A duplicate `turn_id` is unambiguously the second class: the caller
reused an identity that already exists.

## Path ① — Align legacy to `database_conflict` (recommended)

In the legacy turn write path, catch the primary-key constraint violation
for duplicate `turn_id` specifically and map it to `database_conflict`
instead of the generic integrity fallthrough.

* **Blast radius:** one adapter mapping, ~1–3 lines in the legacy
  sidecar's turn write error translation. No storage-format change; no
  tofu-db change.
* **Consumer impact:** any client switching on `database_integrity` for
  this exact case would see `database_conflict` instead. No in-repo
  consumer branches on the duplicate-identity error class; the frontend
  treats both as terminal turn failures. The change only affects an error
  path that already fails the turn either way.
* **Gate effect:** the divergence disappears; the `KNOWN_DIVERGENCES`
  ratchet forces its own removal and the suite stays green — convergence
  is mechanically verified.

## Path ② — Amend tofu-db to emit `database_integrity`

Make tofu-db report the duplicate identity claim as `database_integrity`
to match legacy byte-for-byte.

* **Blast radius:** one error-class branch in
  `packages/tofu-db/src/semantic_executor.rs`.
* **Consumer impact:** preserves the legacy wire exactly, but codifies a
  misleading classification: a routine caller mistake (identity reuse)
  would report as storage corruption on the new authority, and every
  future backend must reproduce the misclassification. This exports an
  accident of the SQLite formulation into the durable contract.
* **When to prefer:** only if a deployed client is discovered that treats
  `database_integrity` on this path as a distinct, meaningful signal.
  None is known today.

## Recommendation

**Path ①.** `database_conflict` is the truthful classification for a
duplicate identity claim; the legacy mapping is a one-line adapter fix
with no known consumer depending on the misclassification, and the
differential ratchet verifies the fix automatically. Path ② permanently
encodes a SQLite artifact as contract.

Until a decision lands, the divergence stays pinned and asserted in
`KNOWN_DIVERGENCES`; whichever path is chosen, the gate must go green by
convergence (①) or by explicit contract amendment plus manifest update
(②), never by deleting the probe.
