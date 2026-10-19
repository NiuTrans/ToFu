//! Owner-scoped optimizer proposals and reversible action audit rows.
//!
//! Large evidence/metric fields live in blob-capable versioned documents;
//! compact entity indexes preserve legacy time ordering without hydrating
//! unrelated documents. Every document, index, count, receipt, and outbox
//! mutation shares one OCC commit through Transaction IR.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE, OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE,
    OPTIMIZER_ACTION_COUNT_NAMESPACE, OPTIMIZER_ACTION_DOCUMENT_NAMESPACE,
    OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE, OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_COUNT_NAMESPACE, OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE, OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_STATUS_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const PROPOSAL_LOGICAL_NAMESPACE: &str = "optimizer_proposals";
const ACTION_LOGICAL_NAMESPACE: &str = "optimizer_action_log";
const COUNT_KEY: &[u8] = b"count";

fn checked_byte_sum(lengths: &[usize]) -> io::Result<usize> {
    lengths.iter().try_fold(0_usize, |sum, length| sum.checked_add(*length)
        .ok_or_else(|| invalid_data("optimizer byte count overflow")))
}

fn encoded_json_bytes(value: &impl Serialize) -> io::Result<usize> {
    struct ByteCounter(usize);
    impl io::Write for ByteCounter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0 = checked_byte_sum(&[self.0, bytes.len()])?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> { Ok(()) }
    }
    let mut counter = ByteCounter(0);
    serde_json::to_writer(&mut counter, value)
        .map_err(|_| invalid_data("optimizer JSON cannot be encoded"))?;
    Ok(counter.0)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn owner_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn namespace_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        prefix,
    )
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 254]);
    }
    output.extend_from_slice(&[u8::MAX, u8::MAX]);
}

fn ascending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[byte, 1]);
    }
    output.extend_from_slice(&[0, 0]);
}

fn ordered_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    first: &str,
    id: &str,
    descending: bool,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((first.len() + id.len()) * 2 + 2);
    if descending {
        descending_text(&mut raw, first);
    } else {
        ascending_text(&mut raw, first);
    }
    ascending_text(&mut raw, id);
    owner_key(transaction, namespace, &raw)
}

fn proposal_action_prefix(proposal_id: &str) -> Vec<u8> {
    let mut raw = Vec::with_capacity(proposal_id.len() * 2 + 1);
    ascending_text(&mut raw, proposal_id);
    raw
}

fn proposal_action_index_key(
    transaction: &AuthorityTransaction,
    proposal_id: &str,
    applied_at: &str,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = proposal_action_prefix(proposal_id);
    raw.reserve((applied_at.len() + id.len()) * 2 + 2);
    descending_text(&mut raw, applied_at);
    ascending_text(&mut raw, id);
    owner_key(transaction, OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE, &raw)
}

fn proposal_status_prefix(status: &str) -> Vec<u8> {
    let mut raw = Vec::with_capacity(status.len() * 2 + 2);
    ascending_text(&mut raw, status);
    raw
}

fn proposal_status_index_key(
    transaction: &AuthorityTransaction,
    proposal: &Proposal,
) -> io::Result<EntityKey> {
    let mut raw = proposal_status_prefix(&proposal.status);
    raw.reserve((proposal.created_at.len() + proposal.id.len()) * 2 + 4);
    descending_text(&mut raw, &proposal.created_at);
    ascending_text(&mut raw, &proposal.id);
    owner_key(transaction, OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE, &raw)
}

fn proposal_status_key(
    transaction: &AuthorityTransaction,
    proposal_id: &str,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        OPTIMIZER_PROPOSAL_STATUS_NAMESPACE,
        proposal_id.as_bytes(),
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<u64> {
    let key = owner_key(transaction, namespace, COUNT_KEY)?;
    match database.entity_get(transaction, &key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("optimizer count is malformed")),
    }
}

