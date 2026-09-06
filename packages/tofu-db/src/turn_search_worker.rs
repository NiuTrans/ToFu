//! Bounded authority-to-search projection maintenance round.
//!
//! The worker holds an immutable MVCC source transaction across pages but
//! releases the foreground authority mutex between every bounded read. A
//! changed dirty epoch survives exact-token ACK and schedules another complete
//! generation, while an interrupted build remains invisible in the target.

use std::io;
use std::sync::{Arc, Mutex, TryLockError};

use blake3::Hasher;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::search_dirty::{self, DirtyConversation};
use crate::turn;
use crate::turn_search_projection::{TurnSearchDocument, TurnSearchProjection};

const MAX_SOURCE_PAGES: usize = 1_024;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct TurnSearchWorkerMetrics {
    pub dirty_conversations: u64,
    pub rebuilt_conversations: u64,
    pub removed_conversations: u64,
    pub acknowledged_tokens: u64,
    pub source_pages: u64,
    pub indexed_turns: u64,
    pub skipped_oversized_turns: u64,
    pub source_bytes: u64,
    pub foreground_deferrals: u64,
}

fn poisoned(name: &str) -> io::Error {
    io::Error::other(format!("{name} mutex is poisoned"))
}

fn generation(dirty: &DirtyConversation) -> String {
    let mut tokens = dirty.tokens.clone();
    tokens.sort_by(|left, right| left.key.cmp(&right.key));
    let mut hasher = Hasher::new();
    hasher.update(b"tofu-db:turn-search:worker-generation:v1\0");
    hasher.update(&(dirty.conversation_id.len() as u64).to_be_bytes());
    hasher.update(dirty.conversation_id.as_bytes());
    for token in tokens {
        hasher.update(&(token.key.encoded().len() as u64).to_be_bytes());
        hasher.update(token.key.encoded());
        hasher.update(&(token.value.len() as u64).to_be_bytes());
        hasher.update(&token.value);
    }
    hasher.finalize().to_hex().to_string()
}

fn try_authority<'a>(
    authority: &'a Arc<Mutex<AuthorityDatabase>>,
    metrics: &mut TurnSearchWorkerMetrics,
) -> io::Result<Option<std::sync::MutexGuard<'a, AuthorityDatabase>>> {
    match authority.try_lock() {
        Ok(database) => Ok(Some(database)),
        Err(TryLockError::WouldBlock) => {
            metrics.foreground_deferrals = metrics.foreground_deferrals.saturating_add(1);
            Ok(None)
        }
        Err(TryLockError::Poisoned(_)) => Err(poisoned("turn-search authority")),
    }
}

fn projection_lock(
    projection: &Arc<Mutex<TurnSearchProjection>>,
) -> io::Result<std::sync::MutexGuard<'_, TurnSearchProjection>> {
    projection
        .lock()
        .map_err(|_| poisoned("turn-search projection"))
}

fn acknowledge(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    scope: crate::maintenance::MaintenanceScope,
    dirty: &DirtyConversation,
    metrics: &mut TurnSearchWorkerMetrics,
) -> io::Result<bool> {
    let Some(mut database) = try_authority(authority, metrics)? else {
        return Ok(false);
    };
    let acknowledged =
        database.acknowledge_search_dirty(scope.tenant_id, scope.owner_user_id, &dirty.tokens)?;
    metrics.acknowledged_tokens = metrics
        .acknowledged_tokens
        .saturating_add(acknowledged as u64);
    Ok(true)
}

fn process_conversation(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    dirty: &DirtyConversation,
    mut source: AuthorityTransaction,
    metrics: &mut TurnSearchWorkerMetrics,
) -> io::Result<bool> {
    let Some(database) = try_authority(authority, metrics)? else {
        return Ok(false);
    };
    let updated_at_ms = crate::conversation_header::search_projection_updated_at(
        &database,
        &mut source,
        &dirty.conversation_id,
    )?;
    drop(database);
    let generation = generation(dirty);
    let Some(updated_at_ms) = updated_at_ms else {
        projection_lock(projection)?.remove_conversation(
            scope.tenant_id,
            scope.owner_user_id,
            &dirty.conversation_id,
        )?;
        metrics.removed_conversations = metrics.removed_conversations.saturating_add(1);
        return acknowledge(authority, scope, dirty, metrics);
    };

    projection_lock(projection)?.begin_conversation_rebuild(
        scope.tenant_id,
        scope.owner_user_id,
        &dirty.conversation_id,
        &generation,
        updated_at_ms,
    )?;
    let mut cursor = Vec::new();
    for page_number in 0..MAX_SOURCE_PAGES {
        let Some(database) = try_authority(authority, metrics)? else {
            return Ok(false);
        };
        let page =
            turn::search_projection_page(&database, &mut source, &dirty.conversation_id, &cursor)?;
        drop(database);
        if !page.complete && page.next_cursor == cursor {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "turn-search source cursor stalled",
            ));
        }
        let documents = page
            .turns
            .into_iter()
            .map(|turn| TurnSearchDocument {
                turn_id: turn.turn_id,
                ordinal: turn.ordinal,
                search_text: turn.search_text,
            })
            .collect::<Vec<_>>();
        if !documents.is_empty() {
            projection_lock(projection)?.append_conversation_page(
                scope.tenant_id,
                scope.owner_user_id,
                &dirty.conversation_id,
                &generation,
                &documents,
            )?;
        }
        metrics.source_pages = metrics.source_pages.saturating_add(1);
        metrics.indexed_turns = metrics.indexed_turns.saturating_add(documents.len() as u64);
        metrics.skipped_oversized_turns = metrics
            .skipped_oversized_turns
            .saturating_add(page.skipped_oversized as u64);
        metrics.source_bytes = metrics.source_bytes.saturating_add(page.source_bytes);
        cursor = page.next_cursor;
        if page.complete {
            projection_lock(projection)?.finalize_conversation_rebuild(
                scope.tenant_id,
                scope.owner_user_id,
                &dirty.conversation_id,
                &generation,
            )?;
            metrics.rebuilt_conversations = metrics.rebuilt_conversations.saturating_add(1);
            return acknowledge(authority, scope, dirty, metrics);
        }
        if page_number + 1 == MAX_SOURCE_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "turn-search source exceeds its page budget",
            ));
        }
    }
    unreachable!("bounded turn-search page loop returns at its limit")
}

