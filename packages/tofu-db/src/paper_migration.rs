//! Offline paper-domain migration dispatcher for the exclusive operator
//! importer.
//!
//! Routes each `migration.paper.<subdomain>.*` operation to the certified
//! adapter hosted by its native authority module (`paper`, `paper_artifact`,
//! `paper_library`, `paper_podcast`). Each adapter preserves exact source
//! state (insert-or-match; no server-clock overwrite), rebuilds its indexes
//! and counts in the same authority transaction, enumerates its owner-scoped
//! catalog independently, and fails closed on any divergence. Entry point:
//! `execute`, dispatched from `migration_session`.

use serde_json::Value;
use std::io;

use crate::authority::AuthorityDatabase;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    match operation {
        "migration.paper.library.restore"
        | "migration.paper.library.get"
        | "migration.paper.library.scan" => crate::paper_library::library_migration_execute(
            database, tenant, owner, operation, payload,
        ),
        "migration.paper.report.restore"
        | "migration.paper.report.get"
        | "migration.paper.report.scan" => crate::paper_artifact::report_migration_execute(
            database, tenant, owner, operation, payload,
        ),
        "migration.paper.translation.restore"
        | "migration.paper.translation.get"
        | "migration.paper.translation.scan" => {
            crate::paper_artifact::translation_migration_execute(
                database, tenant, owner, operation, payload,
            )
        }
        "migration.paper.note.restore"
        | "migration.paper.note.get"
        | "migration.paper.note.scan" => {
            crate::paper::note_migration_execute(database, tenant, owner, operation, payload)
        }
        "migration.paper.podcast.restore"
        | "migration.paper.podcast.get"
        | "migration.paper.podcast.scan" => crate::paper_podcast::podcast_migration_execute(
            database, tenant, owner, operation, payload,
        ),
        _ => Err(invalid_input("unknown paper migration operation")),
    }
}