fn increment_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<()> {
    let count = read_count(database, transaction, namespace)?.checked_add(1)
        .ok_or_else(|| invalid_data("optimizer owner count overflow"))?;
    database.entity_put(
        transaction,
        owner_key(transaction, namespace, COUNT_KEY)?,
        count.to_le_bytes().to_vec(),
    )
}

fn valid_text(value: &str, maximum: usize, required: bool) -> bool {
    (!required || !value.is_empty()) && value.chars().count() <= maximum
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Proposal {
    pub user_id: u64,
    pub id: String,
    pub created_at: String,
    pub title: String,
    pub rationale: String,
    pub action_type: String,
    pub action_args: String,
    pub severity: String,
    pub confidence: f64,
    pub evidence: String,
    pub status: String,
    pub status_reason: String,
}

impl Proposal {
    fn valid(&self) -> bool {
        self.user_id > 0
            && valid_text(&self.id, 128, true)
            && valid_text(&self.created_at, 64, true)
            && valid_text(&self.action_type, 256, true)
            && !self.action_args.is_empty()
            && valid_text(&self.severity, 64, false)
            && self.confidence.is_finite()
            && (0.0..=1.0).contains(&self.confidence)
            && !self.evidence.is_empty()
            && valid_text(&self.status, 64, false)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Action {
    pub user_id: u64,
    pub id: String,
    pub proposal_id: String,
    pub applied_at: String,
    pub expires_at: String,
    pub pre_metric: String,
    pub outcome_metric: String,
    pub outcome_recorded_at: String,
    pub reverted_at: String,
    pub revert_reason: String,
}

impl Action {
    fn valid(&self) -> bool {
        self.user_id > 0
            && valid_text(&self.id, 128, true)
            && valid_text(&self.proposal_id, 128, true)
            && valid_text(&self.applied_at, 64, true)
            && valid_text(&self.expires_at, 64, true)
            && !self.pre_metric.is_empty()
            && valid_text(&self.outcome_recorded_at, 64, false)
            && valid_text(&self.reverted_at, 64, false)
    }
}

#[derive(Deserialize, Serialize)]
struct ExpiryIndexValue {
    id: String,
    proposal_id: String,
    expires_at: String,
}

#[derive(Clone, Debug)]
pub enum Request {
    ProposalCreate {
        proposal: Proposal,
        updated_at_ms: u64,
    },
    ProposalUpdate {
        id: String,
        status: String,
        reason: String,
        updated_at_ms: u64,
    },
    ProposalGet {
        id: String,
    },
    ProposalList {
        status: String,
        limit: usize,
    },
    ActionRecord {
        action: Action,
        updated_at_ms: u64,
    },
    ActionOutcome {
        id: String,
        metric: String,
        recorded_at: String,
        updated_at_ms: u64,
    },
    ActionRevert {
        id: String,
        reverted_at: String,
        reason: String,
        updated_at_ms: u64,
    },
    ActionList {
        include_reverted: bool,
        limit: usize,
    },
    ActionExpired {
        now_iso: String,
    },
    ActionForProposal {
        proposal_id: String,
    },
}

impl Request {
    pub(crate) fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::ProposalCreate { .. }
                | Self::ProposalUpdate { .. }
                | Self::ActionRecord { .. }
                | Self::ActionOutcome { .. }
                | Self::ActionRevert { .. }
        )
    }

    pub(crate) fn validate(&self, owner_user_id: u64) -> io::Result<usize> {
        let bytes = match self {
            Self::ProposalCreate {
                proposal,
                updated_at_ms,
            } => {
                if proposal.user_id != owner_user_id || !proposal.valid() || *updated_at_ms == 0 {
                    return Err(invalid_input("invalid optimizer proposal create"));
                }
                encoded_json_bytes(proposal)?
            }
            Self::ProposalUpdate {
                id,
                status,
                reason,
                updated_at_ms,
            } => {
                if !valid_text(id, 128, true)
                    || !valid_text(status, 64, true)
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid optimizer proposal update"));
                }
                checked_byte_sum(&[id.len(), status.len(), reason.len()])?
            }
            Self::ProposalGet { id } => {
                if !valid_text(id, 128, true) {
                    return Err(invalid_input("invalid optimizer proposal get"));
                }
                id.len()
            }
            Self::ProposalList { status, limit } => {
                if !valid_text(status, 64, false) || *limit == 0
                {
                    return Err(invalid_input("invalid optimizer proposal list"));
                }
                status.len()
            }
            Self::ActionRecord {
                action,
                updated_at_ms,
            } => {
                if action.user_id != owner_user_id || !action.valid() || *updated_at_ms == 0 {
                    return Err(invalid_input("invalid optimizer action record"));
                }
                encoded_json_bytes(action)?
            }
            Self::ActionOutcome {
                id,
                metric,
                recorded_at,
                updated_at_ms,
            } => {
                if !valid_text(id, 128, true)
                    || metric.is_empty()
                    || !valid_text(recorded_at, 64, true)
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid optimizer action outcome"));
                }
                checked_byte_sum(&[id.len(), metric.len(), recorded_at.len()])?
            }
            Self::ActionRevert {
                id,
                reverted_at,
                reason,
                updated_at_ms,
            } => {
                if !valid_text(id, 128, true)
                    || !valid_text(reverted_at, 64, true)
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid optimizer action revert"));
                }
                checked_byte_sum(&[id.len(), reverted_at.len(), reason.len()])?
            }
            Self::ActionList { limit, .. } => {
                if *limit == 0 {
                    return Err(invalid_input("invalid optimizer action list"));
                }
                0
            }
            Self::ActionExpired { now_iso } => {
                if !valid_text(now_iso, 64, true) {
                    return Err(invalid_input("invalid optimizer expiry query"));
                }
                now_iso.len()
            }
            Self::ActionForProposal { proposal_id } => {
                if !valid_text(proposal_id, 128, true) {
                    return Err(invalid_input("invalid optimizer proposal action query"));
                }
                proposal_id.len()
            }
        };
        Ok(bytes)
    }
}

