//! Embedded command phase execution and active-task teardown. Waiting phases
//! own request bytes/replies/leases in a registry, not an OS execution carrier.
//! Queue delivery and task wakes retain weak scheduler/runtime links only.
use super::*;
use std::collections::HashMap;
use std::sync::Weak;
use crate::authority_admission::{AdmissionPoll,QueuedAdmission};
use crate::embed::EmbeddedCommandOutcome;
use crate::server::{CompiledOwnedCommand,PreparedOwnedCommand};
use continuation::Continuation;

enum Phase {
    Compile {operation:String,payload:Vec<u8>,command_id:Option<String>,deadline:u64},
    Capture {command:CompiledOwnedCommand,ticket:QueuedAdmission,wait:crate::fsync_stats::ForegroundWait},
    Commit {command:PreparedOwnedCommand,ticket:QueuedAdmission,wait:crate::fsync_stats::ForegroundWait},
    Fallback(crate::protocol::Request),
}
pub(super) struct PhaseJob {
    phase:Phase,respond:std::sync::mpsc::Sender<JobOutcome>,_memory:OwnedInputLease,
    registry:Weak<Registry>,
}
#[derive(Default)]
struct RegistryState {closed:bool,tasks:HashMap<usize,Arc<Continuation<PhaseJob>>>}
#[derive(Default)]
pub(super) struct Registry {state:Mutex<RegistryState>}
impl Registry {
    #[cfg(test)]
    pub(super) fn active_count(&self)->usize {self.state.lock().unwrap().tasks.len()}

