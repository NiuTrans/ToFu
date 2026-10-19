//! Startup recovery for the disposable search index, never the source authority.
//! Entry point: open. Dependencies: AuthorityDatabase, directory lease, RealVfs.
//! One exclusively created sibling stages replacement; errors preserve evidence
//! and cannot accumulate unbounded generations of failed recovery directories.

use std::fs;
use std::io;
use std::path::Path;

use crate::authority::AuthorityDatabase;
use crate::engine::acquire_lease;
use crate::vfs::{sync_directory_barrier, RealVfs};

pub(super) fn open(data_dir: &Path) -> io::Result<AuthorityDatabase> {
    match AuthorityDatabase::open(data_dir) {
        Ok(database) => Ok(database),
        Err(error) if error.kind() == io::ErrorKind::InvalidData => rebuild(data_dir, error),
        Err(error) => Err(error),
    }
}

fn rebuild(data_dir: &Path, cause: io::Error) -> io::Result<AuthorityDatabase> {
    // The interior lease prevents moving an index still owned by a writer.
    // Symlink roots are not disposable recovery targets.
    if fs::symlink_metadata(data_dir)?.file_type().is_symlink() {
        return Err(cause);
    }
    let vfs = RealVfs;
    let retired_lease = acquire_lease(data_dir, &vfs)?;
    let parent = data_dir.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "search projection has no parent",
        )
    })?;
    let name = data_dir.file_name().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "search projection has no directory name",
        )
    })?;
    let mut recovery_name = std::ffi::OsString::from(".");
    recovery_name.push(name);
    recovery_name.push(".startup-recovery");
    let recovery = parent.join(recovery_name);
    // Exclusive create refuses a concurrent/interrupted repair or foreign path.
    // Never delete an existing sibling to make room for this attempt.
    fs::create_dir(&recovery)?;
    let replacement = recovery.join("replacement");
    let retired = recovery.join("retired");
    if let Err(error) = AuthorityDatabase::initialize(&replacement) {
        // Only the directory exclusively created by this attempt is reclaimed.
        if let Err(cleanup) = fs::remove_dir_all(&recovery) {
            crate::engine_log::warn(
                "search_recovery",
                &format!("replacement cleanup failed: {cleanup}"),
            );
        }
        return Err(error);
    }
    sync_directory_barrier(&vfs, &recovery)?;
    fs::rename(data_dir, &retired)?;
    sync_directory_barrier(&vfs, parent)?;
    sync_directory_barrier(&vfs, &recovery)?;
    if let Err(error) = fs::rename(&replacement, data_dir) {
        // Restore the previous index name if publication fails. Evidence stays
        // in the bounded recovery directory when even rollback is unavailable.
        if let Err(rollback) = fs::rename(&retired, data_dir) {
            return Err(io::Error::new(
                error.kind(),
                format!(
                    "search replacement publication failed: {error}; rollback failed: {rollback}"
                ),
            ));
        }
        return Err(error);
    }
    sync_directory_barrier(&vfs, parent)?;
    let database = AuthorityDatabase::open(data_dir)?;
    drop(retired_lease);
    crate::engine_log::warn("search_recovery", &format!(
        "rebuilt disposable search projection after integrity failure: {cause}; source authority unchanged"
    ));
    // Only after a verified replacement can the disposable old index go away.
    if let Err(error) = fs::remove_dir_all(&recovery) {
        crate::engine_log::warn(
            "search_recovery",
            &format!("retired projection cleanup failed: {error}"),
        );
    }
    Ok(database)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::ControlFile;
    use crate::turn_search_projection::TurnSearchProjection;

    fn corrupt_acknowledged_suffix(path: &Path) {
        let mut control = ControlFile::open(path).unwrap();
        let mut state = control.read_current().unwrap();
        state.generation += 1;
        state.durable_sequence += 15;
        state.root_hash = [1; 32];
        control.publish(&state).unwrap();
    }

    #[test]
    fn projection_missing_acknowledged_suffix_rebuilds_without_touching_source() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source");
        let projection = directory.path().join("projection");
        let source_db = AuthorityDatabase::initialize(&source).unwrap();
        let source_control = fs::read(source.join("CONTROL")).unwrap();
        let source_uuid = source_db.authority_uuid();
        drop(TurnSearchProjection::initialize(&projection, 1 << 20).unwrap());
        corrupt_acknowledged_suffix(&projection);
        assert_eq!(
            AuthorityDatabase::open(&projection).err().unwrap().kind(),
            io::ErrorKind::InvalidData
        );
        let mut rebuilt = TurnSearchProjection::open(&projection, 1 << 20).unwrap();
        rebuilt
            .begin_conversation_rebuild(7, 11, "conversation", "generation", 1)
            .unwrap();
        assert_eq!(crate::authority_gc_online::OnlineGcDatabase::gc_database(&rebuilt).unwrap().entity_overlay_entries_for_test(), 0);
        // A fresh index cannot claim complete source coverage.
        rebuilt.require_source_admission(source_uuid);
        assert!(rebuilt.owner_indexing(7, 11).unwrap());
        assert_eq!(fs::read(source.join("CONTROL")).unwrap(), source_control);
        assert_eq!(source_db.authority_uuid(), source_uuid);
        assert!(!directory
            .path()
            .join(".projection.startup-recovery")
            .exists());
        drop(rebuilt);
        assert!(TurnSearchProjection::open(&projection, 1 << 20).is_ok());
    }

    #[test]
    fn reopening_healthy_projection_keeps_synchronous_writes_and_index_identity() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("projection");
        let fresh = TurnSearchProjection::initialize(&path, 1 << 20).unwrap();
        let uuid = crate::authority_gc_online::OnlineGcDatabase::gc_database(&fresh).unwrap().authority_uuid();
        drop(fresh);
        let mut reopened = TurnSearchProjection::open(&path, 1 << 20).unwrap();
        assert_eq!(crate::authority_gc_online::OnlineGcDatabase::gc_database(&reopened).unwrap().authority_uuid(), uuid);
        reopened
            .begin_conversation_rebuild(7, 11, "conversation", "generation", 1)
            .unwrap();
        assert_eq!(crate::authority_gc_online::OnlineGcDatabase::gc_database(&reopened).unwrap().entity_overlay_entries_for_test(), 0);
    }

    #[test]
    fn both_native_transports_reach_readiness_after_projection_integrity_failure() {
        for embedded in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let source = directory.path().join("source");
            let projection = directory.path().join("projection");
            drop(AuthorityDatabase::initialize(&source).unwrap());
            let source_control = fs::read(source.join("CONTROL")).unwrap();
            drop(TurnSearchProjection::initialize(&projection, 1 << 20).unwrap());
            corrupt_acknowledged_suffix(&projection);
            let mut secret = [b's'; 32];
            if embedded {
                let runtime = crate::embed::EmbeddedRuntime::open(
                    &source,
                    11,
                    Some(7),
                    Some(&projection),
                    &mut secret,
                )
                .unwrap();
                drop(runtime);
            } else {
                let config = crate::daemon::DaemonConfig::new(&source, 11, Some(7), &mut secret)
                    .unwrap()
                    .with_search_projection_dir(&projection)
                    .unwrap();
                let mut readiness = Vec::new();
                crate::daemon::serve_supervised(
                    config,
                    io::Cursor::new(Vec::new()),
                    &mut readiness,
                )
                .unwrap();
                let document: serde_json::Value = serde_json::from_slice(&readiness).unwrap();
                assert_eq!(
                    document["resourceBudget"]["searchProjectionAvailable"],
                    true
                );
            }
            assert_eq!(fs::read(source.join("CONTROL")).unwrap(), source_control);
            assert!(TurnSearchProjection::open(&projection, 1 << 20).is_ok());
        }
    }

    #[test]
    fn busy_projection_is_never_rebuilt_or_removed() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("projection");
        let live = TurnSearchProjection::initialize(&path, 1 << 20).unwrap();
        let control = fs::read(path.join("CONTROL")).unwrap();
        assert_eq!(
            TurnSearchProjection::open(&path, 1 << 20)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(fs::read(path.join("CONTROL")).unwrap(), control);
        assert!(!directory
            .path()
            .join(".projection.startup-recovery")
            .exists());
        drop(live);
    }

    #[test]
    fn interrupted_or_foreign_recovery_is_bounded_and_never_overwritten() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("projection");
        drop(TurnSearchProjection::initialize(&path, 1 << 20).unwrap());
        corrupt_acknowledged_suffix(&path);
        let control = fs::read(path.join("CONTROL")).unwrap();
        let recovery = directory.path().join(".projection.startup-recovery");
        fs::create_dir(&recovery).unwrap();
        fs::write(recovery.join("evidence"), b"keep").unwrap();
        for _ in 0..2 {
            assert_eq!(
                TurnSearchProjection::open(&path, 1 << 20)
                    .err()
                    .unwrap()
                    .kind(),
                io::ErrorKind::AlreadyExists
            );
            assert_eq!(fs::read(path.join("CONTROL")).unwrap(), control);
            assert_eq!(fs::read(recovery.join("evidence")).unwrap(), b"keep");
        }
    }
}
