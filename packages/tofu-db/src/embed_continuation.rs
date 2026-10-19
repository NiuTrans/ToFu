//! Carrier-free task ownership and wake coalescing. The scheduler owns live
//! tasks and supplies an infallible ready-queue enqueue signal. Wakers retain
//! only Weak task targets. No phase/callback destructor runs under state lock.
use std::sync::{Arc, Mutex, Weak};
use std::task::{Wake, Waker};

struct State<S> {
    phase: Option<S>, running: bool, queued: bool, wake_pending: bool,
    cancelled: bool, complete: bool,
}
pub(super) struct Continuation<S: Send + 'static> {
    state: Mutex<State<S>>,
    enqueue: Box<dyn Fn(Arc<Continuation<S>>) -> bool + Send + Sync>,
}
pub(super) struct Running<S: Send + 'static> {
    task: Arc<Continuation<S>>, phase: Option<S>, finished: bool,
}
struct TaskWake<S: Send + 'static>(Weak<Continuation<S>>);
impl<S: Send + 'static> Wake for TaskWake<S> {
    fn wake(self: Arc<Self>) { self.wake_by_ref(); }
    fn wake_by_ref(self: &Arc<Self>) {
        if let Some(task)=self.0.upgrade() { task.schedule(); }
    }
}
impl<S: Send + 'static> Continuation<S> {
    fn lock_state(&self)->Result<std::sync::MutexGuard<'_,State<S>>,()> {
        match self.state.lock() {
            Ok(state)=>Ok(state),
            Err(error)=>{
                let mut state=error.into_inner();
                state.cancelled=true;state.queued=false;state.wake_pending=false;
                let phase=state.phase.take();drop(state);drop(phase);Err(())
            }
        }
    }

    pub(super) fn new(phase:S,enqueue:impl Fn(Arc<Self>)->bool+Send+Sync+'static)->Arc<Self> {
        Arc::new(Self { state:Mutex::new(State {phase:Some(phase),running:false,queued:false,
            wake_pending:false,cancelled:false,complete:false}),enqueue:Box::new(enqueue) })
    }
    pub(super) fn waker(self:&Arc<Self>)->Waker {
        Waker::from(Arc::new(TaskWake(Arc::downgrade(self))))
    }
    pub(super) fn schedule(self:&Arc<Self>) {
        let enqueue={
            let Ok(mut state)=self.lock_state() else {return;};
            if state.cancelled || state.complete {false}
            else if state.running {state.wake_pending=true;false}
            else if state.queued {false}
            else {state.queued=true;true}
        };
        if enqueue && !(self.enqueue)(Arc::clone(self)) {self.cancel();}
    }
    pub(super) fn claim(self:&Arc<Self>)->Option<Running<S>> {
        let phase={
            let Ok(mut state)=self.lock_state() else {return None;};
            if state.cancelled || state.complete {return None;}
            if state.running {state.wake_pending=true;return None;}
            let phase=state.phase.take()?;
            state.running=true;state.queued=false;state.wake_pending=false;
            phase
        };
        Some(Running {task:Arc::clone(self),phase:Some(phase),finished:false})
    }
    /// Shutdown first marks every task and collects detached phases, then
    /// drops them after the registry lock is released. This avoids recursive
    /// successor wakes while cancelling a long FIFO chain.
    pub(super) fn begin_cancel(&self)->Option<S> {
        let mut state=self.state.lock().unwrap_or_else(|error|error.into_inner());
        state.cancelled=true;state.queued=false;state.wake_pending=false;
        state.phase.take()
    }
    pub(super) fn cancel(&self) {drop(self.begin_cancel());}
    fn finish(self:&Arc<Self>,phase:Option<S>) {
        let mut incoming=phase;
        let enqueue={
            let mut state=match self.lock_state() {
                Ok(state)=>state,
                Err(())=>{
                    self.state.lock().unwrap_or_else(|error|error.into_inner()).running=false;
                    drop(incoming);return;
                }
            };
            state.running=false;
            if state.cancelled {false}
            else if incoming.is_none() {state.complete=true;state.queued=false;state.wake_pending=false;false}
            else {
                debug_assert!(state.phase.is_none());
                state.phase=incoming.take();
                let ready=state.wake_pending;state.wake_pending=false;
                state.queued=ready;ready
            }
        };
        drop(incoming);
        if enqueue && !(self.enqueue)(Arc::clone(self)) {self.cancel();}
    }
    #[cfg(test)]
    fn running(&self)->bool {self.state.lock().unwrap().running}
}
impl<S: Send + 'static> Running<S> {
    pub(super) fn take_phase(&mut self)->S {self.phase.take().expect("phase already consumed")}
    pub(super) fn park(mut self,phase:S) {self.task.finish(Some(phase));self.finished=true;}
    pub(super) fn complete(mut self) {self.task.finish(None);self.finished=true;}
}
impl<S: Send + 'static> Drop for Running<S> {
    fn drop(&mut self) {
        if !self.finished {self.task.cancel();self.task.finish(None);}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    #[test]
    fn wakes_coalesce_and_a_wake_during_execution_survives_parking() {
        let (send,receive)=mpsc::channel();
        let task=Continuation::new(1,move|task|send.send(task).is_ok());
        let wake=task.waker();
        for _ in 0..100 {wake.wake_by_ref();}
        let ready=receive.try_recv().unwrap();assert!(receive.try_recv().is_err());
        let mut running=ready.claim().unwrap();assert_eq!(running.take_phase(),1);
        assert!(task.claim().is_none());
        for _ in 0..100 {wake.wake_by_ref();}
        assert!(receive.try_recv().is_err());
        running.park(2);
        let mut running=receive.try_recv().unwrap().claim().unwrap();assert_eq!(running.take_phase(),2);
        running.complete();wake.wake_by_ref();assert!(receive.try_recv().is_err());assert!(!task.running());
    }
    #[test]
    fn cancelling_running_work_waits_for_carrier_release_and_discards_returned_phase() {
        let task=Continuation::new(Arc::new(7),|_|true);
        let mut running=task.claim().unwrap();let phase=running.take_phase();let weak=Arc::downgrade(&phase);
        task.cancel();assert!(task.running());assert!(task.claim().is_none());
        running.park(phase);assert!(!task.running());assert!(weak.upgrade().is_none());
    }
    #[test]
    fn dropped_execution_and_disconnected_queue_release_owned_phase() {
        let value=Arc::new(7);let weak=Arc::downgrade(&value);
        let task=Continuation::new(value,|_|true);drop(task.claim().unwrap());
        assert!(weak.upgrade().is_none());assert!(!task.running());
        let value=Arc::new(8);let weak=Arc::downgrade(&value);
        let task=Continuation::new(value,|_|false);task.schedule();assert!(weak.upgrade().is_none());
    }
    #[test]
    fn a_registered_waker_does_not_retain_the_task_or_its_phase() {
        let value=Arc::new(7);let weak=Arc::downgrade(&value);
        let task=Continuation::new(value,|_|true);let wake=task.waker();drop(task);
        assert!(weak.upgrade().is_none());wake.wake_by_ref();
    }
    #[test]
    fn racing_wake_and_park_always_deliver_one_resumption() {
        for _ in 0..128 {
            let (send,receive)=mpsc::channel();
            let task=Continuation::new(1,move|task|send.send(task).is_ok());
            let mut running=task.claim().unwrap();assert_eq!(running.take_phase(),1);
            let wake=task.waker();let barrier=Arc::new(std::sync::Barrier::new(2));let other=Arc::clone(&barrier);
            let waking=std::thread::spawn(move||{other.wait();wake.wake_by_ref();});
            barrier.wait();running.park(2);waking.join().unwrap();
            let mut running=receive.recv_timeout(std::time::Duration::from_secs(1)).unwrap().claim().unwrap();
            assert_eq!(running.take_phase(),2);running.complete();assert!(receive.try_recv().is_err());
        }
    }
    #[test]
    fn poisoned_state_cancels_instead_of_dispatching_again() {
        let value=Arc::new(7);let weak=Arc::downgrade(&value);
        let task=Continuation::new(value,|_|panic!("poisoned task cannot enqueue"));let other=Arc::clone(&task);
        assert!(std::thread::spawn(move||{let _state=other.state.lock().unwrap();panic!("injected state poison");}).join().is_err());
        task.schedule();assert!(task.claim().is_none());assert!(weak.upgrade().is_none());
    }

    #[test]
    fn a_thousand_fifo_waiters_resume_on_one_carrier_without_waiting_threads() {
        use crate::authority_admission::{AuthorityAdmission,Admission,AdmissionPoll,QueuedAdmission};
        enum Phase { Register, Waiting(QueuedAdmission) }
        let gate=Arc::new(AuthorityAdmission::default());
        let Admission::Entered(first)=gate.acquire(std::time::Instant::now()+std::time::Duration::from_secs(1),&mut ||true).unwrap() else {panic!()};
        let (send,receive)=mpsc::channel();let mut tasks=Vec::new();
        for _ in 0..1000 {
            let ready=send.clone();let task=Continuation::new(Phase::Register,move|task|ready.send(task).is_ok());
            let mut running=task.claim().unwrap();assert!(matches!(running.take_phase(),Phase::Register));
            let ticket=gate.enqueue(task.waker()).unwrap();
            let AdmissionPoll::Waiting(ticket)=ticket.try_enter().unwrap() else {panic!("cannot bypass head")};
            running.park(Phase::Waiting(ticket));tasks.push(task);
        }
        assert!(receive.try_recv().is_err());assert_eq!(gate.waiting_count(),1001);drop(first);
        for _ in 0..1000 {
            let mut running=receive.recv_timeout(std::time::Duration::from_secs(1)).unwrap().claim().unwrap();
            let Phase::Waiting(ticket)=running.take_phase() else {panic!()};
            let AdmissionPoll::Entered(permit)=ticket.try_enter().unwrap() else {panic!("wake must respect FIFO")};
            drop(permit);running.complete();
        }
        assert_eq!(gate.waiting_count(),0);assert!(receive.try_recv().is_err());drop(tasks);
    }
    #[test]
    fn shutdown_marks_all_waiters_before_dropping_their_fifo_positions() {
        use crate::authority_admission::{AuthorityAdmission,Admission,QueuedAdmission};
        let gate=Arc::new(AuthorityAdmission::default());
        let Admission::Entered(first)=gate.acquire(std::time::Instant::now()+std::time::Duration::from_secs(1),&mut ||true).unwrap() else {panic!()};
        let tasks:Vec<_>=(0..1000).map(|_| {
            let task=Continuation::<Option<QueuedAdmission>>::new(None,|_|panic!("shutdown must not enqueue"));
            let mut running=task.claim().unwrap();running.take_phase();
            let ticket=gate.enqueue(task.waker()).unwrap();running.park(Some(ticket));task
        }).collect();
        let phases:Vec<_>=tasks.iter().filter_map(|task|task.begin_cancel()).collect();
        drop(phases);drop(first);assert_eq!(gate.waiting_count(),0);
    }

}
