//! Immutable child birth evidence in the task-result authority.
//!
//! Ports `lib/storage_authority_sql/operations_pkg/_invocations.py` one for one:
//! `register_child` proves an owned active conversation plus parent task
//! birth (task-result record, or an exact bound generation attempt as the
//! fallback), then stores the immutable user/conversation/parent/child
//! identity inside the existing `task_results` record. Natural replay
//! returns the same identity; rebinding is a conflict. `resolve_child`
//! authorizes an archive scope strictly against the persisted identity —
//! syntax locates the record, never grants access.

use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};

const MAX_IDENTITY_TEXT: usize = 512;
const MAX_ARCHIVE_SCOPE: usize = 512;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn not_found(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn valid_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.chars().count() <= maximum
}

fn identity_object(
    owner_user_id: u64,
    conversation_id: &str,
    parent_task_id: &str,
    child_task_id: &str,
) -> Map<String, Value> {
    json!({
        "user_id": owner_user_id,
        "conversation_id": conversation_id,
        "parent_task_id": parent_task_id,
        "child_task_id": child_task_id,
        "archive_scope": format!("invocation:{owner_user_id}:{child_task_id}"),
    })
    .as_object()
    .expect("identity literal is an object")
    .clone()
}

#[derive(Clone, Debug)]
pub(crate) enum Request {
    RegisterChild {
        conversation_id: String,
        parent_task_id: String,
        child_task_id: String,
        created_at: i64,
        updated_at_ms: u64,
    },
    ResolveChild {
        archive_scope: String,
    },
}

impl Request {
    /// Validate bound-sensitive fields; returns the literal byte cost.
    pub(crate) fn validate(&self) -> io::Result<usize> {
        match self {
            Self::RegisterChild {
                conversation_id,
                parent_task_id,
                child_task_id,
                created_at,
                updated_at_ms,
            } => {
                if !valid_text(conversation_id, MAX_IDENTITY_TEXT)
                    || !valid_text(parent_task_id, MAX_IDENTITY_TEXT)
                    || !valid_text(child_task_id, MAX_IDENTITY_TEXT)
                    || *created_at <= 0
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid invocation registration step"));
                }
                Ok(conversation_id.len() + parent_task_id.len() + child_task_id.len())
            }
            Self::ResolveChild { archive_scope } => {
                if !valid_text(archive_scope, MAX_ARCHIVE_SCOPE) {
                    return Err(invalid_input("invalid invocation resolve step"));
                }
                Ok(archive_scope.len())
            }
        }
    }
}

fn register_child(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    parent_task_id: &str,
    child_task_id: &str,
    created_at: i64,
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let owner_user_id = transaction.owner_user_id();
    if !crate::conversation_header::is_active(database, transaction, conversation_id)? {
        return Err(not_found("Conversation not found"));
    }
    let parent =
        crate::task_result::read_document_value(database, transaction, parent_task_id)?;
    let parent_matches = parent.as_ref().is_some_and(|document| {
        document.get("user_id").and_then(Value::as_u64) == Some(owner_user_id)
            && document.get("conv_id").and_then(Value::as_str) == Some(conversation_id)
    });
    // The fallback mirrors the legacy join: an exact bound generation
    // attempt inside the already-proven owned conversation.
    if !parent_matches
        && !crate::turn::attempt_exists_for_task_in_conversation(
            database,
            transaction,
            parent_task_id,
            conversation_id,
        )?
    {
        return Err(not_found("Parent task not found"));
    }
    let identity = identity_object(owner_user_id, conversation_id, parent_task_id, child_task_id);
    // The child task ID itself is the immutable global identity, independent
    // of scope; replay of the same birth returns the persisted identity.
    match crate::task_result::read_document_value(database, transaction, child_task_id)? {
        Some(current) => {
            if current.get("child_invocation") != Some(&Value::Object(identity.clone())) {
                return Err(conflict("Child identity is immutable"));
            }
        }
        None => {
            let document = json!({
                "task_id": child_task_id,
                "user_id": owner_user_id,
                "conv_id": conversation_id,
                "status": "registered",
                "created_at": created_at,
                "child_invocation": Value::Object(identity.clone()),
            });
            crate::task_result::checkpoint_child_birth(
                database,
                transaction,
                child_task_id,
                serde_json::to_vec(&document)
                    .map_err(|_| invalid_data("invocation birth record cannot be encoded"))?,
                updated_at_ms,
            )?;
        }
    }
    serde_json::to_vec(&Value::Object(identity))
        .map_err(|_| invalid_data("invocation identity cannot be encoded"))
}

fn resolve_child(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive_scope: &str,
) -> io::Result<Vec<u8>> {
    let owner_user_id = transaction.owner_user_id();
    let prefix = format!("invocation:{owner_user_id}:");
    let child = archive_scope.strip_prefix(&prefix).unwrap_or("");
    let lookup_key = if child.is_empty() { archive_scope } else { child };
    let document = crate::task_result::read_document_value(database, transaction, lookup_key)?;
    let identity = document
        .as_ref()
        .and_then(|value| value.get("child_invocation"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if identity.get("archive_scope").and_then(Value::as_str) != Some(archive_scope)
        || identity.get("user_id").and_then(Value::as_u64) != Some(owner_user_id)
    {
        return Err(not_found("Child invocation not found"));
    }
    serde_json::to_vec(&Value::Object(identity))
        .map_err(|_| invalid_data("invocation identity cannot be encoded"))
}

/// Authorize a nonempty archive scope against the persisted birth identity
/// (ports `_invocations.require_child`): the scope must resolve through the
/// exact registered identity, and that identity must name this conversation
/// and the authenticated owner — never just a prefix.
pub(crate) fn require_child(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive_scope: &str,
    conversation_id: &str,
) -> io::Result<Map<String, Value>> {
    let owner_user_id = transaction.owner_user_id();
    let prefix = format!("invocation:{owner_user_id}:");
    let child = archive_scope.strip_prefix(&prefix).unwrap_or("");
    let lookup_key = if child.is_empty() { archive_scope } else { child };
    let document = crate::task_result::read_document_value(database, transaction, lookup_key)?;
    let identity = document
        .as_ref()
        .and_then(|value| value.get("child_invocation"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if identity.get("archive_scope").and_then(Value::as_str) != Some(archive_scope)
        || identity.get("user_id").and_then(Value::as_u64) != Some(owner_user_id)
        || identity.get("conversation_id").and_then(Value::as_str) != Some(conversation_id)
    {
        return Err(not_found("Child invocation not found"));
    }
    Ok(identity)
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::RegisterChild {
            conversation_id,
            parent_task_id,
            child_task_id,
            created_at,
            updated_at_ms,
        } => register_child(
            database,
            transaction,
            conversation_id,
            parent_task_id,
            child_task_id,
            *created_at,
            *updated_at_ms,
        )
        .map(Some),
        Request::ResolveChild { archive_scope } => {
            resolve_child(database, transaction, archive_scope).map(Some)
        }
    }
}
