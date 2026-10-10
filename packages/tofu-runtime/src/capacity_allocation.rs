//! Composition-owned aggregate observations from one immutable capacity probe.
//! Private RAII observations remain held until joined close; no quota, global registry,
//! waiting queue, background worker or per-request host probe is introduced.
use crate::capacity::{Demand, Error, Snapshot};
use std::sync::{Arc, Mutex};
struct State {
    bytes: usize,
    pools: usize,
    workers: usize,
    cpu_units: usize,
    closed: bool,
}
pub struct Allocator {
    snapshot: Snapshot,
    limit: Option<usize>,
    cpu_limit: usize,
    state: Mutex<State>,
}
#[derive(Debug, PartialEq, Eq)]
pub struct AllocationSnapshot {
    pub weighted_bytes: usize,
    pub pools: usize,
    pub workers: usize,
    pub cpu_units: usize,
    pub cpu_limit: usize,
    pub limit: Option<usize>,
    pub closed: bool,
}
pub(crate) struct Lease {
    allocator: Arc<Allocator>,
    bytes: usize,
    cpu_units: usize,
    budget: crate::executor::Budget,
}
impl Allocator {
    pub fn new(snapshot: Snapshot, memory_divisor: usize) -> Result<Arc<Self>, Error> {
        if memory_divisor == 0 {
            return Err(Error::InvalidDemand);
        }
        let limit = snapshot
            .memory_available_bytes
            .map(|n| usize::try_from(n / memory_divisor as u64).map_err(|_| Error::Overflow))
            .transpose()?;
        let cpu_limit = snapshot
            .effective_cpus
            .max(1)
            .checked_mul(1000)
            .ok_or(Error::Overflow)?;
        Ok(Arc::new(Self {
            snapshot,
            cpu_limit,
            limit,
            state: Mutex::new(State {
                bytes: 0,
                pools: 0,
                workers: 0,
                cpu_units: 0,
                closed: false,
            }),
        }))
    }
    pub(crate) fn reserve(self: &Arc<Self>, demand: Demand) -> Result<Lease, Error> {
        let mut state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        if state.closed {
            return Err(Error::Closed);
        }
        // Each owner sizes reuse from the same launch snapshot. Aggregate
        // observations grant no pool-admission permit or free-capacity gate.
        let budget = self.snapshot.executor_budget(demand)?;
        let cpu_units = budget
            .workers
            .checked_mul(demand.cpu_units_per_worker)
            .ok_or(Error::Overflow)?;
        let bytes = budget
            .workers
            .checked_mul(demand.worker_bytes)
            .and_then(|n| n.checked_add(budget.max_inflight_bytes))
            .ok_or(Error::Overflow)?;
        let total = state.bytes.checked_add(bytes).ok_or(Error::Overflow)?;
        let pools = state.pools.checked_add(1).ok_or(Error::Overflow)?;
        let workers = state
            .workers
            .checked_add(budget.workers)
            .ok_or(Error::Overflow)?;
        let total_cpu_units = state
            .cpu_units
            .checked_add(cpu_units)
            .ok_or(Error::Overflow)?;
        state.workers = workers;
        state.cpu_units = total_cpu_units;
        state.bytes = total;
        state.pools = pools;
        Ok(Lease {
            allocator: self.clone(),
            bytes,
            cpu_units,
            budget,
        })
    }
    /// Refuse new pool allocation; existing leases remain charged until join.
    pub fn close(&self) -> Result<(), Error> {
        self.state
            .lock()
            .map_err(|_| Error::StateUnavailable)?
            .closed = true;
        Ok(())
    }
    pub fn snapshot(&self) -> Result<AllocationSnapshot, Error> {
        let state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        Ok(AllocationSnapshot {
            weighted_bytes: state.bytes,
            pools: state.pools,
            workers: state.workers,
            cpu_units: state.cpu_units,
            cpu_limit: self.cpu_limit,
            limit: self.limit,
            closed: state.closed,
        })
    }
}
impl Lease {
    pub(crate) fn budget(&self) -> crate::executor::Budget {
        self.budget
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut state = self
            .allocator
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        state.bytes -= self.bytes;
        state.pools -= 1;
        state.workers -= self.budget.workers;
        state.cpu_units -= self.cpu_units;
    }
}