pub fn process_turn_search_batch(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
) -> io::Result<TurnSearchWorkerMetrics> {
    let mut metrics = TurnSearchWorkerMetrics::default();
    let Some(database) = try_authority(authority, &mut metrics)? else {
        return Ok(metrics);
    };
    let dirty = search_dirty::list(&database, scope.tenant_id, scope.owner_user_id)?;
    let sources = dirty
        .iter()
        .map(|_| database.begin(scope.tenant_id, scope.owner_user_id))
        .collect::<io::Result<Vec<_>>>()?;
    drop(database);
    metrics.dirty_conversations = dirty.len() as u64;
    for (dirty, source) in dirty.iter().zip(sources) {
        if !process_conversation(authority, projection, scope, dirty, source, &mut metrics)? {
            break;
        }
    }
    Ok(metrics)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::conversation_header::{CreateRequest, DeleteRequest, TurnDefaults};
    use crate::turn::AppendSettledRequest;
    use crate::turn_search_projection::ConversationSearchRequest;

    fn append_turn(
        database: &mut AuthorityDatabase,
        turn_id: &str,
        actor: &str,
        lane_id: &str,
        status: &str,
        projection_json: serde_json::Value,
        timestamp: u64,
    ) {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        turn::append_settled(
            database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "conversation".to_owned(),
                actor: actor.to_owned(),
                status: status.to_owned(),
                projection_json: serde_json::to_vec(&projection_json).unwrap(),
                settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
                lane_id: lane_id.to_owned(),
                command_id: format!("command-{turn_id}"),
                kind: "message".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: None,
                created_at_ms: timestamp,
                committed_at_ms: timestamp,
                defaults: TurnDefaults {
                    allow_create: false,
                    title: String::new(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: timestamp,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }

    fn search(
        projection: &Arc<Mutex<TurnSearchProjection>>,
        query: &str,
    ) -> Vec<crate::turn_search_projection::ConversationSearchHit> {
        projection
            .lock()
            .unwrap()
            .search(&ConversationSearchRequest {
                tenant_id: 7,
                owner_user_id: 11,
                query: query.to_owned(),
                limit: 50,
                snippet_radius: 10,
            })
            .unwrap()
    }

    #[test]
    fn batch_rebuilds_settled_main_turns_acks_tokens_and_removes_deleted_header() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        let mut create = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut create,
            &CreateRequest {
                conversation_id: "conversation".to_owned(),
                title: "title".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 100,
                committed_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(create).unwrap();
        append_turn(
            &mut database,
            "main",
            "assistant",
            "main",
            "completed",
            serde_json::json!({
                "content": "primary searchable phrase",
                "thinking": "reasoning token",
                "translatedContent": "翻译命中",
                "originalContent": "original token"
            }),
            200,
        );
        append_turn(
            &mut database,
            "branch",
            "assistant",
            "branch-a",
            "completed",
            serde_json::json!({"content": "branch must stay hidden"}),
            300,
        );
        append_turn(
            &mut database,
            "running",
            "assistant",
            "main",
            "running",
            serde_json::json!({"content": "running must stay hidden"}),
            400,
        );
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();

        let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(metrics.dirty_conversations, 1);
        assert_eq!(metrics.rebuilt_conversations, 1);
        assert_eq!(metrics.indexed_turns, 1);
        assert_eq!(metrics.acknowledged_tokens, 2);
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11).unwrap(),
            []
        );
        for query in ["searchable", "reasoning", "翻译", "original"] {
            assert_eq!(search(&projection, query).len(), 1);
        }
        for query in ["branch must", "running must"] {
            assert!(search(&projection, query).is_empty());
        }

        let mut database = authority.lock().unwrap();
        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &database,
            &mut delete,
            &DeleteRequest {
                conversation_id: "conversation".to_owned(),
                deleted_at_ms: 500,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        drop(database);
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11)
                .unwrap()
                .len(),
            1
        );

        let removed = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(removed.removed_conversations, 1);
        assert!(search(&projection, "searchable").is_empty());
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11).unwrap(),
            []
        );
    }
}
