//! Transactional, owner-scoped generation numbering for compact postings.
//! Full generation digests are the identity authority. Committed IDs never
//! repeat until an owner-wide projection wipe also retires every posting.
//! Callers charge returned byte deltas and publish builds in this transaction.

use super::{invalid_data, key};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use std::io;

pub(super) const NAMESPACE: &str = "search_generation_ids_v1";
const COUNTER_KEY: &[u8] = b"next";

fn decode_id(bytes: &[u8]) -> io::Result<u64> {
    let array: [u8; 8] = bytes.try_into().map_err(|_| invalid_data("invalid search generation ID"))?;
    let value = u64::from_be_bytes(array);
    if value == 0 { return Err(invalid_data("zero search generation ID")); }
    Ok(value)
}

pub(super) fn lookup(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
) -> io::Result<Option<u64>> {
    let stored_key = key(transaction, NAMESPACE, digest)?;
    database.entity_get(transaction, &stored_key)?.as_deref().map(decode_id).transpose()
}

#[cfg(test)]
pub(super) fn retained_counter_bytes(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction) -> io::Result<u64> {
    let counter_key = key(transaction, NAMESPACE, COUNTER_KEY)?;
    match database.entity_get(transaction, &counter_key)? {
        Some(bytes) => { decode_id(&bytes)?; Ok((counter_key.key_bytes().len() + bytes.len()) as u64) },
        None => Ok(0),
    }
}

/// Return (stable ID, newly charged bytes). Both dictionary and allocator
/// metadata are counted exactly; an existing assignment costs no new bytes.
/// No writes are staged when the supplied remaining budget is insufficient.
pub(super) fn allocate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
    remaining_bytes: u64,
) -> io::Result<(u64, u64)> {
    let counter_key = key(transaction, NAMESPACE, COUNTER_KEY)?;
    let counter = database.entity_get(transaction, &counter_key)?;
    let high_water = counter.as_deref().map(decode_id).transpose()?;
    if let Some(id) = lookup(database, transaction, digest)? {
        if !high_water.is_some_and(|water| id <= water) {
            return Err(invalid_data("search generation ID exceeds allocator state"));
        }
        return Ok((id, 0));
    }
    let id = high_water.unwrap_or(0).checked_add(1).ok_or_else(|| {
        io::Error::new(io::ErrorKind::OutOfMemory, "search generation ID space exhausted")
    })?;
    let stored_key = key(transaction, NAMESPACE, digest)?;
    let added_bytes = (stored_key.key_bytes().len() + 8
        + if counter.is_none() { counter_key.key_bytes().len() + 8 } else { 0 }) as u64;
    if added_bytes > remaining_bytes {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory, "search generation dictionary exceeds owner budget"));
    }
    database.entity_put(transaction, stored_key, id.to_be_bytes().to_vec())?;
    database.entity_put(transaction, counter_key, id.to_be_bytes().to_vec())?;
    Ok((id, added_bytes))
}