    pub(super) fn cancel_all(&self) {
        let tasks={let mut state=self.state.lock().unwrap_or_else(|error|error.into_inner());
            state.closed=true;std::mem::take(&mut state.tasks)};
        let phases:Vec<_>=tasks.values().filter_map(|task|task.begin_cancel()).collect();
        drop(phases);drop(tasks);
    }
    fn remove(&self,task:&Arc<Continuation<PhaseJob>>) {
        let removed=self.state.lock().unwrap_or_else(|error|error.into_inner()).tasks.remove(&(Arc::as_ptr(task) as usize));
        drop(removed);
    }
    pub(super) fn start(self:&Arc<Self>,runtime:&Arc<EmbeddedRuntime>,poisoned:&Arc<AtomicBool>,jobs:&JobReceiver<Job>,raw:RawJob) {
        let delivery=jobs.weak_delivery();let owner=Arc::downgrade(runtime);let poison=Arc::clone(poisoned);let registry=Arc::downgrade(self);
        let RawJob{operation,payload,command_id,deadline_unix_ms,respond,_memory,..}=raw;
        let backup=respond.clone();
        let task=Continuation::new(PhaseJob{phase:Phase::Compile{operation,payload,command_id,deadline:deadline_unix_ms},respond,_memory,registry:Arc::downgrade(self)},move|task| {
            let Some(runtime)=owner.upgrade() else {return false;};
            if poison.load(Ordering::Acquire){return false;}
            match delivery.try_handoff(Job::Resume(task)) {
                Ok(None)=>true,
                Ok(Some(job))=>{
                    let Some(receiver)=delivery.receiver() else {return false;};
                    let Some(registry)=registry.upgrade() else {return false;};
                    std::thread::Builder::new().name("tofu-db-embed-demand".to_owned())
                        .stack_size(runtime.resource_budget.connection_stack_bytes)
                        .spawn({let poison=Arc::clone(&poison);move||run_worker(runtime,receiver,poison,registry,Some(job))}).is_ok()
                },
                Err(_)=>false,
            }
        });
        let admitted={let mut state=self.state.lock().unwrap_or_else(|error|error.into_inner());
            if state.closed {Err(io::Error::new(io::ErrorKind::BrokenPipe,"embedded phase scheduler closed"))}
            else if let Err(error)=state.tasks.try_reserve(1){Err(io::Error::other(format!("embedded phase registration allocation failed: {error}")))}
            else {state.tasks.insert(Arc::as_ptr(&task) as usize,Arc::clone(&task));Ok(())}};
        if let Err(error)=admitted {task.cancel();let _=backup.send(JobOutcome::Responded(Err(error)));return;}
        run(runtime,poisoned,jobs,task);
    }
}
pub(super) fn eligible(job:&RawJob)->bool {
    #[cfg(feature = "native-runtime-checkpoints")]
    if job.checkpoint.is_some() { return false; }
    !crate::request_profile::enabled() && job.command_id.is_some()
        && crate::generated_storage_operations::storage_operation(&job.operation)
            .is_some_and(|operation|operation.kind.mutates_state())
}
pub(super) fn run(runtime:&Arc<EmbeddedRuntime>,poisoned:&Arc<AtomicBool>,jobs:&JobReceiver<Job>,task:Arc<Continuation<PhaseJob>>) {
    let Some(mut running)=task.claim() else {return;};
    let job=running.take_phase();let reply=job.respond.clone();let registry=job.registry.upgrade();
    let outcome=catch_unwind(AssertUnwindSafe(||drive(runtime,poisoned,&task,job)));
    match outcome {
        Ok(Ok(Some(job)))=>{let _=jobs.prepare_for_next_job();running.park(job);},
        Ok(Ok(None))=>{running.complete();if let Some(registry)=registry{registry.remove(&task);}},
        Ok(Err(error))=>{let _=reply.send(JobOutcome::timestamped(JobOutcome::Responded(Err(error))));running.complete();if let Some(registry)=registry{registry.remove(&task);}},
        Err(payload)=>{
            poisoned.store(true,Ordering::Release);runtime.disarm_drop();
            let _=reply.send(JobOutcome::timestamped(JobOutcome::Panicked(panic_message(payload))));
            if let Some(registry)=registry{registry.cancel_all();}drop(running);
        }
    }
}
fn drive(runtime:&Arc<EmbeddedRuntime>,poisoned:&Arc<AtomicBool>,task:&Arc<Continuation<PhaseJob>>,mut job:PhaseJob)->io::Result<Option<PhaseJob>> {
    if poisoned.load(Ordering::Acquire){return Err(io::Error::other("embedded handle is poisoned"));}
    let result=match job.phase {
        Phase::Compile{operation,payload,command_id,deadline}=>match runtime.compile_owned_command(operation,payload,command_id,deadline)? {
            EmbeddedCommandOutcome::Ready(command)=>{job.phase=Phase::Capture{command,ticket:runtime.command_admission().enqueue(task.waker())?,wait:crate::fsync_stats::ForegroundWait::start()};return Ok(Some(job));},
            EmbeddedCommandOutcome::Finished(response)=>Some(response),
            EmbeddedCommandOutcome::Fallback(request)=>{job.phase=Phase::Fallback(request);task.schedule();return Ok(Some(job));},
        },
        Phase::Capture{command,ticket,wait}=>match ticket.try_enter()? {
            AdmissionPoll::Waiting(ticket)=>{job.phase=Phase::Capture{command,ticket,wait};return Ok(Some(job));},
            AdmissionPoll::Entered(permit)=>match {drop(wait);runtime.prepare_owned_command(command,permit,&mut ||!poisoned.load(Ordering::Acquire))?} {
                EmbeddedCommandOutcome::Ready(command)=>{job.phase=Phase::Commit{command,ticket:runtime.command_admission().enqueue(task.waker())?,wait:crate::fsync_stats::ForegroundWait::start()};return Ok(Some(job));},
                EmbeddedCommandOutcome::Finished(response)=>Some(response),
                EmbeddedCommandOutcome::Fallback(request)=>{job.phase=Phase::Fallback(request);task.schedule();return Ok(Some(job));},
            },
        },
        Phase::Commit{command,ticket,wait}=>match ticket.try_enter()? {
            AdmissionPoll::Waiting(ticket)=>{job.phase=Phase::Commit{command,ticket,wait};return Ok(Some(job));},
            AdmissionPoll::Entered(permit)=>match {drop(wait);runtime.finish_owned_command(command,permit,&mut ||!poisoned.load(Ordering::Acquire))?} {
                EmbeddedCommandOutcome::Finished(response)=>Some(response),
                EmbeddedCommandOutcome::Fallback(request)=>{job.phase=Phase::Fallback(request);task.schedule();return Ok(Some(job));},
                EmbeddedCommandOutcome::Ready(())=>return Err(io::Error::other("embedded commit phase produced no response")),
            },
        },
        Phase::Fallback(request)=>Some(runtime.execute_serialized_fallback(request,&mut ||!poisoned.load(Ordering::Acquire))?),
    };
    let _=job.respond.send(JobOutcome::timestamped(JobOutcome::Responded(Ok(result.expect("completed response")))));Ok(None)
}