fn document_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    id: &str,
) -> io::Result<EntityKey> {
    owner_key(transaction, namespace, id.as_bytes())
}

fn decode_proposal(raw: &[u8], id: &str, owner: u64) -> io::Result<Proposal> {
    let value: Proposal =
        serde_json::from_slice(raw).map_err(|_| invalid_data("optimizer proposal is malformed"))?;
    if value.id != id || value.user_id != owner || !value.valid() {
        return Err(invalid_data("optimizer proposal fields are inconsistent"));
    }
    Ok(value)
}

fn decode_action(raw: &[u8], id: &str, owner: u64) -> io::Result<Action> {
    let value: Action =
        serde_json::from_slice(raw).map_err(|_| invalid_data("optimizer action is malformed"))?;
    if value.id != id || value.user_id != owner || !value.valid() {
        return Err(invalid_data("optimizer action fields are inconsistent"));
    }
    Ok(value)
}

fn get_proposal(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Proposal>> {
    let key = document_key(transaction, OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE, id)?;
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        PROPOSAL_LOGICAL_NAMESPACE,
        id,
        transaction.owner_user_id(),
        usize::MAX,
    )?
    .map(|raw| decode_proposal(&raw, id, transaction.owner_user_id()))
    .transpose()
}

fn get_action(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Action>> {
    let key = document_key(transaction, OPTIMIZER_ACTION_DOCUMENT_NAMESPACE, id)?;
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        ACTION_LOGICAL_NAMESPACE,
        id,
        transaction.owner_user_id(),
        usize::MAX,
    )?
    .map(|raw| decode_action(&raw, id, transaction.owner_user_id()))
    .transpose()
}

