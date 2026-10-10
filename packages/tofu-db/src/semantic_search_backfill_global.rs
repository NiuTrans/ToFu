//! Global `turn.search.backfill` authorization-scope enumeration.
//!
//! The legacy command schedules a global rebuild: it cannot be answered with
//! an authenticated-owner-only rebuild, so this module enumerates real
//! tenant/owner authorization scopes from the tenant-global conversation ID
//! claims and durably schedules one owner-scoped rebuild generation per
//! discovered owner (the `semantic_search_backfill` checkpoint contract).
//!
//! Scheduling is explicit: startup never creates checkpoints and never
//! enumerates owners. Each command invocation consumes at most
//! `MAX_GLOBAL_BACKFILL_CLAIM_ROWS_PER_TRANSACTION` claims, publishes a
//! resumable global cursor, and commits the cursor plus every owner-scoped
//! rebuild generation in ONE authority transaction, so a crash can never
//! advance the sweep past an owner whose rebuild was not scheduled.
//!
//! The sweep state lives in the tenant-global scope; payloads carrying an
//! owner or tenant identity are rejected fail-closed by the semantic
//! compiler, so a global rebuild can never be disguised as (or hijacked
//! into) an authenticated-owner rebuild. Completion means rebuilds were
//! durably scheduled, not that projection publication succeeded.

use std::collections::BTreeSet;
use std::io;

use serde_json::json;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::CONVERSATION_ID_CLAIM_NAMESPACE;

const GLOBAL_NAMESPACE: &str = "search_backfill_global_checkpoint_v1";
const GLOBAL_MAGIC: &[u8; 8] = b"TSBG0001";

/// Hard upper bound for one enumeration page, regardless of the request.
pub(crate) const MAX_GLOBAL_BACKFILL_CLAIM_ROWS_PER_TRANSACTION: usize = 64;
/// Hard upper bound for claim source bytes accounted in one page.
pub(crate) const MAX_GLOBAL_BACKFILL_SOURCE_BYTES_PER_TRANSACTION: usize = 8 * 1024 * 1024;
/// Global and owner checkpoint cursors share the owner-module bound.
pub(crate) const MAX_GLOBAL_BACKFILL_CURSOR_BYTES: usize = 1024;

// Owner-scoped rebuild checkpoints are the `semantic_search_backfill`
// contract. That module must stay untouched, so this mirror re-encodes its
// exact wire format; the interop tests below pin both directions (a
// checkpoint staged here loads there and drives `consume_page`).
const OWNER_NAMESPACE: &str = "search_backfill_checkpoint_v1";
const OWNER_MAGIC: &[u8; 8] = b"TSBF0001";

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct GlobalCheckpoint {
    pub generation: u64,
    pub complete: bool,
    pub cursor: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GlobalBackfillRequest {
    pub cursor: Vec<u8>,
    pub max_rows: usize,
    pub max_bytes: usize,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn global_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        GLOBAL_NAMESPACE,
        b"checkpoint",
    )
}

fn encode_global(state: &GlobalCheckpoint) -> io::Result<Vec<u8>> {
    if state.generation == 0 || state.cursor.len() > MAX_GLOBAL_BACKFILL_CURSOR_BYTES {
        return Err(invalid_data(
            "global search backfill checkpoint exceeds its bound",
        ));
    }
    let mut bytes = Vec::with_capacity(17 + state.cursor.len());
    bytes.extend_from_slice(GLOBAL_MAGIC);
    bytes.extend_from_slice(&state.generation.to_be_bytes());
    bytes.push(u8::from(state.complete));
    bytes.extend_from_slice(&state.cursor);
    Ok(bytes)
}

fn decode_global(bytes: &[u8]) -> io::Result<GlobalCheckpoint> {
    if !(17..=17 + MAX_GLOBAL_BACKFILL_CURSOR_BYTES).contains(&bytes.len())
        || &bytes[..8] != GLOBAL_MAGIC
        || bytes[16] > 1
    {
        return Err(invalid_data(
            "global search backfill checkpoint is malformed",
        ));
    }
    let state = GlobalCheckpoint {
        generation: u64::from_be_bytes(bytes[8..16].try_into().unwrap()),
        complete: bytes[16] == 1,
        cursor: bytes[17..].to_vec(),
    };
    if state.generation == 0 {
        return Err(invalid_data(
            "global search backfill checkpoint generation is zero",
        ));
    }
    Ok(state)
}

fn encode_owner(generation: u64) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(17);
    bytes.extend_from_slice(OWNER_MAGIC);
    bytes.extend_from_slice(&generation.to_be_bytes());
    bytes.push(0);
    bytes
}

