//! Owned command stages for execution-carrier handoff. Admission stays in
//! semantic.rs; snapshot/OCC/WAL/error boundaries use the shared server path.
use super::*;
use crate::semantic::OwnedAdmittedRequest;
use crate::authority_admission::OwnedPermit;

pub(crate) enum OwnedCommandOutcome<T> {
    Ready(T),
    Finished(RequestDispatch),
    Fallback(OwnedAdmittedRequest),
}
pub(crate) struct CompiledOwnedCommand {
    admitted: OwnedAdmittedRequest,
    ir: crate::transaction_ir::TransactionIr,
}
pub(crate) struct PreparedOwnedCommand {
    admitted: OwnedAdmittedRequest,
    prepared: crate::transaction_ir::PreparedTransactionIr,
}
impl CompiledOwnedCommand {
    pub(crate) fn compile(admitted: OwnedAdmittedRequest, session: &StorageV2Session,
        now_unix_ms: u64) -> OwnedCommandOutcome<Self> {
        let _probe = crate::request_profile::Probe::new(crate::request_profile::PARALLEL_COMPILE);
        match crate::semantic_executor::compile_parallel_transaction(admitted.as_borrowed(), now_unix_ms) {
            Ok(Some(ir)) => OwnedCommandOutcome::Ready(Self { admitted, ir }),
            Ok(None) => OwnedCommandOutcome::Fallback(admitted),
            Err(error) => OwnedCommandOutcome::Finished(RequestDispatch::Responded(
                session.finish_admitted(admitted.as_borrowed(), Err(error), Vec::new()))),
        }
    }

    /// Called only after the scheduler has acquired this FIFO position.
    /// Snapshot capture releases authority/permit before CPU preparation.
    pub(crate) fn prepare(self, stores: &StorageV2Stores<'_>, session: &StorageV2Session,
        permit: OwnedPermit, now_unix_ms: &mut impl FnMut() -> u64,
        connection_alive: &mut impl FnMut() -> bool) -> io::Result<OwnedCommandOutcome<PreparedOwnedCommand>> {
        let admitted = self.admitted.as_borrowed();
        if !connection_alive() { return Ok(OwnedCommandOutcome::Finished(RequestDispatch::ConnectionClosed)); }
        let remaining = admitted.request.deadline_unix_ms.saturating_sub(now_unix_ms());
        if remaining == 0 { return Ok(OwnedCommandOutcome::Finished(RequestDispatch::Responded(
            session.finish_admitted(admitted, Err(SemanticExecutionError::DeadlineElapsed), Vec::new())))); }
        let authority = match acquire_authority_owned_until(stores.authority, remaining,
            connection_alive, &stores.authority_admission, permit)? {
            AuthorityAcquisition::Acquired(authority) => authority,
            AuthorityAcquisition::DeadlineElapsed => return Ok(OwnedCommandOutcome::Finished(RequestDispatch::Responded(
                session.finish_admitted(admitted, Err(SemanticExecutionError::DeadlineElapsed), Vec::new())))),
            AuthorityAcquisition::ConnectionClosed => return Ok(OwnedCommandOutcome::Finished(RequestDispatch::ConnectionClosed)),
        };
        if !connection_alive() { return Ok(OwnedCommandOutcome::Finished(RequestDispatch::ConnectionClosed)); }
        if now_unix_ms() >= admitted.request.deadline_unix_ms { return Ok(OwnedCommandOutcome::Finished(RequestDispatch::Responded(
            session.finish_admitted(admitted, Err(SemanticExecutionError::DeadlineElapsed), Vec::new())))); }
        let captured = {
            let _probe = crate::request_profile::Probe::new(crate::request_profile::REPLICA_BUILD);
            authority.build_preparation_replica()
        };
        drop(authority);
        let replica = match captured {
            Ok(replica) => replica,
            Err(error) if error.kind()==io::ErrorKind::WouldBlock || crate::engine::is_rotation_backpressure(&error) => {
                crate::fsync_stats::note_parallel_fallback(crate::fsync_stats::ParallelFallbackPhase::Snapshot,&error);
                return Ok(OwnedCommandOutcome::Fallback(self.admitted));
            }
            Err(error) => return Ok(OwnedCommandOutcome::Finished(RequestDispatch::Responded(
                session.finish_admitted(admitted,Err(crate::semantic_executor::map_storage_error(error)),Vec::new())))),
        };
        match prepare_parallel_ir(replica,session,admitted,&self.ir,now_unix_ms)? {
            ParallelPreparationOutcome::Prepared(prepared) => Ok(OwnedCommandOutcome::Ready(PreparedOwnedCommand { admitted:self.admitted,prepared })),
            ParallelPreparationOutcome::Fallback => Ok(OwnedCommandOutcome::Fallback(self.admitted)),
            ParallelPreparationOutcome::Finished(response) => Ok(OwnedCommandOutcome::Finished(response)),
        }
    }
}
impl PreparedOwnedCommand {
    pub(crate) fn finish(self, stores: &StorageV2Stores<'_>, session: &StorageV2Session,
        permit: OwnedPermit, now_unix_ms: &mut impl FnMut() -> u64,
        connection_alive: &mut impl FnMut() -> bool) -> io::Result<OwnedCommandOutcome<()>> {
        match finish_parallel_command_with_permit(stores,session,self.admitted.as_borrowed(),
            self.prepared,Some(permit),now_unix_ms,connection_alive)? {
            Some(response) => Ok(OwnedCommandOutcome::Finished(response)),
            None => Ok(OwnedCommandOutcome::Fallback(self.admitted)),
        }
    }
}
