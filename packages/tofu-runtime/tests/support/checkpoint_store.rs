//! Atomic in-memory checkpoint store for transition/fault specifications.
use serde_json::Value;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};
use tofu_runtime::checkpoint_journal::{Error, Header, SnapshotWrite, Store};
#[derive(Default)]
pub struct MemoryStore {
    pub record: Mutex<Option<(u64, Value, Option<Value>)>>,
    pub fail_before: AtomicBool,
    pub fail_after: AtomicBool,
    pub snapshot_reads: Mutex<usize>,
    pub snapshot_writes: Mutex<usize>,
    pub writer: Mutex<Option<Value>>,
    pub writer_dead: AtomicBool,
}
impl Store for MemoryStore {
    fn writer_process(&self) -> Option<Value> {
        self.writer.lock().unwrap().clone()
    }
    fn writer_quiescent(&self, _owner: u64, _task: &str, _identity: &Value) -> Result<bool, Error> {
        Ok(self.writer_dead.load(Ordering::Acquire))
    }
    fn header(&self, _owner: u64, _task: &str) -> Result<Option<Header>, Error> {
        Ok(self
            .record
            .lock()
            .unwrap()
            .as_ref()
            .map(|(version, value, _)| Header {
                version: *version,
                value: value.clone(),
            }))
    }
    fn compare_exchange(
        &self,
        _owner: u64,
        _task: &str,
        expected: u64,
        header: &Value,
        snapshot: SnapshotWrite<'_>,
    ) -> Result<u64, Error> {
        let mut record = self.record.lock().unwrap();
        if self.fail_before.swap(false, Ordering::AcqRel) {
            return Err(Error::Storage);
        }
        if record.as_ref().map_or(0, |v| v.0) != expected {
            return Err(Error::Conflict);
        }
        let state = match snapshot {
            SnapshotWrite::Keep => record.as_mut().and_then(|v| v.2.take()),
            SnapshotWrite::Replace(v) => {
                *self.snapshot_writes.lock().unwrap() += 1;
                Some(v.clone())
            }
        };
        *record = Some((expected + 1, header.clone(), state));
        if self.fail_after.swap(false, Ordering::AcqRel) {
            return Err(Error::Storage);
        }
        Ok(expected + 1)
    }
    fn snapshot(&self, _owner: u64, _task: &str, version: u64) -> Result<Option<Value>, Error> {
        *self.snapshot_reads.lock().unwrap() += 1;
        let record = self.record.lock().unwrap();
        let record = record.as_ref().ok_or(Error::Conflict)?;
        if record.0 != version {
            return Err(Error::Conflict);
        }
        Ok(record.2.clone())
    }
}
