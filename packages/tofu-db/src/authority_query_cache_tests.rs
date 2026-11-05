//! Optional facade contention retains the original query path.
use super::*;
#[test]
fn query_result_cache_configuration_contention_and_poison_refuse_owned_candidate() {
    let directory=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let view=database.enable_published_queries().unwrap();
    view.results.configure(65536);
    let request=crate::protocol::Request{correlation_id:[1;16],deadline_unix_ms:1,owner_id:11,tenant_id:Some(7),schema_id:1,operation:"record.get".into(),payload:b"key".to_vec(),command_id:None};
    for _ in 0..2 { view.results.insert((&request).into(),database.query_snapshot(),4,crate::query_result_cache::Admission::RepeatedKey,||Some(crate::transaction_ir::IrResponse::from_snapshot_cache(b"null".to_vec(), crate::query_result_cache::Admission::RepeatedKey))); }
    assert!(view.try_cached((&request).into()).is_some());
    let held=view.options.write().unwrap();assert!(view.try_cached((&request).into()).is_none());drop(held);
    assert!(view.try_cached((&request).into()).is_some());
    assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{let _held=view.options.write().unwrap();panic!("options poison");})).is_err());
    assert!(view.try_cached((&request).into()).is_none());
}

#[test]
fn published_reads_never_count_as_authority_handoff() {
    // The contention watch clears a wedge only on progress proving the mutex
    // changed hands. Published replicas and cached answers never take it, so
    // serving them beside a stuck holder must leave that progress unchanged.
    let directory=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let progress=database.foreground_admissions();
    let view=database.enable_published_queries().unwrap();
    view.results.configure(65536);
    let request=crate::protocol::Request{correlation_id:[2;16],deadline_unix_ms:1,owner_id:11,tenant_id:Some(7),schema_id:1,operation:"record.get".into(),payload:b"key".to_vec(),command_id:None};
    for _ in 0..2 { view.results.insert((&request).into(),database.query_snapshot(),4,crate::query_result_cache::Admission::RepeatedKey,||Some(crate::transaction_ir::IrResponse::from_snapshot_cache(b"null".to_vec(), crate::query_result_cache::Admission::RepeatedKey))); }
    let writer=std::sync::Mutex::new(database);
    let held=writer.lock().unwrap();
    for _ in 0..8 {
        drop(view.try_build().unwrap().unwrap());
        drop(view.try_cached((&request).into()).unwrap().into_response());
    }
    assert_eq!(progress.load(std::sync::atomic::Ordering::Relaxed), 0);
    held.note_foreground_admission();
    assert_eq!(progress.load(std::sync::atomic::Ordering::Relaxed), 1);
}
