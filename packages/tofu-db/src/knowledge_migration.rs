//! Offline knowledge-domain migration dispatcher for the exclusive operator
//! importer.
//!
//! Routes each `migration.knowledge.*` operation to the certified adapter
//! hosted by the native `knowledge` authority module. The adapter preserves
//! exact source state (insert-or-match; no server-clock overwrite), rebuilds
//! the digest index, enrichment queue, and tenant-global owner index in the
//! same authority transaction, enumerates the owner-scoped catalog
//! independently, and fails closed on any divergence. Chunk content and asset
//! text route through the blob-capable record path without truncation. Entry
//! point: `execute`, dispatched from `migration_session`.

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
        "migration.knowledge.settings.restore"
        | "migration.knowledge.settings.get"
        | "migration.knowledge.document.restore"
        | "migration.knowledge.document.get"
        | "migration.knowledge.catalog.scan" => crate::knowledge::knowledge_migration_execute(
            database, tenant, owner, operation, payload,
        ),
        _ => Err(invalid_input("unknown knowledge migration operation")),
    }
}
