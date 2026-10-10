//! Immutable transport bindings and borrowed dispatch views. Embedded calls
//! borrow launch data and shared coordinator handles for the runtime lifetime;
//! legacy framed builders retain owned bindings and copy-on-write overrides.
use super::{AuthorityDatabase, AuthorityGarbageCollectionBudget, TurnSearchProjection};
use std::{borrow::Cow, ops::Deref, sync::Mutex};

#[derive(Clone)]
pub(crate) struct StorageV2Bindings {
    pub(crate) authority_admission: std::sync::Arc<crate::authority_admission::AuthorityAdmission>,
    pub(crate) published_queries: Option<crate::authority::PublishedQueryView>,
    pub(crate) authority_gc_budget: AuthorityGarbageCollectionBudget,
    /// Strict-mode durability coordinator: command responses park on it
    /// *after* the authority mutex is released, so concurrent connections
    /// share one WAL fsync instead of queueing behind each other.
    pub(crate) wal_barrier: Option<crate::wal::WalBarrier>,
    /// Daemon-observation handles for the control operations served ahead of
    /// semantic admission; never used on the catalog dispatch path.
    pub(crate) control_observers: Option<crate::control_ops::ControlObservers>,
    /// Rotation-install signal shared with the daemon's rotation driver.
    /// While a rotation blocks admission, command dispatches park on it for
    /// the install window and replay in place instead of bouncing a busy
    /// round trip off the client. Absent in engine-only serving.
    pub(crate) rotation_barrier: Option<std::sync::Arc<crate::rotation_barrier::RotationBarrier>>,
}

#[derive(Clone)]
pub(crate) struct StorageV2Stores<'stores> {
    pub(super) authority: &'stores Mutex<AuthorityDatabase>,
    pub(super) search_projection: Option<&'stores Mutex<TurnSearchProjection>>,
    bindings: Cow<'stores, StorageV2Bindings>,
}
impl Deref for StorageV2Stores<'_> {
    type Target = StorageV2Bindings;
    fn deref(&self) -> &Self::Target {
        &self.bindings
    }
}
impl<'stores> StorageV2Stores<'stores> {
    pub(crate) fn new(
        authority: &'stores Mutex<AuthorityDatabase>,
        search_projection: Option<&'stores Mutex<TurnSearchProjection>>,
    ) -> Self {
        Self {
            authority,
            search_projection,
            bindings: Cow::Owned(StorageV2Bindings {
                authority_admission: std::sync::Arc::default(),
                published_queries: None,
                authority_gc_budget: AuthorityGarbageCollectionBudget::conservative(),
                wal_barrier: None,
                control_observers: None,
                rotation_barrier: None,
            }),
        }
    }

    pub(crate) fn borrowed(
        authority: &'stores Mutex<AuthorityDatabase>,
        search_projection: Option<&'stores Mutex<TurnSearchProjection>>,
        bindings: &'stores StorageV2Bindings,
    ) -> Self {
        Self {
            authority,
            search_projection,
            bindings: Cow::Borrowed(bindings),
        }
    }

    pub(crate) fn with_wal_barrier(mut self, barrier: crate::wal::WalBarrier) -> Self {
        self.bindings.to_mut().wal_barrier = Some(barrier);
        self
    }

    pub(crate) fn with_published_queries(
        mut self,
        view: crate::authority::PublishedQueryView,
    ) -> Self {
        self.bindings.to_mut().published_queries = Some(view);
        self
    }

    pub(crate) fn with_authority_admission(
        mut self,
        admission: std::sync::Arc<crate::authority_admission::AuthorityAdmission>,
    ) -> Self {
        self.bindings.to_mut().authority_admission = admission;
        self
    }

    pub(crate) fn with_control_observers(
        mut self,
        observers: crate::control_ops::ControlObservers,
    ) -> Self {
        self.bindings.to_mut().control_observers = Some(observers);
        self
    }

    pub(crate) fn with_rotation_barrier(
        mut self,
        barrier: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
    ) -> Self {
        self.bindings.to_mut().rotation_barrier = Some(barrier);
        self
    }

    pub(crate) fn with_authority_gc_budget(
        mut self,
        authority_gc_budget: AuthorityGarbageCollectionBudget,
    ) -> Self {
        self.bindings.to_mut().authority_gc_budget = authority_gc_budget;
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::authority_admission::{Admission, AuthorityAdmission};
    use std::{
        sync::Arc,
        time::{Duration, Instant},
    };
    fn bindings() -> StorageV2Bindings {
        StorageV2Bindings {
            authority_admission: Arc::default(),
            published_queries: None,
            authority_gc_budget: AuthorityGarbageCollectionBudget::conservative(),
            wal_barrier: None,
            rotation_barrier: None,
            control_observers: Some(crate::control_ops::ControlObservers {
                resource_budget: Some(
                    serde_json::json!({"launch": ["retained probe capacity", "mount classification"]}),
                ),
                ..Default::default()
            }),
        }
    }
    #[test]
    fn borrowed_bindings_share_launch_data_and_live_admission_without_cloning() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let bindings = bindings();
        let references = Arc::strong_count(&bindings.authority_admission);
        let stores = StorageV2Stores::borrowed(&database, None, &bindings);
        assert_eq!(Arc::strong_count(&bindings.authority_admission), references);
        assert!(std::ptr::eq(
            stores
                .control_observers
                .as_ref()
                .unwrap()
                .resource_budget
                .as_ref()
                .unwrap(),
            bindings
                .control_observers
                .as_ref()
                .unwrap()
                .resource_budget
                .as_ref()
                .unwrap()
        ));
        let permit = match bindings
            .authority_admission
            .acquire(Instant::now() + Duration::from_secs(1), &mut || true)
            .unwrap()
        {
            Admission::Entered(permit) => permit,
            _ => panic!("idle admission rejected"),
        };
        assert!(matches!(
            stores
                .authority_admission
                .acquire(Instant::now() + Duration::from_millis(2), &mut || true)
                .unwrap(),
            Admission::DeadlineElapsed
        ));
        drop(permit);
        assert!(matches!(
            stores
                .authority_admission
                .acquire(Instant::now() + Duration::from_secs(1), &mut || true)
                .unwrap(),
            Admission::Entered(_)
        ));
    }
    #[test]
    fn borrowed_bindings_builder_override_detaches_launch_state_and_keeps_source_unchanged() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let bindings = bindings();
        let replacement = Arc::new(AuthorityAdmission::default());
        let stores = StorageV2Stores::borrowed(&database, None, &bindings)
            .with_authority_admission(Arc::clone(&replacement));
        assert!(Arc::ptr_eq(&stores.authority_admission, &replacement));
        assert!(!Arc::ptr_eq(&bindings.authority_admission, &replacement));
        assert_eq!(
            stores.control_observers.as_ref().unwrap().resource_budget,
            bindings.control_observers.as_ref().unwrap().resource_budget
        );
        assert!(!std::ptr::eq(
            stores
                .control_observers
                .as_ref()
                .unwrap()
                .resource_budget
                .as_ref()
                .unwrap(),
            bindings
                .control_observers
                .as_ref()
                .unwrap()
                .resource_budget
                .as_ref()
                .unwrap()
        ));
    }
}