fn decode_owner_generation(bytes: &[u8]) -> io::Result<(u64, bool)> {
    if !(17..=17 + MAX_GLOBAL_BACKFILL_CURSOR_BYTES).contains(&bytes.len())
        || &bytes[..8] != OWNER_MAGIC
        || bytes[16] > 1
    {
        return Err(invalid_data(
            "owner search backfill checkpoint is malformed",
        ));
    }
    let generation = u64::from_be_bytes(bytes[8..16].try_into().unwrap());
    if generation == 0 {
        return Err(invalid_data(
            "owner search backfill checkpoint generation is zero",
        ));
    }
    Ok((generation, bytes[16] == 1))
}

pub(crate) fn load_global(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<GlobalCheckpoint>> {
    database
        .entity_get(transaction, &global_key(transaction)?)?
        .map(|bytes| decode_global(&bytes))
        .transpose()
}

/// Stage one owner-scoped rebuild generation inside the sweep transaction.
/// An owner whose rebuild is already scheduled (incomplete checkpoint) keeps
/// its cursor; restarting its scan would discard committed page progress.
fn schedule_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    owner_user_id: u64,
) -> io::Result<bool> {
    database.authorize_entity_namespace_for_owner(transaction, owner_user_id, OWNER_NAMESPACE)?;
    let key = EntityKey::new(
        transaction.tenant_id(),
        owner_user_id,
        OWNER_NAMESPACE,
        b"checkpoint",
    )?;
    let generation = match database.entity_get(transaction, &key)? {
        None => 1,
        Some(raw) => {
            let (generation, complete) = decode_owner_generation(&raw)?;
            if !complete {
                return Ok(false);
            }
            generation
                .checked_add(1)
                .ok_or_else(|| invalid_data("owner search backfill generation overflow"))?
        }
    };
    database.entity_put(transaction, key, encode_owner(generation))?;
    Ok(true)
}

/// Consume one bounded claim page and durably schedule the enumerated owner
/// scopes. The caller owns the enclosing transaction (tenant-global scope
/// with identity-claim authorization) and its receipt; the whole transaction
/// must be discarded on error. Retrying an uncommitted page cannot skip an
/// owner: the global cursor and every owner generation share one commit.
pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &GlobalBackfillRequest,
) -> io::Result<Vec<u8>> {
    let tenant_id = transaction.tenant_id();
    let mut state = load_global(database, transaction)?.unwrap_or(GlobalCheckpoint {
        generation: 0,
        complete: false,
        cursor: Vec::new(),
    });
    if request.cursor.is_empty() {
        // An empty cursor explicitly schedules a new sweep from the
        // beginning, superseding any completed or interrupted earlier sweep.
        if state.generation != 0 && (state.complete || !state.cursor.is_empty()) {
            state = GlobalCheckpoint {
                generation: state
                    .generation
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("global search backfill generation overflow"))?,
                complete: false,
                cursor: Vec::new(),
            };
        } else if state.generation == 0 {
            state.generation = 1;
        }
    } else if state.generation == 0 || state.cursor.as_slice() < request.cursor.as_slice() {
        // A cursor ahead of the durable sweep cannot belong to this sweep;
        // fail closed rather than skipping unenumerated scopes.
        return Err(invalid_input("stale global search backfill sweep cursor"));
    } else if state.complete || request.cursor != state.cursor {
        // Lost-ACK recovery: the sweep already advanced past this token, so
        // answer with the durable position instead of re-scheduling owners
        // or failing the receipt-free retry.
        return response(0, 0, &state.cursor, !state.complete);
    }
    let (mut start, end) = EntityKey::prefix_range(
        tenant_id,
        TENANT_GLOBAL_OWNER_ID,
        CONVERSATION_ID_CLAIM_NAMESPACE,
        b"",
    )?;
    if !state.cursor.is_empty() {
        start = EntityKey::new(
            tenant_id,
            TENANT_GLOBAL_OWNER_ID,
            CONVERSATION_ID_CLAIM_NAMESPACE,
            &state.cursor,
        )?
        .exact_range()?
        .1;
    }
    let rows = database.entity_scan(transaction, &start, &end, request.max_rows)?;
    let mut owners = BTreeSet::new();
    let mut cursor = state.cursor.clone();
    let mut scanned = 0usize;
    let mut source_bytes = 0usize;
    for (key, value) in &rows {
        let row_bytes = key
            .key_bytes()
            .len()
            .checked_add(value.len())
            .ok_or_else(|| invalid_data("global search backfill source bytes overflow"))?;
        if source_bytes + row_bytes > request.max_bytes {
            if scanned == 0 {
                return Err(invalid_input(
                    "global search backfill byte bound cannot fit one claim",
                ));
            }
            break;
        }
        let owner: [u8; 8] = value
            .as_slice()
            .try_into()
            .map_err(|_| invalid_data("conversation claim owner is malformed"))?;
        let owner = u64::from_be_bytes(owner);
        if owner == 0 || owner == TENANT_GLOBAL_OWNER_ID {
            return Err(invalid_data("conversation claim owner is out of scope"));
        }
        // The resume token round-trips through the JSON response, so a
        // non-UTF-8 claim identity can never be published as a cursor.
        std::str::from_utf8(key.key_bytes())
            .map_err(|_| invalid_data("conversation claim identity is not UTF-8"))?;
        source_bytes += row_bytes;
        scanned += 1;
        owners.insert(owner);
        cursor = key.key_bytes().to_vec();
    }
    let complete = scanned == rows.len() && rows.len() < request.max_rows;
    let mut scheduled = 0usize;
    for owner in owners {
        scheduled += usize::from(schedule_owner(database, transaction, owner)?);
    }
    let next = GlobalCheckpoint {
        generation: state.generation,
        complete,
        cursor,
    };
    database.entity_put(transaction, global_key(transaction)?, encode_global(&next)?)?;
    response(scanned, scheduled, &next.cursor, !next.complete)
}

