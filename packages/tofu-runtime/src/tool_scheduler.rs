//! Composition-owned demand tool scheduling with atomic publication.
//! The qualified composition overlaps only explicitly classified reads and keeps
//! every other call in order. The concurrent composition (owner decision
//! 2026-10-28) starts every call at once; a call waits only for the earlier
//! calls the host's settled call order names. Scoped workers start before
//! publication and join before callback inputs expire.
use crate::{
    session::{ExecutionSession, Registry as Sessions},
    tool_contract::{AdmittedCall, Authorization, Permission, Registry},
    tool_execution::{Outcome, Scope, StartError},
};
use std::{
    collections::{BTreeSet, HashMap},
    io::{self, Write},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Condvar, Mutex,
    },
    thread::{Scope as ThreadScope, ScopedJoinHandle},
};
#[derive(Clone, Copy)]
pub struct Budget {
    /// Legacy idle hint; qualified calls start their own joined workers.
    pub workers: usize,
    /// Legacy compatibility field; never an admission quota.
    pub inflight_calls: usize,
    /// Legacy compatibility field; never an admission quota.
    pub inflight_bytes: usize,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Binding,
    Capacity,
    Closed,
    WorkerStart,
    StatePoisoned,
    ReentrantClose,
    ReentrantDispatch,
}
pub struct Invocation {
    pub position: usize,
    pub proof: AdmittedCall,
}
pub struct Batch<'a> {
    pub owner: u64,
    pub epoch: &'a str,
    pub task_id: &'a str,
    pub round: usize,
    pub parent: Arc<ExecutionSession>,
    pub sessions: &'a Sessions,
    pub handlers: &'a Registry,
    pub authorization: &'a dyn Authorization,
    pub calls: Vec<Invocation>,
}
pub struct ResultItem {
    pub position: usize,
    pub outcome: Result<Outcome, StartError>,
}
#[derive(Clone, Copy, Debug)]
pub struct Snapshot {
    pub inflight_calls: usize,
    pub inflight_bytes: usize,
    pub resident_workers: usize,
    pub closed: bool,
}
struct State {
    closed: bool,
    calls: usize,
    bytes: usize,
    callers: HashMap<std::thread::ThreadId, usize>,
    worker_callers: HashMap<std::thread::ThreadId, usize>,
    capacity_lease: Option<crate::capacity_allocation::Lease>,
}
type WorkerJob<'scope> = Box<dyn FnOnce() + Send + 'scope>;
type WorkerStart = for<'scope, 'env> fn(
    &'scope ThreadScope<'scope, 'env>,
    String,
    WorkerJob<'scope>,
) -> io::Result<ScopedJoinHandle<'scope, ()>>;
fn start_worker<'scope, 'env>(
    scope: &'scope ThreadScope<'scope, 'env>,
    name: String,
    run: WorkerJob<'scope>,
) -> io::Result<ScopedJoinHandle<'scope, ()>> {
    std::thread::Builder::new()
        .name(name)
        .spawn_scoped(scope, run)
}
/// Which calls of a batch start on their own joined worker.
enum Overlap {
    /// Explicitly qualified read names overlap; every other call keeps call order.
    Qualified(BTreeSet<String>),
    /// Every call overlaps; only the batch's declared call order is kept.
    Every,
}
pub struct Scheduler {
    overlap: Overlap,
    state: Mutex<State>,
    settled: Condvar,
    resident: Arc<AtomicUsize>,
    worker_start: WorkerStart,
}
struct Admission<'a> {
    scheduler: &'a Scheduler,
    calls: usize,
    bytes: usize,
    caller: std::thread::ThreadId,
}
impl Drop for Admission<'_> {
    fn drop(&mut self) {
        let mut state = self
            .scheduler
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        state.calls -= self.calls;
        state.bytes -= self.bytes;
        let count = state.callers.get_mut(&self.caller).unwrap();
        *count -= 1;
        if *count == 0 {
            state.callers.remove(&self.caller);
        }
        self.scheduler.settled.notify_all();
    }
}
struct Count {
    bytes: usize,
}
impl Write for Count {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(b.len())
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        Ok(b.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn weight(calls: &[Invocation], task_id: &str) -> Result<usize, Error> {
    let mut count = Count { bytes: 0 };
    for call in calls {
        count
            .write_all(task_id.as_bytes())
            .map_err(|_| Error::Capacity)?;
        count
            .write_all(call.proof.name().as_bytes())
            .map_err(|_| Error::Capacity)?;
        serde_json::to_writer(&mut count, call.proof.arguments()).map_err(|_| Error::Capacity)?;
        serde_json::to_writer(&mut count, &call.proof.caller()).map_err(|_| Error::Capacity)?;
        let metadata = std::mem::size_of::<Invocation>() + std::mem::size_of::<Scope>() + 64;
        count.bytes = count.bytes.checked_add(metadata).ok_or(Error::Capacity)?;
    }
    Ok(count.bytes)
}
struct Prepared {
    position: usize,
    proof: AdmittedCall,
    scope: Arc<Scope>,
    parallel: bool,
}
fn prepare(batch: &Batch<'_>) -> Result<(), Error> {
    if !batch.parent.belongs_to(batch.owner, batch.task_id)
        || !batch.handlers.belongs_to(batch.owner, batch.epoch)
    {
        return Err(Error::Binding);
    }
    let mut positions = BTreeSet::new();
    for call in &batch.calls {
        if call.proof.owner() != batch.owner
            || call.proof.epoch() != batch.epoch
            || !positions.insert(call.position)
        {
            return Err(Error::Binding);
        }
    }
    Ok(())
}
fn scopes(
    batch: &Batch<'_>,
    overlap: &Overlap,
    mut calls: Vec<Invocation>,
) -> Result<Vec<Prepared>, Error> {
    calls.sort_unstable_by_key(|call| call.position);
    calls
        .into_iter()
        .map(|call| {
            let parallel = match overlap {
                Overlap::Every => true,
                Overlap::Qualified(names) => {
                    call.proof.permission() == Permission::Read
                        && names.contains(call.proof.name())
                }
            };
            let scope = Scope::with_parent(
                batch.sessions,
                batch.owner,
                &format!("{}:agent:{}:{}", batch.task_id, batch.round, call.position),
                "",
                None,
                batch.parent.clone(),
                batch.task_id,
            )
            .map_err(|_| Error::Binding)?;
            Ok(Prepared {
                position: call.position,
                proof: call.proof,
                scope: Arc::new(scope),
                parallel,
            })
        })
        .collect()
}
/// Parent Stop and deadlines reach a call waiting for its predecessor within this interval.
const ORDER_STOP_POLL: std::time::Duration = std::time::Duration::from_millis(10);
struct Control {
    stopped: AtomicBool,
    scopes: Vec<Arc<Scope>>,
    /// Earlier dispatched positions each position must wait for.
    after: HashMap<usize, Vec<usize>>,
    finished: Mutex<BTreeSet<usize>>,
    progress: Condvar,
}
impl Control {
    fn new(prepared: &[Prepared], after: HashMap<usize, Vec<usize>>) -> Self {
        Self {
            stopped: AtomicBool::new(false),
            scopes: prepared.iter().map(|call| call.scope.clone()).collect(),
            after,
            finished: Mutex::new(BTreeSet::new()),
            progress: Condvar::new(),
        }
    }
    fn stop(&self, owner: u64) {
        if self.stopped.swap(true, Ordering::SeqCst) {
            return;
        }
        for scope in &self.scopes {
            let _ = scope.request_cancel(owner, "batch_stop");
        }
        // Taking the lock orders this wake after any waiter's predicate check.
        let _finished = self.finished.lock().unwrap_or_else(|e| e.into_inner());
        self.progress.notify_all();
    }
    fn finish(&self, position: usize) {
        self.finished
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(position);
        self.progress.notify_all();
    }
    /// Returns when every declared predecessor finished or the call must stop.
    fn await_predecessors(&self, position: usize, scope: &Scope) {
        let Some(before) = self.after.get(&position) else {
            return;
        };
        let stopped = || {
            self.stopped.load(Ordering::SeqCst)
                || scope.snapshot().map_or(true, |snapshot| {
                    snapshot.cancel_requested
                        || snapshot
                            .deadline_remaining_nanos
                            .is_some_and(|remaining| remaining <= 0)
                })
        };
        let mut finished = self.finished.lock().unwrap_or_else(|e| e.into_inner());
        while !stopped() && before.iter().any(|earlier| !finished.contains(earlier)) {
            finished = self
                .progress
                .wait_timeout(finished, ORDER_STOP_POLL)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }
}
/// Publishes a call's completion on every exit, unwinding included, so a
/// successor can never wait on a call that will not finish.
struct Finished<'a>(&'a Control, usize);
impl Drop for Finished<'_> {
    fn drop(&mut self) {
        self.0.finish(self.1);
    }
}
/// Keeps only predecessors that are dispatched in this batch; a declared
/// predecessor must be an earlier call of the same response.
fn order(calls: &[Invocation], after: &[Vec<usize>]) -> Result<HashMap<usize, Vec<usize>>, Error> {
    let dispatched: BTreeSet<usize> = calls.iter().map(|call| call.position).collect();
    let mut order = HashMap::new();
    for &position in &dispatched {
        let Some(before) = after.get(position) else {
            continue;
        };
        if before.iter().any(|earlier| *earlier >= position) {
            return Err(Error::Binding);
        }
        let before: Vec<usize> = before
            .iter()
            .copied()
            .filter(|earlier| dispatched.contains(earlier))
            .collect();
        if !before.is_empty() {
            order.insert(position, before);
        }
    }
    Ok(order)
}
fn invoke(call: Prepared, batch: &Batch<'_>, control: &Control) -> ResultItem {
    let _finished = Finished(control, call.position);
    control.await_predecessors(call.position, &call.scope);
    if control.stopped.load(Ordering::SeqCst) {
        let _ = call.scope.request_cancel(batch.owner, "batch_stop");
    }
    let outcome = batch.handlers.dispatch_scoped_in_round(
        call.proof,
        batch.authorization,
        &call.scope,
        Some(batch.round),
    );
    if outcome.as_ref().map_or(true, |outcome| {
        !outcome.receipt.invariants_satisfied
            || outcome.cancellation_requested
            || outcome.deadline_exceeded
            || outcome.parent_closing
    }) {
        control.stop(batch.owner);
    }
    ResultItem {
        position: call.position,
        outcome,
    }
}
impl Scheduler {
    pub fn with_capacity(
        allocator: &Arc<crate::capacity_allocation::Allocator>,
        demand: crate::capacity::Demand,
        parallel_names: BTreeSet<String>,
    ) -> Result<Self, crate::capacity::Error> {
        let lease = allocator.reserve(demand)?;
        let b = lease.budget();
        let mut scheduler = Self::new(
            Budget {
                workers: b.workers,
                inflight_calls: b.max_inflight_jobs,
                inflight_bytes: b.max_inflight_bytes,
            },
            parallel_names,
        )
        .map_err(|_| crate::capacity::Error::InvalidDemand)?;
        scheduler
            .state
            .get_mut()
            .map_err(|_| crate::capacity::Error::StateUnavailable)?
            .capacity_lease = Some(lease);
        Ok(scheduler)
    }
    pub fn new(budget: Budget, parallel_names: BTreeSet<String>) -> Result<Self, Error> {
        Self::build(budget, Overlap::Qualified(parallel_names))
    }
    /// Every call of a batch starts at once, writes included (owner decision
    /// 2026-10-28); `dispatch_in_order` keeps only the declared call order.
    pub fn concurrent(budget: Budget) -> Result<Self, Error> {
        Self::build(budget, Overlap::Every)
    }
    fn build(budget: Budget, overlap: Overlap) -> Result<Self, Error> {
        if budget.workers == 0 {
            return Err(Error::Configuration);
        }
        Ok(Self {
            overlap,
            state: Mutex::new(State {
                closed: false,
                calls: 0,
                bytes: 0,
                callers: HashMap::new(),
                worker_callers: HashMap::new(),
                capacity_lease: None,
            }),
            settled: Condvar::new(),
            resident: Arc::new(AtomicUsize::new(0)),
            worker_start: start_worker,
        })
    }
    pub fn dispatch(&self, batch: Batch<'_>) -> Result<Vec<ResultItem>, Error> {
        self.dispatch_in_order(batch, &[])
    }
    /// `after[position]` names earlier positions whose calls must finish before
    /// that call enters; predecessors absent from the batch are not awaited.
    pub fn dispatch_in_order(
        &self,
        mut batch: Batch<'_>,
        after: &[Vec<usize>],
    ) -> Result<Vec<ResultItem>, Error> {
        prepare(&batch)?;
        let after = order(&batch.calls, after)?;
        let bytes = weight(&batch.calls, batch.task_id)?;
        let count = batch.calls.len();
        let mut state = self.state.lock().map_err(|_| Error::StatePoisoned)?;
        if state.closed {
            return Err(Error::Closed);
        }
        let next_calls = state.calls.checked_add(count).ok_or(Error::Capacity)?;
        let next_bytes = state.bytes.checked_add(bytes).ok_or(Error::Capacity)?;
        if state
            .worker_callers
            .contains_key(&std::thread::current().id())
        {
            return Err(Error::ReentrantDispatch);
        }
        if count == 0 {
            return Ok(Vec::new());
        }
        let caller = std::thread::current().id();
        *state.callers.entry(caller).or_insert(0) += 1;
        state.calls = next_calls;
        state.bytes = next_bytes;
        drop(state);
        let admission = Admission {
            scheduler: self,
            calls: count,
            bytes,
            caller,
        };
        let calls = std::mem::take(&mut batch.calls);
        let prepared = scopes(&batch, &self.overlap, calls)?;
        let control = Control::new(&prepared, after);
        let results = Mutex::new(Vec::with_capacity(count));
        let publication = (Mutex::new(None::<bool>), Condvar::new());
        let dispatch = std::thread::scope(|scope| {
            let mut ordered = Vec::new();
            let mut handles = Vec::new();
            for call in prepared {
                if !call.parallel {
                    ordered.push(call);
                    continue;
                }
                let results = &results;
                let batch = &batch;
                let control = &control;
                let publication = &publication;
                let name = format!("tofu-tool-{}", handles.len());
                let worker = (self.worker_start)(
                    scope,
                    name,
                    Box::new(move || {
                        self.resident.fetch_add(1, Ordering::SeqCst);
                        struct Exit<'a>(&'a Scheduler);
                        impl Drop for Exit<'_> {
                            fn drop(&mut self) {
                                let mut state =
                                    self.0.state.lock().unwrap_or_else(|e| e.into_inner());
                                state.worker_callers.remove(&std::thread::current().id());
                                self.0.resident.fetch_sub(1, Ordering::SeqCst);
                                self.0.settled.notify_all();
                            }
                        }
                        let _exit = Exit(self);
                        let mut gate = publication.0.lock().unwrap_or_else(|e| e.into_inner());
                        while gate.is_none() {
                            gate = publication.1.wait(gate).unwrap_or_else(|e| e.into_inner());
                        }
                        if *gate != Some(true) {
                            return;
                        }
                        drop(gate);
                        self.state
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .worker_callers
                            .insert(std::thread::current().id(), 1);
                        let result = invoke(call, batch, control);
                        results.lock().unwrap().push(result);
                    }),
                );
                match worker {
                    Ok(handle) => handles.push(handle),
                    Err(_) => {
                        *publication.0.lock().unwrap_or_else(|e| e.into_inner()) = Some(false);
                        publication.1.notify_all();
                        for handle in handles {
                            let _ = handle.join();
                        }
                        return Err(Error::WorkerStart);
                    }
                }
            }
            *publication.0.lock().unwrap_or_else(|e| e.into_inner()) = Some(true);
            publication.1.notify_all();
            for call in ordered {
                let result = invoke(call, &batch, &control);
                results.lock().unwrap().push(result);
            }
            for handle in handles {
                if handle.join().is_err() {
                    return Err(Error::StatePoisoned);
                }
            }
            Ok(())
        });
        dispatch?;
        let mut results = results.into_inner().map_err(|_| Error::StatePoisoned)?;
        results.sort_by_key(|item| item.position);
        drop(admission);
        Ok(results)
    }
    pub fn snapshot(&self) -> Result<Snapshot, Error> {
        let state = self.state.lock().map_err(|_| Error::StatePoisoned)?;
        Ok(Snapshot {
            inflight_calls: state.calls,
            inflight_bytes: state.bytes,
            resident_workers: self.resident.load(Ordering::SeqCst),
            closed: state.closed,
        })
    }
    pub fn close(&self) -> Result<(), Error> {
        let mut state = self.state.lock().map_err(|_| Error::StatePoisoned)?;
        if state.callers.contains_key(&std::thread::current().id()) {
            return Err(Error::ReentrantClose);
        }
        if state
            .worker_callers
            .contains_key(&std::thread::current().id())
        {
            return Err(Error::ReentrantClose);
        }
        state.closed = true;
        while state.calls != 0 {
            state = self.settled.wait(state).map_err(|_| Error::StatePoisoned)?;
        }
        // Publish closed only after the observation lease is released to all closers.
        drop(state.capacity_lease.take());
        drop(state);
        Ok(())
    }
}
impl Drop for Scheduler {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
/// Zero-pool ordered fallback used when no parallel scheduler was composed.
pub fn ordered(mut batch: Batch<'_>) -> Result<Vec<ResultItem>, Error> {
    prepare(&batch)?;
    let calls = std::mem::take(&mut batch.calls);
    let prepared = scopes(&batch, &Overlap::Qualified(BTreeSet::new()), calls)?;
    let control = Control::new(&prepared, HashMap::new());
    Ok(prepared
        .into_iter()
        .map(|call| invoke(call, &batch, &control))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_contract::{Catalog, Handler, InvocationContext, Permission};
    use serde_json::{json, Value};
    struct Allow;
    impl Authorization for Allow {
        fn allow(&self, _: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
            true
        }
    }
    fn fail_second<'scope, 'env>(
        scope: &'scope ThreadScope<'scope, 'env>,
        name: String,
        run: WorkerJob<'scope>,
    ) -> io::Result<ScopedJoinHandle<'scope, ()>> {
        if name.ends_with("-0") {
            std::thread::Builder::new()
                .name(name)
                .spawn_scoped(scope, run)
        } else {
            Err(io::Error::from(io::ErrorKind::Other))
        }
    }
    #[test]
    fn partial_worker_start_failure_joins_started_threads_without_admission() {
        let sessions = Sessions::new();
        let parent = sessions
            .start("start-failure", "task", 7, "", None)
            .unwrap();
        struct Observe(Arc<AtomicUsize>);
        impl Handler for Observe {
            fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(Value::Null)
            }
        }
        let effects = Arc::new(AtomicUsize::new(0));
        let handlers = Registry::new(
            7,
            "epoch",
            vec![("read".into(), Arc::new(Observe(effects.clone())))],
        )
        .unwrap();
        let catalog=Catalog::compile(7,"epoch",&json!({"read":{"name":"read","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}})).unwrap();
        let mut scheduler = Scheduler::new(
            Budget {
                workers: 2,
                inflight_calls: 2,
                inflight_bytes: 65536,
            },
            BTreeSet::from(["read".into()]),
        )
        .unwrap();
        scheduler.worker_start = fail_second;
        let proof = catalog
            .admit(7, "read", json!({}), &Value::Null, &Allow)
            .unwrap();
        let result = scheduler.dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "start-failure",
            round: 0,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &handlers,
            authorization: &Allow,
            calls: vec![
                Invocation {
                    position: 0,
                    proof: catalog
                        .admit(7, "read", json!({}), &Value::Null, &Allow)
                        .unwrap(),
                },
                Invocation { position: 1, proof },
            ],
        });
        assert!(matches!(result, Err(Error::WorkerStart)));
        assert_eq!(effects.load(Ordering::SeqCst), 0);
        let snapshot = scheduler.snapshot().unwrap();
        assert_eq!(snapshot.resident_workers, 0);
        assert_eq!(snapshot.inflight_calls, 0);
        assert_eq!(snapshot.inflight_bytes, 0);
        assert_eq!(sessions.active_count().unwrap(), 1);
        parent
            .settle(crate::execution_contract::ExecutionPhase::Failed, "")
            .unwrap();
        scheduler.close().unwrap();
    }
}
