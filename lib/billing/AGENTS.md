# Billing guidance

## Scope

This package owns wallet, reservation/settlement, ledger, and cleanup policy.
Canonical model rates live in `lib/pricing/`. Read
`docs/modules/auth_providers_billing.md`.

`payments/stripe_events.py` owns signed-event admission and normalized payment
facts without storage access. `payments/stripe.py` owns checkout/provider IO and
the shared record/settle handoff. Add event variants at the admission owner;
preserve PaymentIntent deduplication and unpaid/invalid no-write specifications.

`payments/alipay_protocol.py` owns RSA2 request signing and notification admission
without storage IO. `payments/alipay.py` joins those facts to an existing local
order before shared settlement; never infer a wallet from callback metadata.
`payments/amounts.py` owns exact payment unit conversion, using the existing credit
unit. Verify pending-order durability, merchant/amount binding, frozen rates,
concurrency and lost ACKs through SQLite and both native application transports.

## Editing rules

- Every operation is owner/account scoped and uses stable idempotency keys.
  Unknown foreign resources fail without revealing their existence.
- Reservation, request outcome, ledger append, wallet update, refund/release,
  and emitted event obey one atomic state transition vocabulary.
- Use canonical decimal/rate and usage calculations. Do not copy price tables or
  infer billable usage from UI/provider text.
- Lost acknowledgement, retry, timeout, cancellation, partial provider usage,
  and janitor recovery must not double-charge or mint balance.
- Keep ledger durability distinct from bounded derived summaries/caches. Never
  delete financial evidence as reconstructible cleanup.
- Redact credentials and sensitive provider data while retaining auditable
  request/usage/idempotency attribution.

## Verification

Run focused billing, cost, pricing, idempotency, rollback, settlement, and
janitor tests, plus the Sidecar transaction cases for changed ledger operations.

`cost` adapts canonical dynamic prices; `cost_types` carries the shared integer
result. `native_cost` owns frozen per-round Rust credit validation, child fee
grants and transfer. Its per-projection producer/round index only selects candidate
witnesses; exact evidence and owner checks still authorize each row. It never
reprices native fees or retains an authorization cache between projections.
Verify account coverage/unknown refusal, signed overflow,
zero reserve release and real native wallet lost-ACK idempotency; never reprice.
`native_admission` protects a billed root's positive hold before provider entry
through the existing immutable ledger. Wallet transactions serialize admission
against standalone release; terminal settlement remains the invoice owner.
Verify cold missing-result recovery, concurrent refund/admission and lost ACKs.
Native apiRounds.taskId identifies the executor that incurred each quote.
Direct requests share `reservation_admission` and the physical transport port;
`request_settlement` freezes complete invoices in the existing immutable ledger.
Verify JSON/SSE, all wallet backends, unsent/unknown/zero distinctions, strict
cache evidence, lost ACKs and SIGKILL. Invoice schema belongs to tofudb_ir_v1;
unknown protected holds never authorize refund, repricing or provider replay.
Continue's exact inherited prefix still covers cumulative tokens, but only fresh
owned rows fund this task's capsule. Reject missing/same/foreign provenance before
provider entry; verify public billed resume and cold wallet recovery with lost ACK.
Recovered actor fee transfer retains original producers and payers, validated
against the acknowledged account prefix. Verify multi-generation, mutation and
wallet lost-ACK cases in test_recovered_actor_fee_transfer / fee_wallet.
`native_actor_fees` exports that acknowledged grant into the existing private
completion CAS; it never prices, settles or admits execution. Verify native Flow,
cold reads and lost ACKs; absent evidence is distinct from a proven zero charge.
`native_aggregate_fees` proves whole-cut coverage with unique original producer
rounds; conflicting payers/quotes or uncovered root spend refuse. It describes a
candidate only; atomic source retirement remains mandatory. Explicit cost-only
Goal launches retain original credits without a wallet capsule; paid launches
still require the original hold. Verify mixed actors, generations and overflow.

native_settlement owns the closed-schema terminal capsule and wallet recovery
over existing task-result metadata. Verify fresh-process/lost-ACK, missing-native
evidence, opaque owner mapping and immutable native terminal retries.

`project` describes a paid checkpoint invoice without freezing the running task;
`freeze` alone owns terminal immutability. The capsule schema comes from the
checkpoint contract/generated content schema. Verify later paid rounds, exact
restored quotes and original wallet/reservation binding; a checkpoint candidate
alone never authorizes settlement or refund.

Native pending handoff freezes the same capsule in the retired journal header.
`recover` reads it through the selected read-only invoice port when no terminal
result capsule exists. Existing nonretired journals refuse refunds; passive
copies cannot settle. Verify actual native janitor, repeated handoff, missing
result projection, wallet ownership and lost-ACK idempotency.

`terminal_metadata` preserves failed/aborted/interrupted task outcomes when native
usage is unknown, with no invoice and an explicit incomplete-usage marker.
It grants no refund; `recover` retains the native-required hold. Verify
test_native_terminal_projection and the genuine Goal source crash/lost-ACK case.