fn put_proposal(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    proposal: &Proposal,
    now: u64,
) -> io::Result<()> {
    let raw = serde_json::to_vec(proposal)
        .map_err(|_| invalid_input("optimizer proposal cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(
                transaction,
                OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE,
                &proposal.id,
            )?,
            namespace: PROPOSAL_LOGICAL_NAMESPACE.to_owned(),
            logical_key: proposal.id.clone(),
            value_json: raw,
            expected_version: None,
            updated_at_ms: now,
        },
        transaction.owner_user_id(),
        usize::MAX,
    )
    .map(|_| ())
}

fn put_action(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    action: &Action,
    now: u64,
) -> io::Result<()> {
    let raw = serde_json::to_vec(action)
        .map_err(|_| invalid_input("optimizer action cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, OPTIMIZER_ACTION_DOCUMENT_NAMESPACE, &action.id)?,
            namespace: ACTION_LOGICAL_NAMESPACE.to_owned(),
            logical_key: action.id.clone(),
            value_json: raw,
            expected_version: None,
            updated_at_ms: now,
        },
        transaction.owner_user_id(),
        usize::MAX,
    )
    .map(|_| ())
}

fn encode_optional<T: Serialize>(value: Option<&T>) -> io::Result<Option<Vec<u8>>> {
    value
        .map(|value| {
            serde_json::to_vec(value)
                .map_err(|_| invalid_data("optimizer response cannot be encoded"))
        })
        .transpose()
}

fn encode_rows(rows: &[Value]) -> io::Result<Vec<u8>> {
    let raw = serde_json::to_vec(rows)
        .map_err(|_| invalid_data("optimizer response cannot be encoded"))?;
    Ok(raw)
}

fn push_requested_row(
    rows: &mut Vec<Value>,
    retained_bytes: &mut usize,
    row: Value,
) -> io::Result<()> {
    let row_bytes = encoded_json_bytes(&row)?;
    *retained_bytes = retained_bytes
        .checked_add(row_bytes)
        .and_then(|bytes| bytes.checked_add(usize::from(!rows.is_empty())))
        .ok_or_else(|| resource_exhausted("optimizer response byte count overflow"))?;
    rows.push(row);
    Ok(())
}

fn scan_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    requested_rows: Option<usize>,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let (mut cursor, end) = namespace_range(transaction, namespace, prefix)?;
    let mut rows = Vec::new();
    loop {
        let remaining = requested_rows.map_or(usize::MAX, |limit| limit.saturating_sub(rows.len()));
        if remaining == 0 { break; }
        let page_limit = remaining.min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() { break; }
        let mut successor = page.last().expect("nonempty page").0.key_bytes().to_vec();
        successor.push(0);
        cursor = owner_key(transaction, namespace, &successor)?;
        rows.extend(page);
    }
    Ok(rows)
}

fn id_from_index(raw: &[u8]) -> io::Result<&str> {
    std::str::from_utf8(raw).map_err(|_| invalid_data("optimizer index value is malformed"))
}