/// Retire a generation assignment only after it is no longer published.
/// Leftover postings can still contain the ID as a false candidate; retaining
/// the counter prevents those postings from ever naming a future generation.
pub(super) fn retire(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
) -> io::Result<u64> {
    let stored_key = key(transaction, NAMESPACE, digest)?;
    let Some(bytes) = database.entity_get(transaction, &stored_key)? else { return Ok(0); };
    decode_id(&bytes)?;
    let removed_bytes = (stored_key.key_bytes().len() + bytes.len()) as u64;
    database.entity_delete(transaction, stored_key)?;
    Ok(removed_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;
    use tempfile::tempdir;

    #[test]
    fn complete_digest_identity_is_scoped_durable_and_never_reused() {
        let directory = tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // Deliberate 64-bit fingerprint collision, distinct full identities.
        let first = [9; 32];
        let mut second = first;
        second[31] = 10;
        let mut tx = database.begin(7, 11).unwrap();
        let (first_id, first_charge) = allocate(&database, &mut tx, &first, 1024).unwrap();
        let (second_id, second_charge) = allocate(&database, &mut tx, &second, 1024).unwrap();
        assert_ne!(first_id, second_id);
        assert!(first_charge > second_charge);
        assert_eq!(allocate(&database, &mut tx, &first, 0).unwrap(), (first_id, 0));
        database.commit(tx).unwrap();
        for (tenant, owner) in [(7, 12), (8, 11)] {
            let mut tx = database.begin(tenant, owner).unwrap();
            assert_eq!(lookup(&database, &mut tx, &first).unwrap(), None);
            assert_eq!(allocate(&database, &mut tx, &second, 1024).unwrap().0, 1);
            database.commit(tx).unwrap();
        }
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(lookup(&database, &mut tx, &first).unwrap(), Some(first_id));
        assert_eq!(lookup(&database, &mut tx, &second).unwrap(), Some(second_id));
        assert_eq!(retire(&database, &mut tx, &first).unwrap(), second_charge);
        assert_eq!(retire(&database, &mut tx, &first).unwrap(), 0);
        database.commit(tx).unwrap();
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(lookup(&database, &mut tx, &first).unwrap(), None);
        let (replacement, charge) = allocate(&database, &mut tx, &first, 1024).unwrap();
        assert!(replacement > second_id);
        assert_eq!(charge, second_charge);
        database.commit(tx).unwrap();
    }

    #[test]
    fn rejected_budget_and_aborted_build_publish_no_dictionary_assignment() {
        let directory = tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let digest = [1; 32];
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(allocate(&database, &mut tx, &digest, 0).unwrap_err().kind(), io::ErrorKind::OutOfMemory);
        assert_eq!(lookup(&database, &mut tx, &digest).unwrap(), None);
        assert!(database.commit(tx).unwrap_err().to_string().contains("no durable semantic mutation"));
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(allocate(&database, &mut tx, &digest, 1024).unwrap().0, 1);
        drop(tx);
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(lookup(&database, &mut tx, &digest).unwrap(), None);
        assert_eq!(allocate(&database, &mut tx, &[2; 32], 1024).unwrap().0, 1);
        database.commit(tx).unwrap();
    }

    #[test]
    fn corrupt_allocator_and_id_exhaustion_do_not_allocate() {
        let directory = tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for bytes in [vec![], vec![0; 8], vec![1; 9], u64::MAX.to_be_bytes().to_vec()] {
            let mut tx = database.begin(7, 11).unwrap();
            let counter_key = key(&tx, NAMESPACE, COUNTER_KEY).unwrap();
            database.entity_put(&mut tx, counter_key, bytes).unwrap();
            database.commit(tx).unwrap();
            let mut tx = database.begin(7, 11).unwrap();
            assert!(allocate(&database, &mut tx, &[1; 32], 1024).is_err());
            assert_eq!(lookup(&database, &mut tx, &[1; 32]).unwrap(), None);
        }
    }

    fn prepared(vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
        vfs.create_dir(Path::new("/dictionary")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/dictionary"), vfs).unwrap();
        // The production search projection currently materializes its tree
        // without the authority write overlay. Exercise that publication path.
        database.set_write_overlay_enabled(false);
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(allocate(&database, &mut tx, &[1; 32], 1024).unwrap().0, 1);
        database.commit(tx).unwrap();
        database
    }

    fn publish_build(database: &mut AuthorityDatabase) -> io::Result<()> {
        let mut tx = database.begin(7, 11)?;
        let (id, _) = allocate(database, &mut tx, &[2; 32], 1024)?;
        let build_key = key(&tx, "search_build_v1", b"dictionary-fault-build")?;
        database.entity_put(&mut tx, build_key, id.to_be_bytes().to_vec())?;
        database.commit(tx)?;
        Ok(())
    }

    fn assert_recovery(vfs: Arc<DeterministicVfs>, acknowledged: bool) {
        vfs.arm_fault(None).unwrap();
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/dictionary"), vfs).unwrap();
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(lookup(&database, &mut tx, &[1; 32]).unwrap(), Some(1));
        let second = lookup(&database, &mut tx, &[2; 32]).unwrap();
        assert!(matches!(second, None | Some(2)));
        if acknowledged { assert_eq!(second, Some(2)); }
        let build_key = key(&tx, "search_build_v1", b"dictionary-fault-build").unwrap();
        let build = database.entity_get(&mut tx, &build_key).unwrap();
        assert_eq!(build.as_deref().map(decode_id).transpose().unwrap(), second,
            "a dictionary assignment and its build must recover atomically");
        let next = allocate(&database, &mut tx, &[3; 32], 1024).unwrap().0;
        assert_eq!(next, if second.is_some() { 3 } else { 2 });
        database.commit(tx).unwrap();
    }

    #[test]
    fn every_allocation_commit_io_fault_keeps_dictionary_and_build_atomic() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared(Arc::clone(&baseline_vfs));
        baseline_vfs.arm_fault(None).unwrap();
        publish_build(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_recovery(baseline_vfs, true);
        let mut injected = 0;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if operation == &Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
            for action in actions {
                let vfs = Arc::new(DeterministicVfs::new(None));
                let mut database = prepared(Arc::clone(&vfs));
                vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                let acknowledged = publish_build(&mut database).is_ok();
                drop(database);
                assert_recovery(vfs, acknowledged);
                injected += 1;
            }
        }
        assert!(injected > 0);
        eprintln!("dictionary allocation tested {injected} I/O fault windows");
    }
}
