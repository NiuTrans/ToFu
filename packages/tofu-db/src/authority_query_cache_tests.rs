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