fn joined_action(action: &Action, proposal: &Proposal, include_title: bool) -> io::Result<Value> {
    let mut object = serde_json::to_value(action)
        .map_err(|_| invalid_data("optimizer action cannot be projected"))?
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("optimizer action projection is malformed"))?;
    if include_title {
        object.insert("p_title".to_owned(), Value::String(proposal.title.clone()));
    }
    object.insert(
        "p_action_type".to_owned(),
        Value::String(proposal.action_type.clone()),
    );
    object.insert(
        "p_action_args".to_owned(),
        Value::String(proposal.action_args.clone()),
    );
    object.insert(
        "p_status".to_owned(),
        Value::String(proposal.status.clone()),
    );
    Ok(Value::Object(object))
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate(transaction.owner_user_id())?;
    match request {
        Request::ProposalCreate {
            proposal,
            updated_at_ms,
        } => {
            if get_proposal(database, transaction, &proposal.id)?.is_some() {
                return Err(conflict("optimizer proposal already exists"));
            }
            increment_count(
                database,
                transaction,
                OPTIMIZER_PROPOSAL_COUNT_NAMESPACE,
            )?;
            put_proposal(database, transaction, proposal, *updated_at_ms)?;
            for key in [
                ordered_key(
                    transaction,
                    OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE,
                    &proposal.created_at,
                    &proposal.id,
                    true,
                )?,
                proposal_status_index_key(transaction, proposal)?,
            ] {
                database.entity_put(transaction, key, proposal.id.as_bytes().to_vec())?;
            }
            database.entity_put(
                transaction,
                proposal_status_key(transaction, &proposal.id)?,
                proposal.status.as_bytes().to_vec(),
            )?;
            Ok(Some(
                serde_json::to_vec(&serde_json::json!({"proposal_id": proposal.id})).unwrap(),
            ))
        }
        Request::ProposalUpdate {
            id,
            status,
            reason,
            updated_at_ms,
        } => {
            let Some(mut proposal) = get_proposal(database, transaction, id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            let old_status_index = proposal_status_index_key(transaction, &proposal)?;
            proposal.status.clone_from(status);
            proposal.status_reason.clone_from(reason);
            put_proposal(database, transaction, &proposal, *updated_at_ms)?;
            database.entity_delete(transaction, old_status_index)?;
            database.entity_put(
                transaction,
                proposal_status_index_key(transaction, &proposal)?,
                proposal.id.as_bytes().to_vec(),
            )?;
            database.entity_put(
                transaction,
                proposal_status_key(transaction, &proposal.id)?,
                proposal.status.as_bytes().to_vec(),
            )?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::ProposalGet { id } => {
            encode_optional(get_proposal(database, transaction, id)?.as_ref())
        }
        Request::ProposalList { status, limit } => {
            let mut result = Vec::new();
            let mut retained_bytes = 2_usize;
            let (namespace, prefix) = if status.is_empty() {
                (OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE, Vec::new())
            } else {
                (
                    OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE,
                    proposal_status_prefix(status),
                )
            };
            for (_, raw_id) in scan_index(
                database,
                transaction,
                namespace,
                &prefix,
                Some(*limit),
            )? {
                let id = id_from_index(&raw_id)?;
                let proposal = get_proposal(database, transaction, id)?
                    .ok_or_else(|| invalid_data("optimizer proposal index is inconsistent"))?;
                if !status.is_empty() && proposal.status != *status {
                    return Err(invalid_data(
                        "optimizer proposal status index is inconsistent",
                    ));
                }
                push_requested_row(
                    &mut result,
                    &mut retained_bytes,
                    serde_json::to_value(proposal).unwrap(),
                )?;
                if result.len() == *limit {
                    break;
                }
            }
            Ok(Some(encode_rows(&result)?))
        }
        Request::ActionRecord {
            action,
            updated_at_ms,
        } => {
            if get_proposal(database, transaction, &action.proposal_id)?.is_none() {
                return Err(invalid_data("Optimizer proposal does not exist"));
            }
            if get_action(database, transaction, &action.id)?.is_some() {
                return Err(conflict("optimizer action already exists"));
            }
            increment_count(
                database,
                transaction,
                OPTIMIZER_ACTION_COUNT_NAMESPACE,
            )?;
            put_action(database, transaction, action, *updated_at_ms)?;
            for key in [
                ordered_key(
                    transaction,
                    OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE,
                    &action.applied_at,
                    &action.id,
                    true,
                )?,
                ordered_key(
                    transaction,
                    OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE,
                    &action.applied_at,
                    &action.id,
                    true,
                )?,
                proposal_action_index_key(
                    transaction,
                    &action.proposal_id,
                    &action.applied_at,
                    &action.id,
                )?,
            ] {
                database.entity_put(transaction, key, action.id.as_bytes().to_vec())?;
            }
            database.entity_put(
                transaction,
                ordered_key(
                    transaction,
                    OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE,
                    &action.expires_at,
                    &action.id,
                    false,
                )?,
                serde_json::to_vec(&ExpiryIndexValue {
                    id: action.id.clone(),
                    proposal_id: action.proposal_id.clone(),
                    expires_at: action.expires_at.clone(),
                })
                .map_err(|_| invalid_input("optimizer expiry index cannot be encoded"))?,
            )?;
            Ok(Some(
                serde_json::to_vec(&serde_json::json!({"log_id": action.id})).unwrap(),
            ))
        }
        Request::ActionOutcome {
            id,
            metric,
            recorded_at,
            updated_at_ms,
        } => {
            let Some(mut action) = get_action(database, transaction, id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            action.outcome_metric.clone_from(metric);
            action.outcome_recorded_at.clone_from(recorded_at);
            put_action(database, transaction, &action, *updated_at_ms)?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::ActionRevert {
            id,
            reverted_at,
            reason,
            updated_at_ms,
        } => {
            let Some(mut action) = get_action(database, transaction, id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            action.reverted_at.clone_from(reverted_at);
            action.revert_reason.clone_from(reason);
            put_action(database, transaction, &action, *updated_at_ms)?;
            database.entity_delete(
                transaction,
                ordered_key(
                    transaction,
                    OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE,
                    &action.applied_at,
                    &action.id,
                    true,
                )?,
            )?;
            database.entity_delete(
                transaction,
                ordered_key(
                    transaction,
                    OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE,
                    &action.expires_at,
                    &action.id,
                    false,
                )?,
            )?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::ActionList {
            include_reverted,
            limit,
        } => {
            let mut result = Vec::new();
            let mut retained_bytes = 2_usize;
            let namespace = if *include_reverted {
                OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE
            } else {
                OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE
            };
            for (_, raw_id) in scan_index(
                database,
                transaction,
                namespace,
                b"",
                Some(*limit),
            )? {
                let id = id_from_index(&raw_id)?;
                let action = get_action(database, transaction, id)?
                    .ok_or_else(|| invalid_data("optimizer action index is inconsistent"))?;
                if !include_reverted && !action.reverted_at.is_empty() {
                    return Err(invalid_data(
                        "optimizer active-action index is inconsistent",
                    ));
                }
                let proposal = get_proposal(database, transaction, &action.proposal_id)?
                    .ok_or_else(|| invalid_data("optimizer action proposal is missing"))?;
                push_requested_row(
                    &mut result,
                    &mut retained_bytes,
                    joined_action(&action, &proposal, true)?,
                )?;
                if result.len() == *limit {
                    break;
                }
            }
            Ok(Some(encode_rows(&result)?))
        }
        Request::ActionExpired { now_iso } => {
            let mut result = Vec::new();
            let mut retained_bytes = 2_usize;
            for (_, raw_index) in scan_index(
                database,
                transaction,
                OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE,
                b"",
                None,
            )? {
                let indexed: ExpiryIndexValue = serde_json::from_slice(&raw_index)
                    .map_err(|_| invalid_data("optimizer expiry index value is malformed"))?;
                if !valid_text(&indexed.id, 128, true)
                    || !valid_text(&indexed.proposal_id, 128, true)
                    || !valid_text(&indexed.expires_at, 64, true)
                {
                    return Err(invalid_data("optimizer expiry index value is inconsistent"));
                }
                if indexed.expires_at > *now_iso {
                    break;
                }
                let status_raw = database
                    .entity_get(
                        transaction,
                        &proposal_status_key(transaction, &indexed.proposal_id)?,
                    )?
                    .ok_or_else(|| invalid_data("optimizer proposal status is missing"))?;
                let status = std::str::from_utf8(&status_raw)
                    .map_err(|_| invalid_data("optimizer proposal status is malformed"))?;
                if status != "applied" {
                    continue;
                }
                let action = get_action(database, transaction, &indexed.id)?
                    .ok_or_else(|| invalid_data("optimizer expiry index is inconsistent"))?;
                if action.expires_at != indexed.expires_at
                    || action.proposal_id != indexed.proposal_id
                    || !action.reverted_at.is_empty()
                {
                    return Err(invalid_data(
                        "optimizer active-expiry index is inconsistent",
                    ));
                }
                let proposal = get_proposal(database, transaction, &action.proposal_id)?
                    .ok_or_else(|| invalid_data("optimizer action proposal is missing"))?;
                if proposal.status != status {
                    return Err(invalid_data(
                        "optimizer proposal status projection is inconsistent",
                    ));
                }
                push_requested_row(
                    &mut result,
                    &mut retained_bytes,
                    joined_action(&action, &proposal, false)?,
                )?;
            }
            Ok(Some(encode_rows(&result)?))
        }
        Request::ActionForProposal { proposal_id } => {
            let prefix = proposal_action_prefix(proposal_id);
            let Some((_, raw_id)) = scan_index(
                database,
                transaction,
                OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE,
                &prefix,
                Some(1),
            )?
            .into_iter()
            .next() else {
                return Ok(None);
            };
            let id = id_from_index(&raw_id)?;
            encode_optional(get_action(database, transaction, id)?.as_ref())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{ascending_text, descending_text};

    fn ascending(value: &str) -> Vec<u8> {
        let mut encoded = Vec::new();
        ascending_text(&mut encoded, value);
        encoded
    }

    fn descending(value: &str) -> Vec<u8> {
        let mut encoded = Vec::new();
        descending_text(&mut encoded, value);
        encoded
    }

    #[test]
    fn text_index_encoding_is_prefix_safe_and_preserves_binary_order() {
        let values = ["", "\0", "a", "a\0", "aa", "b", "界"];
        for left in values {
            for right in values {
                assert_eq!(ascending(left).cmp(&ascending(right)), left.cmp(right));
                assert_eq!(descending(left).cmp(&descending(right)), right.cmp(left));
                if left != right {
                    assert_ne!(ascending(left), ascending(right));
                    assert_ne!(descending(left), descending(right));
                }
            }
        }
    }
}

#[cfg(test)]
mod demand_tests {
    use super::*;
    use serde_json::json;

    fn proposal(ordinal:u64) -> Proposal {
        Proposal {user_id:11,id:format!("proposal-{ordinal:05}"),
            created_at:format!("2026-10-12T{:02}:{:02}:{:02}Z",ordinal/3600,(ordinal%3600)/60,ordinal%60),
            title:"Demand fixture".to_owned(),rationale:"retained history".to_owned(),
            action_type:"observe".to_owned(),action_args:"{}".to_owned(),severity:"low".to_owned(),
            confidence:0.5,evidence:"[]".to_owned(),status:"applied".to_owned(),status_reason:String::new()}
    }
    fn action(ordinal:u64) -> Action {
        Action {user_id:11,id:format!("action-{ordinal:05}"),proposal_id:"proposal-00000".to_owned(),
            applied_at:format!("2026-10-12T{:02}:{:02}:{:02}Z",3+ordinal/3600,(ordinal%3600)/60,ordinal%60),
            expires_at:"2026-10-13T00:00:00Z".to_owned(),pre_metric:"{}".to_owned(),
            outcome_metric:String::new(),outcome_recorded_at:String::new(),reverted_at:String::new(),revert_reason:String::new()}
    }
    #[test]
    fn optimizer_history_demand_exceeds_former_quotas_reopens_and_restores_without_losing_audit_rows() {
        let directory = tempfile::tempdir().unwrap();
        let started = std::time::Instant::now();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for start in (0..4201_u64).step_by(64) {
            let mut tx = database.begin(7,11).unwrap();
            for ordinal in start..(start+64).min(4201) {
                execute(&database,&mut tx,&Request::ProposalCreate{proposal:proposal(ordinal),updated_at_ms:1000}).unwrap();
            }
            database.commit(tx).unwrap();
        }
        for start in (0..2201_u64).step_by(64) {
            let mut tx = database.begin(7,11).unwrap();
            for ordinal in start..(start+64).min(2201) {
                execute(&database,&mut tx,&Request::ActionRecord{action:action(ordinal),updated_at_ms:1001}).unwrap();
            }
            database.commit(tx).unwrap();
        }
        let mut imported_proposal = serde_json::to_value(proposal(4201)).unwrap();
        imported_proposal.as_object_mut().unwrap().remove("user_id");
        let restore_proposal = json!({"table":"proposals","document":imported_proposal,"updated_at_ms":1002});
        assert_eq!(crate::optimizer_migration::execute(&mut database,7,11,"migration.optimizer.restore",restore_proposal.clone()).unwrap()["inserted"],true);
        assert_eq!(crate::optimizer_migration::execute(&mut database,7,11,"migration.optimizer.restore",restore_proposal).unwrap()["inserted"],false);
        let mut imported_action = serde_json::to_value(action(2201)).unwrap();
        imported_action.as_object_mut().unwrap().remove("user_id");
        assert_eq!(crate::optimizer_migration::execute(&mut database,7,11,"migration.optimizer.restore",
            json!({"table":"actions","document":imported_action,"updated_at_ms":1003})).unwrap()["inserted"],true);
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = database.begin(7,11).unwrap();
        assert_eq!(read_count(&database,&mut tx,OPTIMIZER_PROPOSAL_COUNT_NAMESPACE).unwrap(),4202);
        assert_eq!(read_count(&database,&mut tx,OPTIMIZER_ACTION_COUNT_NAMESPACE).unwrap(),2202);
        for ordinal in [0,4095,4096,4201] { assert!(get_proposal(&database,&mut tx,&proposal(ordinal).id).unwrap().is_some()); }
        for ordinal in [0,2047,2048,2201] { assert!(get_action(&database,&mut tx,&action(ordinal).id).unwrap().is_some()); }
        let proposals:Value = serde_json::from_slice(&execute(&database,&mut tx,&Request::ProposalList{status:String::new(),limit:1001}).unwrap().unwrap()).unwrap();
        assert_eq!(proposals.as_array().unwrap().len(),1001);
        assert_eq!(proposals[0]["id"],"proposal-04201");
        let actions:Value = serde_json::from_slice(&execute(&database,&mut tx,&Request::ActionList{include_reverted:true,limit:1001}).unwrap().unwrap()).unwrap();
        assert_eq!(actions.as_array().unwrap().len(),1001);
        assert_eq!(actions[0]["id"],"action-02201");
        let latest:Value = serde_json::from_slice(&execute(&database,&mut tx,&Request::ActionForProposal{proposal_id:"proposal-00000".to_owned()}).unwrap().unwrap()).unwrap();
        assert_eq!(latest["id"],"action-02201");
        let expired:Value = serde_json::from_slice(&execute(&database,&mut tx,&Request::ActionExpired{now_iso:"2026-10-14T00:00:00Z".to_owned()}).unwrap().unwrap()).unwrap();
        assert_eq!(expired.as_array().unwrap().len(),2202);
        drop(tx);
        let mut foreign = database.begin(7,12).unwrap();
        assert!(get_proposal(&database,&mut foreign,"proposal-00000").unwrap().is_none());
        assert!(get_action(&database,&mut foreign,"action-00000").unwrap().is_none());
        drop(foreign);
        let mut overflow = database.begin(7,11).unwrap();
        let counter_key = owner_key(&overflow,OPTIMIZER_PROPOSAL_COUNT_NAMESPACE,COUNT_KEY).unwrap();
        database.entity_put(&mut overflow,counter_key,u64::MAX.to_le_bytes().to_vec()).unwrap();
        assert_eq!(execute(&database,&mut overflow,&Request::ProposalCreate{proposal:proposal(9999),updated_at_ms:1004}).unwrap_err().kind(),io::ErrorKind::InvalidData);
        eprintln!("optimizer_demand: 4202 proposals,2202 actions,complete expired audit, {:?}",started.elapsed());
    }
}
