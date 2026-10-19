//! Embedded owned-command stage adapters. Request construction, authenticated
//! admission and response encoding stay in the original transport authorities.
use super::*;
use crate::server::{CompiledOwnedCommand, PreparedOwnedCommand, OwnedCommandOutcome};
use crate::authority_admission::{AuthorityAdmission, OwnedPermit};

pub(crate) enum EmbeddedCommandOutcome<T> { Ready(T), Finished(Vec<u8>), Fallback(Request) }
impl EmbeddedRuntime {
    pub(crate) fn command_admission(&self) -> Arc<AuthorityAdmission> {
        Arc::clone(&self.dispatch_bindings.authority_admission)
    }
    pub(crate) fn compile_owned_command(&self, operation:String,payload:Vec<u8>,
        command_id:Option<String>,deadline_unix_ms:u64) -> io::Result<EmbeddedCommandOutcome<CompiledOwnedCommand>> {
        let request=self.request_from_encoded(operation,payload,command_id,deadline_unix_ms)?;
        crate::fsync_stats::note_foreground_request();
        if crate::control_ops::is_control_operation(&request.operation) {
            return Ok(EmbeddedCommandOutcome::Fallback(request));
        }
        let streamed=request.payload.len()>MAX_EMBEDDED_PAYLOAD_BYTES;
        let admitted=match self.session.admit_owned(request,now_unix_ms(),streamed) {
            Ok(admitted)=>admitted,
            Err(response)=>return Ok(EmbeddedCommandOutcome::Finished(dispatched_response_to_encoded_json(&response.into())?)),
        };
        self.encode_command_outcome(CompiledOwnedCommand::compile(admitted,&self.session,now_unix_ms()))
    }
    pub(crate) fn prepare_owned_command(&self, command:CompiledOwnedCommand,permit:OwnedPermit,
        alive:&mut impl FnMut()->bool) -> io::Result<EmbeddedCommandOutcome<PreparedOwnedCommand>> {
        let stores=StorageV2Stores::borrowed(&self.database,self.search_projection.as_deref(),&self.dispatch_bindings);
        self.encode_command_outcome(command.prepare(&stores,&self.session,permit,&mut now_unix_ms,alive)?)
    }
    pub(crate) fn finish_owned_command(&self,command:PreparedOwnedCommand,permit:OwnedPermit,
        alive:&mut impl FnMut()->bool) -> io::Result<EmbeddedCommandOutcome<()>> {
        let stores=StorageV2Stores::borrowed(&self.database,self.search_projection.as_deref(),&self.dispatch_bindings);
        self.encode_command_outcome(command.finish(&stores,&self.session,permit,&mut now_unix_ms,alive)?)
    }
    fn encode_command_outcome<T>(&self,outcome:OwnedCommandOutcome<T>) -> io::Result<EmbeddedCommandOutcome<T>> {
        match outcome {
            OwnedCommandOutcome::Ready(value)=>Ok(EmbeddedCommandOutcome::Ready(value)),
            OwnedCommandOutcome::Fallback(admitted)=>Ok(EmbeddedCommandOutcome::Fallback(admitted.into_request())),
            OwnedCommandOutcome::Finished(RequestDispatch::Responded(response))=>Ok(EmbeddedCommandOutcome::Finished(dispatched_response_to_encoded_json(&response)?)),
            OwnedCommandOutcome::Finished(RequestDispatch::ConnectionClosed)=>Err(io_other("embedded dispatch reported a closed connection")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct NoopWake;
    impl std::task::Wake for NoopWake { fn wake(self:Arc<Self>){} }
    fn permit(runtime:&EmbeddedRuntime)->OwnedPermit {
        let ticket=runtime.command_admission().enqueue(std::task::Waker::from(Arc::new(NoopWake))).unwrap();
        let crate::authority_admission::AdmissionPoll::Entered(permit)=ticket.try_enter().unwrap() else {panic!("empty queue")};permit
    }
    #[test]
    fn runtime_owned_command_uses_same_commit_and_encoded_read_surface() {
        let directory=tempfile::tempdir().unwrap();
        drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let mut secret=*b"embedded-transport-test-secret-0001";
        let runtime=EmbeddedRuntime::open(directory.path(),11,Some(7),None,&mut secret).unwrap();
        let deadline=now_unix_ms()+30_000;
        let input=serde_json::to_vec(&json!({"namespace":"runtime-phase","key":"k","value":{"v":1}})).unwrap();
        let EmbeddedCommandOutcome::Ready(compiled)=runtime.compile_owned_command("record.put".to_owned(),input,Some("runtime-phase-put".to_owned()),deadline).unwrap() else {panic!("must compile")};
        let EmbeddedCommandOutcome::Ready(prepared)=runtime.prepare_owned_command(compiled,permit(&runtime),&mut ||true).unwrap() else {panic!("must prepare")};
        let EmbeddedCommandOutcome::Finished(encoded)=runtime.finish_owned_command(prepared,permit(&runtime),&mut ||true).unwrap() else {panic!("must finish")};
        let response:Value=serde_json::from_slice(&encoded).unwrap();assert_eq!(response["status"],0);
        let result=runtime.execute_json(&EmbeddedRequest {operation:"record.get".to_owned(),payload:&json!({"namespace":"runtime-phase","key":"k"}),command_id:None,deadline_unix_ms:deadline}).unwrap();
        assert_eq!(result["status"],0);assert_eq!(result["payload"]["value"],json!({"v":1}));
        assert_eq!(runtime.command_admission().waiting_count(),0);
        runtime.close().unwrap();
    }
    #[test]
    fn runtime_owned_admission_errors_match_existing_response_encoding() {
        let directory=tempfile::tempdir().unwrap();drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let mut secret=*b"embedded-transport-test-secret-0001";
        let runtime=EmbeddedRuntime::open(directory.path(),11,Some(7),None,&mut secret).unwrap();
        let deadline=now_unix_ms()+30_000;
        let EmbeddedCommandOutcome::Finished(phased)=runtime.compile_owned_command("unknown.operation".to_owned(),b"{}".to_vec(),None,deadline).unwrap() else {panic!("must refuse")};
        let ordinary=runtime.execute_encoded_json_with_alive("unknown.operation".to_owned(),b"{}".to_vec(),None,deadline,&mut ||true).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&phased).unwrap(),serde_json::from_slice::<Value>(&ordinary).unwrap());
        runtime.close().unwrap();
    }
    #[test]
    fn conflicting_prepared_command_returns_original_request_for_serialized_retry() {
        let directory=tempfile::tempdir().unwrap();drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let mut secret=*b"embedded-transport-test-secret-0001";
        let runtime=EmbeddedRuntime::open(directory.path(),11,Some(7),None,&mut secret).unwrap();
        let deadline=now_unix_ms()+30_000;let mut prepared=Vec::new();
        for (id,value) in [("first",1),("second",2)] {
            let input=serde_json::to_vec(&json!({"namespace":"phase-conflict","key":"k","value":value})).unwrap();
            let EmbeddedCommandOutcome::Ready(compiled)=runtime.compile_owned_command("record.put".to_owned(),input,Some(id.to_owned()),deadline).unwrap() else {panic!("must compile")};
            let EmbeddedCommandOutcome::Ready(command)=runtime.prepare_owned_command(compiled,permit(&runtime),&mut ||true).unwrap() else {panic!("must prepare")};prepared.push(command);
        }
        let second=prepared.pop().unwrap();let first=prepared.pop().unwrap();
        let EmbeddedCommandOutcome::Finished(first)=runtime.finish_owned_command(first,permit(&runtime),&mut ||true).unwrap() else {panic!("must commit first")};
        assert_eq!(serde_json::from_slice::<Value>(&first).unwrap()["status"],0);
        let EmbeddedCommandOutcome::Fallback(request)=runtime.finish_owned_command(second,permit(&runtime),&mut ||true).unwrap() else {panic!("stale same-key command must fall back")};
        assert_eq!(request.command_id.as_deref(),Some("second"));
        let replay=request.clone();
        let completed=runtime.execute_serialized_fallback(request,&mut ||true).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&completed).unwrap()["status"],0);
        let repeated=runtime.execute_serialized_fallback(replay,&mut ||true).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&completed).unwrap(),serde_json::from_slice::<Value>(&repeated).unwrap());
        let result=runtime.execute_json(&EmbeddedRequest{operation:"record.get".to_owned(),payload:&json!({"namespace":"phase-conflict","key":"k"}),command_id:None,deadline_unix_ms:deadline}).unwrap();
        assert_eq!(result["payload"]["value"],2);assert_eq!(runtime.command_admission().waiting_count(),0);runtime.close().unwrap();
    }

}
