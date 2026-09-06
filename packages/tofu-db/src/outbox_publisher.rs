//! Bounded logical-outbox delivery without borrowing the database writer.
//!
//! `publish_batch` accepts only owned records previously fetched from the
//! authority, performs all sink I/O through `LogicalOutboxSink`, and returns
//! durable receipts for a later ordered ACK transaction. A lost sink response
//! deliberately leaves the authority row pending so an idempotent sink can
//! resolve the retry without holding the foreground writer during I/O.

use std::collections::BTreeSet;
use std::io;

use crate::authority::{
    PendingLogicalOutboxRecord, MAX_LOGICAL_OUTBOX_FETCH_BYTES, MAX_LOGICAL_OUTBOX_FETCH_RECORDS,
};
use crate::logical_outbox::SealedLogicalOutboxRecord;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct LogicalOutboxOwnerScope {
    pub tenant_id: u64,
    pub owner_user_id: u64,
}

impl LogicalOutboxOwnerScope {
    pub fn new(tenant_id: u64, owner_user_id: u64) -> io::Result<Self> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input("logical outbox owner scope is zero"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LogicalOutboxPublishBudget {
    pub max_records: usize,
    pub max_bytes: usize,
}

impl LogicalOutboxPublishBudget {
    pub fn new(max_records: usize, max_bytes: usize) -> io::Result<Self> {
        if max_records == 0
            || max_records > MAX_LOGICAL_OUTBOX_FETCH_RECORDS
            || max_bytes == 0
            || max_bytes > MAX_LOGICAL_OUTBOX_FETCH_BYTES
        {
            return Err(invalid_input("invalid logical outbox publish budget"));
        }
        Ok(Self {
            max_records,
            max_bytes,
        })
    }
}

impl Default for LogicalOutboxPublishBudget {
    fn default() -> Self {
        Self {
            max_records: 16,
            max_bytes: MAX_LOGICAL_OUTBOX_FETCH_BYTES,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DurableLogicalOutboxReceipt {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub sequence: u64,
    pub event_id: [u8; 32],
}

impl DurableLogicalOutboxReceipt {
    fn matches(self, record: &SealedLogicalOutboxRecord) -> bool {
        self.tenant_id == record.identity.tenant_id
            && self.owner_user_id == record.identity.owner_user_id
            && self.sequence == record.identity.sequence
            && self.event_id == record.event_id
    }
}

pub trait LogicalOutboxSink: Send {
    /// Return only after this exact record is durable or was already durable.
    fn append_durable(
        &mut self,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<DurableLogicalOutboxReceipt>;

    /// True only when this instance must be reopened before another append.
    fn is_restart_required(&self) -> bool {
        false
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LogicalOutboxPublishResult {
    pub receipts: Vec<DurableLogicalOutboxReceipt>,
    pub published_bytes: usize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LogicalOutboxPublisherMetrics {
    pub batch_attempts: u64,
    pub successful_batches: u64,
    pub published_records: u64,
    pub published_bytes: u64,
    pub failures: u64,
}

pub struct LogicalOutboxPublisher<S> {
    sink: S,
    budget: LogicalOutboxPublishBudget,
    metrics: LogicalOutboxPublisherMetrics,
}

impl<S: LogicalOutboxSink> LogicalOutboxPublisher<S> {
    pub fn new(sink: S, budget: LogicalOutboxPublishBudget) -> io::Result<Self> {
        LogicalOutboxPublishBudget::new(budget.max_records, budget.max_bytes)?;
        Ok(Self {
            sink,
            budget,
            metrics: LogicalOutboxPublisherMetrics::default(),
        })
    }

    pub fn publish(
        &mut self,
        pending: &[PendingLogicalOutboxRecord],
    ) -> io::Result<LogicalOutboxPublishResult> {
        self.metrics.batch_attempts = self.metrics.batch_attempts.saturating_add(1);
        match publish_batch(&mut self.sink, pending, self.budget) {
            Ok(result) => {
                self.metrics.successful_batches = self.metrics.successful_batches.saturating_add(1);
                self.metrics.published_records = self
                    .metrics
                    .published_records
                    .saturating_add(result.receipts.len() as u64);
                self.metrics.published_bytes = self
                    .metrics
                    .published_bytes
                    .saturating_add(result.published_bytes as u64);
                Ok(result)
            }
            Err(error) => {
                self.metrics.failures = self.metrics.failures.saturating_add(1);
                Err(error)
            }
        }
    }

    pub fn metrics(&self) -> LogicalOutboxPublisherMetrics {
        self.metrics
    }

    pub fn sink_restart_required(&self) -> bool {
        self.sink.is_restart_required()
    }

    pub fn into_sink(self) -> S {
        self.sink
    }
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub fn publish_batch<S: LogicalOutboxSink + ?Sized>(
    sink: &mut S,
    pending: &[PendingLogicalOutboxRecord],
    budget: LogicalOutboxPublishBudget,
) -> io::Result<LogicalOutboxPublishResult> {
    let published_bytes = validate_batch(pending, budget)?;
    let mut receipts = Vec::with_capacity(pending.len());
    for pending_record in pending {
        let receipt = sink.append_durable(&pending_record.record)?;
        if !receipt.matches(&pending_record.record) {
            return Err(invalid_data("logical outbox sink receipt identity differs"));
        }
        receipts.push(receipt);
    }
    Ok(LogicalOutboxPublishResult {
        receipts,
        published_bytes,
    })
}

pub(crate) fn validate_batch(
    pending: &[PendingLogicalOutboxRecord],
    budget: LogicalOutboxPublishBudget,
) -> io::Result<usize> {
    LogicalOutboxPublishBudget::new(budget.max_records, budget.max_bytes)?;
    if pending.len() > budget.max_records {
        return Err(invalid_input(
            "logical outbox batch exceeds its record budget",
        ));
    }
    let mut published_bytes = 0_usize;
    let mut previous_identity: Option<(u64, u64, u64)> = None;
    let mut event_ids = BTreeSet::new();
    for pending_record in pending {
        pending_record.record.validate()?;
        let encoded_len = pending_record.record.encoded_len()?;
        if pending_record.record_bytes != encoded_len as u64 {
            return Err(invalid_data("logical outbox batch byte witness differs"));
        }
        published_bytes = published_bytes
            .checked_add(encoded_len)
            .filter(|bytes| *bytes <= budget.max_bytes)
            .ok_or_else(|| invalid_input("logical outbox batch exceeds its byte budget"))?;
        let identity = &pending_record.record.identity;
        if let Some((tenant_id, owner_user_id, sequence)) = previous_identity {
            if identity.tenant_id != tenant_id
                || identity.owner_user_id != owner_user_id
                || sequence.checked_add(1) != Some(identity.sequence)
            {
                return Err(invalid_data(
                    "logical outbox batch scope or sequence is not contiguous",
                ));
            }
        }
        previous_identity = Some((
            identity.tenant_id,
            identity.owner_user_id,
            identity.sequence,
        ));
        if !event_ids.insert(pending_record.record.event_id) {
            return Err(invalid_data(
                "logical outbox batch repeats an event identity",
            ));
        }
    }

    Ok(published_bytes)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::authority::AuthorityDatabase;
    use crate::entity::EntityKey;
    use crate::logical_outbox::{LogicalOutboxCapture, LogicalOutboxCipher, LogicalOutboxIdentity};
    use crate::outbox_sink::EngineLogicalOutboxSink;

    #[derive(Default)]
    struct MemorySink {
        durable: BTreeMap<(u64, u64, u64), Vec<u8>>,
        lose_first_ack: bool,
        append_calls: usize,
    }

    impl LogicalOutboxSink for MemorySink {
        fn append_durable(
            &mut self,
            record: &SealedLogicalOutboxRecord,
        ) -> io::Result<DurableLogicalOutboxReceipt> {
            self.append_calls += 1;
            let key = (
                record.identity.tenant_id,
                record.identity.owner_user_id,
                record.identity.sequence,
            );
            let encoded = record.encode()?;
            match self.durable.get(&key) {
                Some(existing) if existing != &encoded => {
                    return Err(invalid_data("sink sequence contains another record"));
                }
                Some(_) => {}
                None => {
                    self.durable.insert(key, encoded);
                    if self.lose_first_ack {
                        self.lose_first_ack = false;
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "simulated lost sink acknowledgement",
                        ));
                    }
                }
            }
            Ok(DurableLogicalOutboxReceipt {
                tenant_id: record.identity.tenant_id,
                owner_user_id: record.identity.owner_user_id,
                sequence: record.identity.sequence,
                event_id: record.event_id,
            })
        }
    }

    fn sealed(sequence: u64) -> PendingLogicalOutboxRecord {
        let cipher = LogicalOutboxCipher::new(&[41; 32]);
        let record = cipher
            .seal(
                LogicalOutboxIdentity {
                    sequence,
                    tenant_id: 7,
                    owner_user_id: 11,
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: format!("request-{sequence}"),
                    request_digest: [sequence as u8; 32],
                    command_id: Some(format!("command-{sequence}")),
                    committed_at_ms: 4_000 + sequence,
                },
                b"transaction IR",
            )
            .unwrap();
        PendingLogicalOutboxRecord {
            record_bytes: record.encoded_len().unwrap() as u64,
            record,
        }
    }

    #[test]
    fn batch_is_fully_validated_before_the_first_sink_side_effect() {
        let mut sink = MemorySink::default();
        let invalid = vec![sealed(1), sealed(3)];
        assert_eq!(
            publish_batch(&mut sink, &invalid, LogicalOutboxPublishBudget::default())
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(sink.append_calls, 0);
        assert!(sink.durable.is_empty());

        let mut wrong_bytes = sealed(1);
        wrong_bytes.record_bytes += 1;
        assert!(publish_batch(
            &mut sink,
            &[wrong_bytes],
            LogicalOutboxPublishBudget::default(),
        )
        .is_err());
        assert_eq!(sink.append_calls, 0);
    }

    #[test]
    fn publish_budget_is_hard_and_checked_before_sink_io() {
        let records = vec![sealed(1), sealed(2)];
        let mut sink = MemorySink::default();
        let one_record_budget = LogicalOutboxPublishBudget::new(1, 1024).unwrap();
        assert!(publish_batch(&mut sink, &records, one_record_budget).is_err());
        assert_eq!(sink.append_calls, 0);
        let too_few_bytes = LogicalOutboxPublishBudget::new(2, 1).unwrap();
        assert!(publish_batch(&mut sink, &records, too_few_bytes).is_err());
        assert_eq!(sink.append_calls, 0);
    }

    #[test]
    fn durable_sink_lost_ack_retries_without_losing_the_authority_record() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[41; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "semantic", b"artifact").unwrap(),
                b"created".to_vec(),
            )
            .unwrap();
        database
            .logical_outbox_capture(
                &mut transaction,
                LogicalOutboxCapture {
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: "request-1".to_owned(),
                    request_digest: [6; 32],
                    command_id: Some("command-1".to_owned()),
                    committed_at_ms: 4_000,
                    clear_payload: b"transaction IR".to_vec(),
                },
            )
            .unwrap();
        database.commit(transaction).unwrap();

        let pending = database.logical_outbox_pending(7, 11, 16).unwrap();
        let mut sink = MemorySink {
            lose_first_ack: true,
            ..MemorySink::default()
        };
        assert_eq!(
            publish_batch(&mut sink, &pending, LogicalOutboxPublishBudget::default(),)
                .unwrap_err()
                .kind(),
            io::ErrorKind::Interrupted
        );
        assert_eq!(sink.durable.len(), 1);
        assert_eq!(
            database
                .logical_outbox_status(7, 11)
                .unwrap()
                .published_sequence,
            0
        );

        let retry = database.logical_outbox_pending(7, 11, 16).unwrap();
        let published =
            publish_batch(&mut sink, &retry, LogicalOutboxPublishBudget::default()).unwrap();
        assert_eq!(published.receipts.len(), 1);
        assert_eq!(sink.durable.len(), 1);
        let receipt = published.receipts[0];
        database
            .logical_outbox_acknowledge(
                receipt.tenant_id,
                receipt.owner_user_id,
                receipt.sequence,
                receipt.event_id,
            )
            .unwrap();
        assert!(database
            .logical_outbox_pending(7, 11, 16)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn mismatched_sink_receipt_never_authorizes_an_ack() {
        struct WrongReceiptSink;
        impl LogicalOutboxSink for WrongReceiptSink {
            fn append_durable(
                &mut self,
                record: &SealedLogicalOutboxRecord,
            ) -> io::Result<DurableLogicalOutboxReceipt> {
                Ok(DurableLogicalOutboxReceipt {
                    tenant_id: record.identity.tenant_id,
                    owner_user_id: record.identity.owner_user_id,
                    sequence: record.identity.sequence,
                    event_id: [0; 32],
                })
            }
        }
        assert_eq!(
            publish_batch(
                &mut WrongReceiptSink,
                &[sealed(1)],
                LogicalOutboxPublishBudget::default(),
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn publisher_metrics_count_success_and_failure_without_user_content() {
        let mut publisher = LogicalOutboxPublisher::new(
            MemorySink {
                lose_first_ack: true,
                ..MemorySink::default()
            },
            LogicalOutboxPublishBudget::default(),
        )
        .unwrap();
        let pending = [sealed(1)];
        assert!(publisher.publish(&pending).is_err());
        assert!(publisher.publish(&pending).is_ok());
        assert_eq!(
            publisher.metrics(),
            LogicalOutboxPublisherMetrics {
                batch_attempts: 2,
                successful_batches: 1,
                published_records: 1,
                published_bytes: pending[0].record_bytes,
                failures: 1,
            }
        );
    }

    #[test]
    fn concrete_sink_lost_response_replays_then_authority_ack_survives_reopen() {
        struct LoseResponseOnce {
            sink: EngineLogicalOutboxSink,
            lose_response: bool,
        }
        impl LogicalOutboxSink for LoseResponseOnce {
            fn append_durable(
                &mut self,
                record: &SealedLogicalOutboxRecord,
            ) -> io::Result<DurableLogicalOutboxReceipt> {
                let receipt = self.sink.append_durable(record)?;
                if self.lose_response {
                    self.lose_response = false;
                    return Err(io::Error::new(
                        io::ErrorKind::Interrupted,
                        "simulated response loss after durable sink append",
                    ));
                }
                Ok(receipt)
            }
        }

        let root = tempfile::tempdir().unwrap();
        let authority_path = root.path().join("authority");
        let sink_path = root.path().join("sink");
        let mut database = AuthorityDatabase::initialize(&authority_path).unwrap();
        database
            .configure_logical_outbox(&[41; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "semantic", b"artifact").unwrap(),
                b"created".to_vec(),
            )
            .unwrap();
        database
            .logical_outbox_capture(
                &mut transaction,
                LogicalOutboxCapture {
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: "request-1".to_owned(),
                    request_digest: [6; 32],
                    command_id: Some("command-1".to_owned()),
                    committed_at_ms: 4_000,
                    clear_payload: b"transaction IR".to_vec(),
                },
            )
            .unwrap();
        database.commit(transaction).unwrap();
        let pending = database.logical_outbox_pending(7, 11, 16).unwrap();
        let mut sink = LoseResponseOnce {
            sink: EngineLogicalOutboxSink::initialize(&sink_path, 7, 11, 1024 * 1024).unwrap(),
            lose_response: true,
        };
        assert!(
            publish_batch(&mut sink, &pending, LogicalOutboxPublishBudget::default(),).is_err()
        );
        assert_eq!(sink.sink.status().durable_sequence, 1);
        assert_eq!(
            database
                .logical_outbox_status(7, 11)
                .unwrap()
                .published_sequence,
            0
        );
        let result = publish_batch(
            &mut sink,
            &database.logical_outbox_pending(7, 11, 16).unwrap(),
            LogicalOutboxPublishBudget::default(),
        )
        .unwrap();
        let receipt = result.receipts[0];
        database
            .logical_outbox_acknowledge(
                receipt.tenant_id,
                receipt.owner_user_id,
                receipt.sequence,
                receipt.event_id,
            )
            .unwrap();
        drop(database);
        drop(sink);

        let mut reopened_authority = AuthorityDatabase::open(&authority_path).unwrap();
        reopened_authority
            .configure_logical_outbox(&[41; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert!(reopened_authority
            .logical_outbox_pending(7, 11, 16)
            .unwrap()
            .is_empty());
        let reopened_sink = EngineLogicalOutboxSink::open(&sink_path, 7, 11, 1024 * 1024).unwrap();
        assert_eq!(reopened_sink.status().durable_sequence, 1);
    }
}
