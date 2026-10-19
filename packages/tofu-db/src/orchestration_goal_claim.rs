//! TofuDB transaction reads for the shared Rust Goal ancestry/witness owner.
//! Owner checks and document limits remain here; accepted lineage and chunk
//! verification live in tofu_runtime::checkpoint_goal_context. Cursor mutation
//! remains part of the caller's existing all-or-none authority transaction.
use super::*;
use tofu_runtime::checkpoint_goal_context::{self as shared, ReadAuthority};
pub(crate) struct Reads<'a> {
    pub(crate) database: &'a AuthorityDatabase,
    pub(crate) transaction: &'a mut AuthorityTransaction,
}
impl ReadAuthority for Reads<'_> {
    fn owner(&self) -> u64 {
        self.transaction.owner_user_id()
    }
    fn event(&mut self, run: &str, label: &str, sequence: u64) -> io::Result<Option<Value>> {
        let Some(document) = get_global_document::<EventDocument>(
            self.database,
            self.transaction,
            &event_key(self.transaction, run, sequence)?,
            EVENT_LOGICAL_NAMESPACE,
            &format!("{run}:{sequence}"),
            MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
        )?
        else {
            return Ok(None);
        };
        if document.owner != self.transaction.owner_user_id()
            || document.tenant_label != label
            || document.run_id != run
            || document.sequence != sequence
        {
            return Err(invalid_data("Goal claim event identity differs"));
        }
        Ok(Some(document.payload))
    }

    fn run(&mut self, run: &str, label: &str) -> io::Result<Option<(Value, Value)>> {
        let owner = self.transaction.owner_user_id();
        read_run(self.database, self.transaction, run, owner, label)?
            .map(|(core, state)| {
                Ok((
                    serde_json::to_value(core).map_err(|_| invalid_data("Goal run core JSON"))?,
                    serde_json::to_value(state).map_err(|_| invalid_data("Goal run state JSON"))?,
                ))
            })
            .transpose()
    }
    fn attempt(&mut self, id: &str) -> io::Result<Option<Value>> {
        crate::turn::attempt_get_with_config(self.database, self.transaction, id, true)?
            .map(|bytes| {
                serde_json::from_slice(&bytes).map_err(|_| invalid_data("Goal claim attempt JSON"))
            })
            .transpose()
    }
    fn turn(&mut self, conversation: &str, id: &str) -> io::Result<Option<Value>> {
        crate::turn::get(self.database, self.transaction, conversation, id)?
            .map(|bytes| {
                serde_json::from_slice(&bytes)
                    .map_err(|_| invalid_data("Goal claim continuation JSON"))
            })
            .transpose()
    }
}
pub(crate) fn event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    run: &str,
    label: &str,
    sequence: u64,
    remaining: &mut usize,
) -> io::Result<Option<Value>> {
    shared::event(
        &mut Reads {
            database,
            transaction: tx,
        },
        run,
        label,
        sequence,
        remaining,
    )
}
pub(crate) fn logical_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    run: &str,
    label: &str,
    sequence: u64,
    remaining: &mut usize,
) -> io::Result<Value> {
    shared::logical_event(
        &mut Reads {
            database,
            transaction: tx,
        },
        run,
        label,
        sequence,
        remaining,
    )
}
pub(crate) fn context(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    source_run: &str,
    target_attempt: &str,
    label: &str,
    cursor: u64,
    launch: u64,
    account: u64,
    remaining: &mut usize,
) -> io::Result<Value> {
    context_with_claim(
        database,
        tx,
        source_run,
        target_attempt,
        label,
        cursor,
        launch,
        account,
        None,
        remaining,
    )
}
pub(crate) fn context_with_claim(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    source_run: &str,
    target_attempt: &str,
    label: &str,
    cursor: u64,
    launch: u64,
    account: u64,
    claim: Option<&Value>,
    remaining: &mut usize,
) -> io::Result<Value> {
    shared::context(
        &mut Reads {
            database,
            transaction: tx,
        },
        source_run,
        target_attempt,
        label,
        cursor,
        launch,
        account,
        claim,
        remaining,
    )
}
pub(crate) fn append_claim(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    source: &str,
    label: &str,
    cursor: u64,
    claim: Value,
    now: u64,
) -> io::Result<()> {
    let (core, mut state) = read_run(database, tx, source, tx.owner_user_id(), label)?
        .ok_or_else(|| invalid_data("Goal claim source disappeared"))?;
    if state.next_event_sequence != cursor
        || !append_event(database, tx, &core, &mut state, cursor, claim, now)?
    {
        return Err(conflict("Goal claim source cursor changed"));
    }
    write_run_state(database, tx, &core, &state.status.clone(), &state, now)
}
