//! Bounded compaction-archive source pages for the disposable search worker.
//! Reads at most the admitted archive bytes and eight archive index rows from
//! the caller's pinned snapshot. Oversized payloads stay explicitly partial.

use super::*;
use crate::generated_conversation_search::{SOURCE_PAGE_BYTES, SOURCE_PAGE_ROWS};

pub(crate) fn page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    cursor: &[u8],
) -> io::Result<crate::turn::SearchProjectionPage> {
    let prefix = conversation_prefix(conversation)?;
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    if !cursor.is_empty() {
        if !cursor.starts_with(&prefix) {
            return Err(invalid_input("archive search cursor escaped conversation"));
        }
        let mut next = cursor.to_vec();
        next.push(0);
        start = key(
            transaction,
            COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &next,
        )?;
    }
    let rows = database.entity_scan(transaction, &start, &end, SOURCE_PAGE_ROWS + 1)?;
    let mut result = crate::turn::SearchProjectionPage {
        turns: Vec::new(),
        next_cursor: cursor.to_vec(),
        complete: false,
        skipped_oversized: 0,
        source_bytes: 0,
        source_turns: 0,
    };
    let mut consumed = 0;
    for (index_key, value) in rows.iter().take(SOURCE_PAGE_ROWS) {
        let archive = archive_id_from_index(value)?;
        let stored_key =
            document_key(transaction, COMPACTION_ARCHIVE_MESSAGES_NAMESPACE, &archive)?;
        let stored = database
            .entity_get(transaction, &stored_key)?
            .ok_or_else(|| invalid_data("archive search source is missing"))?;
        let bytes = crate::versioned_document::stored_document_logical_bytes(
            &stored,
            COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
        )?;
        if bytes > SOURCE_PAGE_BYTES as u64 {
            result.skipped_oversized += 1;
        } else {
            if result.source_bytes + bytes > SOURCE_PAGE_BYTES as u64 {
                break;
            }
            let (messages, _) = load_value(
                database,
                transaction,
                COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
                &archive,
            )?
            .ok_or_else(|| invalid_data("archive search messages are missing"))?;
            let messages = messages
                .as_array()
                .ok_or_else(|| invalid_data("archive search messages are malformed"))?;
            for (index, message) in messages.iter().enumerate() {
                let Some(message) = message.as_object() else {
                    return Err(invalid_data("archive search message is malformed"));
                };
                if let Some(role @ ("user" | "assistant")) =
                    message.get("role").and_then(Value::as_str)
                {
                    let turn_id = format!(
                        "history:{}",
                        serde_json::to_string(&json!([archive, index])).map_err(|_| {
                            invalid_data("archive search identity cannot be encoded")
                        })?
                    );
                    crate::search_text::chunks(
                        &turn_id,
                        index as u64,
                        role,
                        message,
                        &mut result.turns,
                    );
                    result.source_turns += 1;
                }
            }
            result.source_bytes += bytes;
        }
        consumed += 1;
        result.next_cursor = index_key.key_bytes().to_vec();
    }
    result.complete = consumed == rows.len();
    Ok(result)
}