fn response(
    scanned: usize,
    scheduled: usize,
    cursor: &[u8],
    remaining: bool,
) -> io::Result<Vec<u8>> {
    let body = json!({
        "scanned": scanned,
        "indexed": scheduled,
        "failed": 0,
        "nextCursor": std::str::from_utf8(cursor)
            .map_err(|_| invalid_data("global search backfill cursor is not UTF-8"))?,
        "remaining": remaining,
    });
    serde_json::to_vec(&body)
        .map_err(|_| io::Error::other("global search backfill response is not encodable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::semantic_executor::search_backfill;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    const TENANT: u64 = 7;

    fn request(cursor: &[u8], max_rows: usize) -> GlobalBackfillRequest {
        GlobalBackfillRequest {
            cursor: cursor.to_vec(),
            max_rows,
            max_bytes: 2_000_000,
        }
    }

    fn seed_claims(database: &mut AuthorityDatabase, tenant: u64, claims: &[(&str, u64)]) {
        let mut transaction = database
            .begin_with_identity_claim_scopes(tenant, TENANT_GLOBAL_OWNER_ID)
            .unwrap();
        for (conversation_id, owner) in claims {
            database
                .entity_put(
                    &mut transaction,
                    EntityKey::new(
                        tenant,
                        TENANT_GLOBAL_OWNER_ID,
                        CONVERSATION_ID_CLAIM_NAMESPACE,
                        conversation_id.as_bytes(),
                    )
                    .unwrap(),
                    owner.to_be_bytes().to_vec(),
                )
                .unwrap();
        }
        database.commit(transaction).unwrap();
    }

    fn page(
        database: &mut AuthorityDatabase,
        request: &GlobalBackfillRequest,
    ) -> io::Result<Vec<u8>> {
        let mut transaction =
            database.begin_with_identity_claim_scopes(TENANT, TENANT_GLOBAL_OWNER_ID)?;
        let response = execute(database, &mut transaction, request)?;
        database.commit(transaction)?;
        Ok(response)
    }

    fn response(
        database: &mut AuthorityDatabase,
        request: &GlobalBackfillRequest,
    ) -> serde_json::Value {
        serde_json::from_slice(&page(database, request).unwrap()).unwrap()
    }

    fn owner_checkpoint(
        database: &AuthorityDatabase,
        tenant: u64,
        owner: u64,
    ) -> Option<search_backfill::Checkpoint> {
        search_backfill::load(database, &mut database.begin(tenant, owner).unwrap()).unwrap()
    }

    fn global_checkpoint(database: &AuthorityDatabase) -> Option<GlobalCheckpoint> {
        load_global(
            database,
            &mut database
                .begin_with_identity_claim_scopes(TENANT, TENANT_GLOBAL_OWNER_ID)
                .unwrap(),
        )
        .unwrap()
    }

    fn setup(vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap()
    }

    #[test]
    fn sweep_enumerates_multiple_owner_scopes_with_resumable_cursor() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let mut database = setup(vfs.clone());
        let claims: Vec<(String, u64)> = (0..5u64)
            .map(|index| (format!("c{index:02}"), 11))
            .chain((5..10u64).map(|index| (format!("c{index:02}"), 12)))
            .collect();
        let claims: Vec<(&str, u64)> = claims.iter().map(|(id, o)| (id.as_str(), *o)).collect();
        seed_claims(&mut database, TENANT, &claims);
        seed_claims(&mut database, 8, &[("other-tenant", 11)]);

        let first = response(&mut database, &request(b"", 4));
        assert_eq!(first["scanned"], 4);
        assert_eq!(first["indexed"], 1);
        assert_eq!(first["nextCursor"], "c03");
        assert_eq!(first["remaining"], true);
        // The page committed atomically: cursor and the owner-11 generation.
        assert_eq!(global_checkpoint(&database).unwrap().cursor, b"c03");
        let owner11 = owner_checkpoint(&database, TENANT, 11).unwrap();
        assert_eq!(owner11.generation, 1);
        assert!(!owner11.complete);
        assert_eq!(owner_checkpoint(&database, TENANT, 12), None);
        assert_eq!(owner_checkpoint(&database, 8, 11), None);

        // A cursor from the sweep's future fails closed; a token the sweep
        // already passed is a lost-ACK retry and reads the durable position.
        assert_eq!(
            page(&mut database, &request(b"c99", 4)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let mut read_only = database
            .begin_with_identity_claim_scopes(TENANT, TENANT_GLOBAL_OWNER_ID)
            .unwrap();
        let retry_bytes = execute(&database, &mut read_only, &request(b"c01", 4)).unwrap();
        drop(read_only);
        let retry: serde_json::Value = serde_json::from_slice(&retry_bytes).unwrap();
        assert_eq!(retry["scanned"], 0);
        assert_eq!(retry["nextCursor"], "c03");
        assert_eq!(retry["remaining"], true);
        assert_eq!(global_checkpoint(&database).unwrap().cursor, b"c03");

        let second = response(&mut database, &request(b"c03", 4));
        assert_eq!(second["scanned"], 4);
        assert_eq!(second["nextCursor"], "c07");
        assert_eq!(second["remaining"], true);
        assert!(owner_checkpoint(&database, TENANT, 12).is_some());

        let last = response(&mut database, &request(b"c07", 4));
        assert_eq!(last["scanned"], 2);
        assert_eq!(last["nextCursor"], "c09");
        assert_eq!(last["remaining"], false);
        assert!(global_checkpoint(&database).unwrap().complete);

        // A completed sweep followed by an empty cursor starts a new sweep.
        // Owner 11 finished its rebuild, so the sweep schedules its next
        // generation; owner 12 is still in flight and is never reset.
        let mut finish = database.begin(TENANT, 11).unwrap();
        let observed = owner_checkpoint(&database, TENANT, 11).unwrap();
        search_backfill::advance(&database, &mut finish, &observed, &observed.cursor, true)
            .unwrap();
        database.commit(finish).unwrap();
        let again = response(&mut database, &request(b"", 64));
        assert_eq!(again["remaining"], false);
        assert_eq!(again["indexed"], 1);
        assert_eq!(global_checkpoint(&database).unwrap().generation, 2);
        assert_eq!(
            owner_checkpoint(&database, TENANT, 11).unwrap().generation,
            2
        );
        assert_eq!(
            owner_checkpoint(&database, TENANT, 12).unwrap().generation,
            1
        );
        // Enumeration never crosses the authenticated tenant boundary.
        assert_eq!(owner_checkpoint(&database, 8, 11), None);
        drop(database);
        vfs.crash().unwrap();
        let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(global_checkpoint(&database).unwrap().complete);
        assert_eq!(
            owner_checkpoint(&database, TENANT, 11).unwrap().generation,
            2
        );
    }

    #[test]
    fn in_flight_owner_rebuild_is_never_reset_by_a_sweep() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let mut database = setup(vfs);
        seed_claims(&mut database, TENANT, &[("c00", 11), ("c01", 12)]);
        // Owner 11 already has an incomplete rebuild with committed progress.
        let mut transaction = database.begin(TENANT, 11).unwrap();
        let scheduled = search_backfill::schedule(&database, &mut transaction).unwrap();
        search_backfill::advance(&database, &mut transaction, &scheduled, b"progress", false)
            .unwrap();
        database.commit(transaction).unwrap();
        let sweep = response(&mut database, &request(b"", 8));
        assert_eq!(sweep["indexed"], 1);
        let owner11 = owner_checkpoint(&database, TENANT, 11).unwrap();
        assert_eq!(owner11.generation, 1);
        assert_eq!(owner11.cursor, b"progress");
        assert_eq!(
            owner_checkpoint(&database, TENANT, 12).unwrap().generation,
            1
        );
    }

    #[test]
    fn scheduled_owner_checkpoint_drives_the_owner_scoped_consumer() {
        use crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE;
        let vfs = Arc::new(DeterministicVfs::new(None));
        let mut database = setup(vfs);
        seed_claims(&mut database, TENANT, &[("c00", 11)]);
        response(&mut database, &request(b"", 8));
        // The wire-format mirror must match `semantic_search_backfill` in
        // both directions: the owner module loads the staged checkpoint and
        // its bounded consumer accepts it as a live rebuild generation.
        let mut transaction = database.begin(TENANT, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(TENANT, 11, CONVERSATION_DOCUMENT_NAMESPACE, b"c00").unwrap(),
                b"{}".to_vec(),
            )
            .unwrap();
        database.commit(transaction).unwrap();
        let progress = search_backfill::consume_page(&mut database, TENANT, 11).unwrap();
        assert_eq!(progress.enqueued, 1);
        assert!(progress.complete);
        assert!(owner_checkpoint(&database, TENANT, 11).unwrap().complete);
    }

    #[test]
    fn malformed_claims_and_checkpoints_fail_closed() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let mut database = setup(vfs);
        seed_claims(&mut database, TENANT, &[("c00", 11)]);
        let mut transaction = database
            .begin_with_identity_claim_scopes(TENANT, TENANT_GLOBAL_OWNER_ID)
            .unwrap();
        for (id, value) in [
            ("short", vec![1; 7]),
            ("zero-owner", 0u64.to_be_bytes().to_vec()),
            (
                "global-owner",
                TENANT_GLOBAL_OWNER_ID.to_be_bytes().to_vec(),
            ),
        ] {
            database
                .entity_put(
                    &mut transaction,
                    EntityKey::new(
                        TENANT,
                        TENANT_GLOBAL_OWNER_ID,
                        CONVERSATION_ID_CLAIM_NAMESPACE,
                        id.as_bytes(),
                    )
                    .unwrap(),
                    value,
                )
                .unwrap();
        }
        database.commit(transaction).unwrap();
        let first = response(&mut database, &request(b"", 1));
        assert_eq!(first["nextCursor"], "c00");
        // Every malformed claim identity aborts the page before publication.
        assert_eq!(
            page(&mut database, &request(b"c00", 8)).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(global_checkpoint(&database).unwrap().cursor, b"c00");
        for bytes in [
            vec![],
            vec![0; 17],
            vec![0; 17 + MAX_GLOBAL_BACKFILL_CURSOR_BYTES + 1],
        ] {
            assert!(decode_global(&bytes).is_err());
        }
        let mut malformed = encode_global(&GlobalCheckpoint {
            generation: 1,
            complete: false,
            cursor: Vec::new(),
        })
        .unwrap();
        malformed[16] = 2;
        assert!(decode_global(&malformed).is_err());
    }

    #[test]
    fn page_commit_faults_never_publish_cursor_without_owner_generations() {
        let claims: &[(&str, u64)] = &[("c00", 11), ("c01", 12)];
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = setup(baseline_vfs.clone());
        seed_claims(&mut baseline, TENANT, claims);
        baseline_vfs.arm_fault(None).unwrap();
        page(&mut baseline, &request(b"", 8)).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        let mut faults = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            faults.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                faults.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
        }
        for (operation_number, action) in faults {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = setup(vfs.clone());
            seed_claims(&mut database, TENANT, claims);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = page(&mut database, &request(b"", 8));
            drop(database);
            vfs.arm_fault(None).unwrap();
            vfs.crash().unwrap();
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            // All-or-nothing: a published sweep cursor requires every
            // enumerated owner generation in the same durable state.
            match global_checkpoint(&database) {
                None => {
                    assert_eq!(owner_checkpoint(&database, TENANT, 11), None);
                    assert_eq!(owner_checkpoint(&database, TENANT, 12), None);
                }
                Some(state) => {
                    assert_eq!(state.cursor, b"c01");
                    assert!(state.complete);
                    assert!(owner_checkpoint(&database, TENANT, 11).is_some());
                    assert!(owner_checkpoint(&database, TENANT, 12).is_some());
                }
            }
            let finished = response(&mut database, &request(b"", 8));
            assert_eq!(finished["remaining"], false);
            assert_eq!(
                owner_checkpoint(&database, TENANT, 11).unwrap().generation,
                1
            );
            assert_eq!(
                owner_checkpoint(&database, TENANT, 12).unwrap().generation,
                1
            );
        }
    }
}
