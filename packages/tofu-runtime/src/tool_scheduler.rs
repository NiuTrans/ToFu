//! Composition-owned demand tool scheduling with atomic publication.
//! Only explicitly classified read calls overlap; ordered calls retain order.
//! Scoped workers start before publication and join before callback inputs expire.
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
pub struct Scheduler {
    parallel_names: BTreeSet<String>,
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
    parallel_names: &BTreeSet<String>,
    mut calls: Vec<Invocation>,
) -> Result<Vec<Prepared>, Error> {
    calls.sort_unstable_by_key(|call| call.position);
    calls
        .into_iter()
        .map(|call| {
            let parallel = call.proof.permission() == Permission::Read
                && parallel_names.contains(call.proof.name());
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
struct Control {
    stopped: AtomicBool,
    scopes: Vec<Arc<Scope>>,
}
impl Control {
    fn stop(&self, owner: u64) {
        if self.stopped.swap(true, Ordering::SeqCst) {
            return;
        }
        for scope in &self.scopes {
            let _ = scope.request_cancel(owner, "batch_stop");
        }
    }
}
fn invoke(call: Prepared, batch: &Batch<'_>, control: &Control) -> ResultItem {
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
        if budget.workers == 0 {
            return Err(Error::Configuration);
        }
        Ok(Self {
            parallel_names,
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
    pub fn dispatch(&self, mut batch: Batch<'_>) -> Result<Vec<ResultItem>, Error> {
        prepare(&batch)?;
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
        let prepared = scopes(&batch, &self.parallel_names, calls)?;
        let control = Control {
            stopped: AtomicBool::new(false),
            scopes: prepared.iter().map(|call| call.scope.clone()).collect(),
        };
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
    let names = BTreeSet::new();
    prepare(&batch)?;
    let calls = std::mem::take(&mut batch.calls);
    let prepared = scopes(&batch, &names, calls)?;
    let control = Control {
        stopped: AtomicBool::new(false),
        scopes: prepared.iter().map(|call| call.scope.clone()).collect(),
    };
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
