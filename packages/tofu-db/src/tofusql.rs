//! TofuSQL: a read-only SQL front over the catalog-declared entity namespaces.
//!
//! A caller supplies a single `SELECT` over one catalog table
//! (`generated_tofusql_catalog::TOFUSQL_TABLES`). Key-prefix equality
//! predicates push down to a B+Tree prefix scan of the physical namespace,
//! and a range predicate pushes down only on the trailing `restText` key part,
//! whose encoding order equals SQLite BINARY collation. P1 adds the
//! lane/update/time index namespaces and the task-event stream family as raw
//! tables: trailing `orderedBinary` key parts (opaque tag/text-prefix/integer
//! suffixes that must not decode as one logical value) push equality down by
//! hex through the `tofusql_key()` table function, never range, and their
//! bodies surface the raw stored entity value as JSON when it decodes. P2
//! adds constrained joins: at most two catalog tables through one INNER JOIN
//! whose ON clause equates only catalog-declared join-edge identity columns
//! (key parts or declared single-field body paths), with both sides scanning
//! under an equality-pinned leading key part so the statement-wide entity-read
//! ceiling bounds the combined work. The
//! statement runs inside a read transaction that is never committed, under a
//! fresh in-memory SQLite handle guarded by a read-only authorizer and a
//! progress-handler deadline. Document bodies hydrate through the bounded
//! `versioned_document` path against `maximumBodyHydrationBytes`; the response
//! serializes as UTF-8 JSON bounded by `maximumResponseBytes` and the shared
//! `MAX_TRANSACTION_IR_LITERAL_BYTES` slab ceiling.

use std::io;
use std::time::{Duration, Instant};

use rusqlite::functions::FunctionFlags;
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
use rusqlite::types::ValueRef;
use rusqlite::vtab::{
    eponymous_only_module, Context, IndexConstraintOp, IndexInfo, VTab, VTabConfig, VTabConnection,
    VTabCursor, Values,
};
use rusqlite::{Connection, Error as RusqliteError};
use serde_json::{Map as JsonMap, Value as JsonValue};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{MAX_ENTITY_RANGE_ROWS, MAX_TRANSACTION_IR_LITERAL_BYTES};
use crate::generated_tofusql_catalog::{
    tofusql_join_edges_between, tofusql_table, TofuSqlKeyEncoding, TofuSqlTable,
    TOFUSQL_CATALOG_VERSION, TOFUSQL_MAXIMUM_BODY_HYDRATION_BYTES,
    TOFUSQL_MAXIMUM_ENTITY_READS_PER_STATEMENT, TOFUSQL_MAXIMUM_QUERY_TEXT_BYTES,
    TOFUSQL_MAXIMUM_RESPONSE_BYTES, TOFUSQL_MAXIMUM_RESULT_ROWS,
    TOFUSQL_MAXIMUM_STATEMENT_MILLISECONDS, TOFUSQL_TABLES,
};
use crate::semantic::AdmittedRequest;
use crate::semantic_executor::{integrity_failure, SemanticExecutionError};

const TOFUSQL_OPERATION: &str = "tofusql.query";

/// Build the declared DDL for one catalog table so the vtab `xConnect` schema
/// is derived from the single catalog source of truth rather than duplicated.
fn table_schema(table: &TofuSqlTable) -> String {
    let mut ddl = String::from("CREATE TABLE x(");
    let mut primary = String::new();
    for (index, part) in table.key_parts.iter().enumerate() {
        if index > 0 {
            ddl.push_str(", ");
            primary.push_str(", ");
        }
        ddl.push_str(part.name);
        // orderedBinary parts decode to opaque bytes, so their column is BLOB
        // (hex equality is addressable through `tofusql_key()`); every other
        // part decodes to text.
        if part.encoding == TofuSqlKeyEncoding::OrderedBinary {
            ddl.push_str(" BLOB NOT NULL");
        } else {
            ddl.push_str(" TEXT NOT NULL");
        }
        primary.push_str(part.name);
    }
    ddl.push_str(", version INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL, body TEXT");
    // Declared body-derived identity columns follow the value columns as
    // nullable TEXT; they exist so P2 joins can equate documented identities.
    for column in table.identity_columns {
        ddl.push_str(", ");
        ddl.push_str(column.name);
        ddl.push_str(" TEXT");
    }
    ddl.push_str(", PRIMARY KEY (");
    ddl.push_str(&primary);
    ddl.push_str(")) WITHOUT ROWID");
    ddl
}

/// The per-statement scan scope, threaded through the vtab by pointer because
/// the `VTabCursor` callbacks are `'static` and cannot borrow it directly. The
/// pointer is installed immediately before stepping and cleared on every exit
/// path, so it never outlives the borrow it was taken from.
struct ScanScope<'scope> {
    database: &'scope AuthorityDatabase,
    transaction: &'scope mut AuthorityTransaction,
    tenant_id: u64,
    owner_id: u64,
    hydration_bytes: usize,
    entity_reads: usize,
    /// P2: the two catalog tables a validated join statement may scan. Every
    /// cursor filter in join mode must belong to one of them and must run
    /// with an equality-pinned leading key part.
    join_tables: Option<(&'static str, &'static str)>,
}

thread_local! {
    static SCAN_SCOPE: std::cell::Cell<*mut ScanScope<'static>> = const { std::cell::Cell::new(std::ptr::null_mut()) };
}

fn with_scope<R>(f: impl FnOnce(&mut ScanScope<'_>) -> R) -> R {
    let pointer = SCAN_SCOPE.with(|cell| cell.get());
    assert!(
        !pointer.is_null(),
        "tofusql vtab used outside a statement scope"
    );
    // The scope is installed on this thread for the duration of one statement
    // step and cleared before control returns; see `run_statement`.
    let scope = unsafe { &mut *pointer };
    f(scope)
}

/// Entry point wired into the semantic dispatch. Returns `Ok(None)` for any
/// other operation so the shared `execute_admitted_request_inner` fallthrough
/// can handle it.
pub(crate) fn execute_tofusql_query(
    database: &mut AuthorityDatabase,
    admitted: &AdmittedRequest<'_>,
) -> Result<Option<Vec<u8>>, SemanticExecutionError> {
    if admitted.operation.name != TOFUSQL_OPERATION {
        return Ok(None);
    }
    let payload = payload_object(&admitted.request.payload)?;
    let query = payload
        .get("query")
        .and_then(JsonValue::as_str)
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    if query.len() > TOFUSQL_MAXIMUM_QUERY_TEXT_BYTES {
        return Err(SemanticExecutionError::PayloadTooLarge);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_id = admitted.request.owner_id;
    let statement_budget_ms = admitted
        .remaining_millis
        .min(TOFUSQL_MAXIMUM_STATEMENT_MILLISECONDS as u64);

    let mut transaction = database.begin(tenant_id, owner_id).map_err(storage_error)?;
    let response = run_statement(
        database,
        &mut transaction,
        tenant_id,
        owner_id,
        query,
        statement_budget_ms,
    )?;
    // The transaction is dropped without commit; reads publish nothing.
    drop(transaction);
    Ok(Some(response))
}

/// Offline operator entry (`tofu-db sql`): runs one statement against an
/// already-open authority with the operator-asserted scope, reusing the same
/// authorizer, pushdown, bounds, and deadline path as the admitted dispatch.
/// The transaction is never committed.
pub fn run_operator_query(
    database: &mut AuthorityDatabase,
    tenant_id: u64,
    owner_id: u64,
    query: &str,
) -> Result<Vec<u8>, SemanticExecutionError> {
    if query.len() > TOFUSQL_MAXIMUM_QUERY_TEXT_BYTES {
        return Err(SemanticExecutionError::PayloadTooLarge);
    }
    let mut transaction = database.begin(tenant_id, owner_id).map_err(storage_error)?;
    let response = run_statement(
        database,
        &mut transaction,
        tenant_id,
        owner_id,
        query,
        TOFUSQL_MAXIMUM_STATEMENT_MILLISECONDS as u64,
    )?;
    drop(transaction);
    Ok(response)
}

fn payload_object(payload: &[u8]) -> Result<JsonMap<String, JsonValue>, SemanticExecutionError> {
    let value: JsonValue =
        serde_json::from_slice(payload).map_err(|_| SemanticExecutionError::InvalidPayload)?;
    value
        .as_object()
        .cloned()
        .ok_or(SemanticExecutionError::InvalidPayload)
}

fn storage_error(error: io::Error) -> SemanticExecutionError {
    match error.kind() {
        io::ErrorKind::InvalidInput => SemanticExecutionError::InvalidPayload,
        io::ErrorKind::NotFound => SemanticExecutionError::NotFound,
        io::ErrorKind::InvalidData => integrity_failure(error.to_string()),
        io::ErrorKind::PermissionDenied => SemanticExecutionError::AuthorityScopeMismatch,
        io::ErrorKind::WouldBlock | io::ErrorKind::AlreadyExists => {
            SemanticExecutionError::Conflict
        }
        io::ErrorKind::OutOfMemory => SemanticExecutionError::ResourceExhausted,
        io::ErrorKind::TimedOut => SemanticExecutionError::DeadlineElapsed,
        _ => SemanticExecutionError::StorageUnavailable,
    }
}

/// Map a SQLite-layer error onto the semantic surface. Authorizer denials and
/// malformed SQL are caller faults; the progress-handler deadline and the
/// statement ceilings are resource faults; anything else is a storage fault.
fn sql_error(error: RusqliteError) -> SemanticExecutionError {
    match error {
        RusqliteError::SqliteFailure(code, _) => match code.code {
            rusqlite::ErrorCode::OperationInterrupted => SemanticExecutionError::DeadlineElapsed,
            rusqlite::ErrorCode::AuthorizationForStatementDenied => {
                SemanticExecutionError::InvalidPayload
            }
            rusqlite::ErrorCode::TooBig => SemanticExecutionError::ResourceExhausted,
            _ => SemanticExecutionError::InvalidPayload,
        },
        RusqliteError::ModuleError(_) => SemanticExecutionError::InvalidPayload,
        RusqliteError::SqlInputError { .. } => SemanticExecutionError::InvalidPayload,
        _ => SemanticExecutionError::StorageUnavailable,
    }
}

fn run_statement(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_id: u64,
    owner_id: u64,
    query: &str,
    budget_ms: u64,
) -> Result<Vec<u8>, SemanticExecutionError> {
    let connection = Connection::open_in_memory().map_err(sql_error)?;
    // Hex-decoder for equality predicates on trailing orderedBinary key parts:
    // `WHERE ordinal_be = tofusql_key('0000000000000000')` pushes the exact
    // byte suffix down to the B+Tree scan.
    connection
        .create_scalar_function(
            "tofusql_key",
            1,
            FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
            |context| {
                let hex: String = context.get(0)?;
                if !hex.len().is_multiple_of(2) || !hex.bytes().all(|byte| byte.is_ascii_hexdigit())
                {
                    return Err(RusqliteError::UserFunctionError(
                        io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "tofusql_key requires an even-length ASCII hex string",
                        )
                        .into(),
                    ));
                }
                Ok((0..hex.len())
                    .step_by(2)
                    .map(|offset| u8::from_str_radix(&hex[offset..offset + 2], 16).unwrap())
                    .collect::<Vec<u8>>())
            },
        )
        .map_err(sql_error)?;
    for table in TOFUSQL_TABLES {
        let table_index = TOFUSQL_TABLES
            .iter()
            .position(|candidate| candidate.name == table.name)
            .unwrap() as u32;
        connection
            .create_module(
                table.name,
                eponymous_only_module::<TofuSqlTab>(),
                Some(table_index),
            )
            .map_err(sql_error)?;
    }
    // Read-only authorizer: allow plain SELECT reads of the catalog tables and
    // their vtab declarations; deny writes, DDL, pragmas, attaches, and
    // recursion outright. P0 addresses exactly one catalog table per statement
    // and P2 at most two, so the first two distinct catalog-table Reads pin
    // the statement's tables and a Read of any third is denied. The statement
    // analyzer independently validates the join shape and the post-prepare
    // cross-check below requires both mechanisms to agree.
    let statement_tables: std::sync::Arc<std::sync::Mutex<Vec<String>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let authorizer_tables = std::sync::Arc::clone(&statement_tables);
    connection.authorizer(Some(move |context: AuthContext<'_>| {
        match context.action {
            AuthAction::Select
            | AuthAction::Function { .. }
            | AuthAction::CreateVtable { .. }
            | AuthAction::Recursive => Authorization::Allow,
            AuthAction::Read { table_name, .. } => {
                // sqlite_master and other internal reads carry names that are
                // not catalog tables; only catalog tables participate in the
                // table-count rule.
                if !TOFUSQL_TABLES.iter().any(|table| table.name == table_name) {
                    return Authorization::Allow;
                }
                // Fail closed on a poisoned pin set: the authorizer is the
                // read-only enforcement boundary, never a panic site.
                let mut pinned = match authorizer_tables.lock() {
                    Ok(pinned) => pinned,
                    Err(_) => return Authorization::Deny,
                };
                if pinned.iter().any(|name| name == table_name) {
                    return Authorization::Allow;
                }
                if pinned.len() < 2 {
                    pinned.push(table_name.to_owned());
                    Authorization::Allow
                } else {
                    Authorization::Deny
                }
            }
            _ => Authorization::Deny,
        }
    }));
    let deadline = Instant::now() + Duration::from_millis(budget_ms.max(1));
    connection.progress_handler(256, Some(move || Instant::now() >= deadline));

    let shape = analyze_statement(query)?;
    let mut statement = connection.prepare(query).map_err(sql_error)?;
    if !statement.readonly() {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    if statement.column_count() == 0 {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    // The analyzer and the authorizer must agree on the statement's table set:
    // a single-table shape may not reach a second catalog table through a
    // subquery, and a join shape must read exactly its two validated tables.
    let join_tables = {
        // Fail closed on a poisoned pin set, matching the authorizer.
        let recorded = statement_tables
            .lock()
            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
        match &shape {
            StatementShape::SingleTable => {
                if recorded.len() > 1 {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                None
            }
            StatementShape::Join { table_a, table_b } => {
                if recorded.len() != 2
                    || !recorded.iter().any(|name| name == table_a.name)
                    || !recorded.iter().any(|name| name == table_b.name)
                {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                Some((table_a.name, table_b.name))
            }
        }
    };
    let column_names: Vec<String> = statement
        .column_names()
        .iter()
        .map(|name| (*name).to_owned())
        .collect();
    let column_count = statement.column_count();

    let mut scope = ScanScope {
        database,
        transaction,
        tenant_id,
        owner_id,
        hydration_bytes: 0,
        entity_reads: 0,
        join_tables,
    };
    // Install the scope as a raw pointer widened to `'static` purely to cross
    // the thread-local cell; it is cleared on every path below before the
    // real borrow ends, so the widened lifetime never escapes.
    let scope_pointer = &mut scope as *mut ScanScope<'_>;
    let scope_pointer: *mut ScanScope<'static> = unsafe {
        std::mem::transmute::<*mut ScanScope<'_>, *mut ScanScope<'static>>(scope_pointer)
    };
    SCAN_SCOPE.with(|cell| cell.set(scope_pointer));
    let mut cleared = ScopeGuard;
    let result = (|| -> Result<Vec<JsonValue>, SemanticExecutionError> {
        let mut rows = statement.query([]).map_err(sql_error)?;
        let mut output_rows = Vec::new();
        while let Some(row) = rows.next().map_err(sql_error)? {
            if output_rows.len() >= TOFUSQL_MAXIMUM_RESULT_ROWS {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            let mut object = JsonMap::with_capacity(column_count);
            for (index, name) in column_names.iter().enumerate() {
                let value = sql_value_to_json(row.get_ref_unwrap(index))?;
                object.insert(name.clone(), value);
            }
            output_rows.push(JsonValue::Object(object));
        }
        Ok(output_rows)
    })();
    cleared.clear();
    let rows = result?;

    let payload = serde_json::json!({
        "rows": rows,
        "rowCount": rows.len(),
        "catalogVersion": TOFUSQL_CATALOG_VERSION,
        "statementCeilings": {
            "maximumQueryTextBytes": TOFUSQL_MAXIMUM_QUERY_TEXT_BYTES,
            "maximumEntityReadsPerStatement": TOFUSQL_MAXIMUM_ENTITY_READS_PER_STATEMENT,
            "maximumResultRows": TOFUSQL_MAXIMUM_RESULT_ROWS,
            "maximumResponseBytes": TOFUSQL_MAXIMUM_RESPONSE_BYTES,
            "maximumBodyHydrationBytes": TOFUSQL_MAXIMUM_BODY_HYDRATION_BYTES,
            "maximumStatementMilliseconds": TOFUSQL_MAXIMUM_STATEMENT_MILLISECONDS,
        },
    });
    let body = serde_json::to_vec(&payload)
        .map_err(|_| integrity_failure("failed to serialize the TofuSQL catalog response"))?;
    if body.len() > TOFUSQL_MAXIMUM_RESPONSE_BYTES.min(MAX_TRANSACTION_IR_LITERAL_BYTES) {
        return Err(SemanticExecutionError::ResourceExhausted);
    }
    Ok(body)
}

/// Clears the thread-local scope pointer even on the early-return paths so a
/// widened `'static` pointer never dangles past its real borrow.
struct ScopeGuard;

impl ScopeGuard {
    fn clear(&mut self) {
        SCAN_SCOPE.with(|cell| cell.set(std::ptr::null_mut()));
    }
}

impl Drop for ScopeGuard {
    fn drop(&mut self) {
        self.clear();
    }
}

fn sql_value_to_json(value: ValueRef<'_>) -> Result<JsonValue, SemanticExecutionError> {
    Ok(match value {
        ValueRef::Null => JsonValue::Null,
        ValueRef::Integer(integer) => JsonValue::from(integer),
        ValueRef::Real(real) => JsonValue::from(
            serde_json::Number::from_f64(real).ok_or(SemanticExecutionError::InvalidPayload)?,
        ),
        ValueRef::Text(text) => JsonValue::String(
            String::from_utf8(text.to_vec()).map_err(|_| SemanticExecutionError::InvalidPayload)?,
        ),
        // orderedBinary key-part columns surface as lowercase hex so the JSON
        // response can carry opaque bytes losslessly.
        ValueRef::Blob(bytes) => {
            JsonValue::String(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
        }
    })
}

// Column ordinals: key parts occupy `0..key_parts.len()`, then three value
// columns in catalog order.

fn version_column(table: &TofuSqlTable) -> usize {
    table.key_parts.len()
}

fn updated_at_column(table: &TofuSqlTable) -> usize {
    table.key_parts.len() + 1
}

fn body_column(table: &TofuSqlTable) -> usize {
    table.key_parts.len() + 2
}

/// The chosen scan for one statement: a leading run of equality-pushed key
/// parts plus, when it ends exactly on the trailing `restText` part, an
/// optional inclusive lower/upper range on that part.
#[derive(Clone, Copy, Default)]
struct ScanPlan {
    prefix_eq_len: usize,
    range: bool,
    lower: Option<usize>,
    lower_inclusive: bool,
    upper: Option<usize>,
    upper_inclusive: bool,
    descending: bool,
}

#[repr(C)]
struct TofuSqlTab {
    base: rusqlite::ffi::sqlite3_vtab,
    table: &'static TofuSqlTable,
}

unsafe impl<'vtab> VTab<'vtab> for TofuSqlTab {
    type Aux = u32;
    type Cursor = TofuSqlCursor<'vtab>;

    fn connect(
        db: &mut VTabConnection,
        aux: Option<&u32>,
        _args: &[&[u8]],
    ) -> Result<(String, TofuSqlTab), RusqliteError> {
        let index = *aux.ok_or_else(|| module_error("catalog table index missing"))? as usize;
        let table = TOFUSQL_TABLES
            .get(index)
            .ok_or_else(|| module_error("catalog table index out of range"))?;
        db.config(VTabConfig::Innocuous)?;
        Ok((
            table_schema(table),
            TofuSqlTab {
                base: rusqlite::ffi::sqlite3_vtab::default(),
                table,
            },
        ))
    }

    fn best_index(&self, info: &mut IndexInfo) -> Result<(), RusqliteError> {
        let table = self.table;
        let key_parts = table.key_parts.len();
        let mut equality: Vec<Option<usize>> = vec![None; key_parts];
        let mut lower: Option<(usize, bool)> = None;
        let mut upper: Option<(usize, bool)> = None;
        for (position, constraint) in info.constraints().enumerate() {
            let column = constraint.column();
            if column < 0 || (column as usize) >= key_parts || !constraint.is_usable() {
                continue;
            }
            match constraint.operator() {
                IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_EQ => {
                    equality[column as usize] = Some(position);
                }
                IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_GT => {
                    lower = Some((position, false));
                }
                IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_GE => {
                    lower = Some((position, true));
                }
                IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_LT => {
                    upper = Some((position, false));
                }
                IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_LE => {
                    upper = Some((position, true));
                }
                _ => {}
            }
        }
        // Longest leading run of equality constraints.
        let mut plan = ScanPlan::default();
        let mut argv: Vec<usize> = Vec::new();
        while plan.prefix_eq_len < key_parts {
            match equality[plan.prefix_eq_len] {
                Some(position) => {
                    argv.push(position);
                    plan.prefix_eq_len += 1;
                }
                None => break,
            }
        }
        // Range pushdown only on the trailing restText part reached by the
        // run. A usable range predicate on any other key part (notably an
        // orderedBinary part, whose decoded blob order must not be treated as
        // meaningful) is rejected instead of silently degrading to a
        // post-filtered scan.
        if lower.is_some() || upper.is_some() {
            for constraint in info.constraints() {
                let column = constraint.column();
                if column < 0 || (column as usize) >= key_parts || !constraint.is_usable() {
                    continue;
                }
                if matches!(
                    constraint.operator(),
                    IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_GT
                        | IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_GE
                        | IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_LT
                        | IndexConstraintOp::SQLITE_INDEX_CONSTRAINT_LE
                ) && table.key_parts[column as usize].encoding
                    == TofuSqlKeyEncoding::OrderedBinary
                {
                    return Err(module_error(
                        "range predicates are not supported on orderedBinary key parts",
                    ));
                }
            }
        }
        let rest_index = table
            .key_parts
            .iter()
            .rposition(|part| part.encoding == TofuSqlKeyEncoding::RestText);
        if let (Some(_rest), true) = (
            rest_index,
            plan.prefix_eq_len == rest_index.unwrap_or(usize::MAX),
        ) {
            if lower.is_some() || upper.is_some() {
                plan.range = true;
                if let Some((position, inclusive)) = lower {
                    argv.push(position);
                    plan.lower = Some(argv.len() - 1);
                    plan.lower_inclusive = inclusive;
                }
                if let Some((position, inclusive)) = upper {
                    argv.push(position);
                    plan.upper = Some(argv.len() - 1);
                    plan.upper_inclusive = inclusive;
                }
            }
        }
        // Consume a single ORDER BY on the trailing restText part when the
        // prefix fully pins all preceding parts, since prefix-scan order is the
        // declared key order.
        let mut order_by_columns = Vec::new();
        for order_by in info.order_bys() {
            order_by_columns.push((order_by.column(), order_by.is_order_by_desc()));
        }
        if order_by_columns.len() > 1 {
            return Err(module_error(
                "ORDER BY over multiple columns is not supported",
            ));
        }
        if let Some((order_column, order_descending)) = order_by_columns.first() {
            let rest =
                rest_index.ok_or_else(|| module_error("ORDER BY requires a restText key part"))?;
            if *order_column as usize != rest || plan.prefix_eq_len < rest {
                return Err(module_error(
                    "ORDER BY is only supported on the restText key part with its prefix pinned",
                ));
            }
            plan.descending = *order_descending;
            info.set_order_by_consumed(true);
        }
        let mut idx_num = (plan.prefix_eq_len as i32) & 0x0F;
        if plan.range {
            idx_num |= 1 << 4;
        }
        if plan.lower.is_some() {
            idx_num |= 1 << 5;
        }
        if plan.upper.is_some() {
            idx_num |= 1 << 6;
        }
        if plan.descending {
            idx_num |= 1 << 7;
        }
        if plan.lower_inclusive {
            idx_num |= 1 << 8;
        }
        if plan.upper_inclusive {
            idx_num |= 1 << 9;
        }
        for (argv_index, position) in argv.iter().enumerate() {
            let mut usage = info.constraint_usage(*position);
            usage.set_argv_index((argv_index + 1) as i32);
            usage.set_omit(true);
        }
        info.set_idx_num(idx_num);
        info.set_estimated_cost(if plan.prefix_eq_len > 0 {
            10.0
        } else {
            1_000.0
        });
        info.set_estimated_rows(100);
        Ok(())
    }

    fn open(&mut self) -> Result<TofuSqlCursor<'vtab>, RusqliteError> {
        Ok(TofuSqlCursor::new(self.table))
    }
}

fn module_error(message: &str) -> RusqliteError {
    RusqliteError::ModuleError(format!("tofusql: {message}"))
}

/// The validated table shape of one statement: P0/P1 single-table scans, or a
/// P2 constrained join over exactly two catalog tables whose ON clause
/// equates only declared join-edge identity columns.
enum StatementShape {
    SingleTable,
    Join {
        table_a: &'static TofuSqlTable,
        table_b: &'static TofuSqlTable,
    },
}

/// One lexical token of the scrubbed statement: a lowercased word (SQL is
/// case-insensitive and every catalog identifier is lowercase) or one
/// punctuation character.
#[derive(Clone, Debug, PartialEq, Eq)]
enum StatementToken {
    Word(String),
    Symbol(char),
}

fn word(token: Option<&StatementToken>) -> Option<&str> {
    match token {
        Some(StatementToken::Word(word)) => Some(word.as_str()),
        _ => None,
    }
}

/// Analyze the statement's table shape. Single-quoted string literals are
/// blanked first so a keyword inside a value never causes a false rejection;
/// double-quoted identifiers are left in place because they can carry a table
/// reference. Set/union keywords and comma-separated FROM lists are always
/// rejected. One bare or INNER JOIN between two distinct catalog tables is
/// accepted when every top-level ON term is an equality between qualified
/// columns that matches a declared catalog join edge in either orientation;
/// anything else fails closed.
fn analyze_statement(query: &str) -> Result<StatementShape, SemanticExecutionError> {
    let invalid = || SemanticExecutionError::InvalidPayload;
    let mut scrubbed = String::with_capacity(query.len());
    let mut in_string = false;
    let mut previous = '\u{0}';
    for character in query.chars() {
        match character {
            '\'' if !in_string => {
                in_string = true;
                scrubbed.push(' ');
            }
            '\'' if previous == '\\' => {
                scrubbed.push(' ');
            }
            '\'' => {
                in_string = false;
                scrubbed.push(' ');
            }
            _ if in_string => scrubbed.push(' '),
            character => scrubbed.push(character),
        }
        previous = character;
    }
    let mut tokens = Vec::new();
    let mut current = String::new();
    for character in scrubbed.chars() {
        if character.is_ascii_alphanumeric() || character == '_' {
            current.push(character);
        } else {
            if !current.is_empty() {
                tokens.push(StatementToken::Word(
                    std::mem::take(&mut current).to_lowercase(),
                ));
            }
            if !character.is_whitespace() {
                tokens.push(StatementToken::Symbol(character));
            }
        }
    }
    if !current.is_empty() {
        tokens.push(StatementToken::Word(current.to_lowercase()));
    }
    for token in &tokens {
        if let StatementToken::Word(word) = token {
            if matches!(
                word.as_str(),
                "union" | "intersect" | "except" | "cross" | "natural"
            ) {
                return Err(invalid());
            }
        }
    }
    let join_positions: Vec<usize> = tokens
        .iter()
        .enumerate()
        .filter(|(_, token)| word(Some(token)) == Some("join"))
        .map(|(index, _)| index)
        .collect();
    if join_positions.len() > 1 {
        return Err(invalid());
    }
    if join_positions.is_empty() {
        reject_comma_from(&tokens)?;
        return Ok(StatementShape::SingleTable);
    }
    // P2 join shape. Outer join qualifiers change correlation semantics and
    // stay rejected; subqueries and CTEs would multiply the scan set beyond
    // the two validated tables.
    let join_at = join_positions[0];
    if matches!(
        word(tokens.get(join_at.wrapping_sub(1))).filter(|_| join_at > 0),
        Some("left" | "right" | "full" | "outer")
    ) {
        return Err(invalid());
    }
    if tokens
        .iter()
        .filter(|token| word(Some(token)) == Some("select"))
        .count()
        > 1
    {
        return Err(invalid());
    }
    if tokens.iter().any(|token| word(Some(token)) == Some("with")) {
        return Err(invalid());
    }
    parse_join_shape(&tokens)
}

/// A comma between FROM and the first clause terminator is a second table
/// reference; single-table statements never allow one.
fn reject_comma_from(tokens: &[StatementToken]) -> Result<(), SemanticExecutionError> {
    let terminator = [
        "where", "group", "order", "limit", "having", "window", "values",
    ];
    let mut in_from = false;
    for token in tokens {
        match token {
            StatementToken::Symbol(',') if in_from => {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            StatementToken::Word(word) => {
                if in_from && terminator.contains(&word.as_str()) {
                    in_from = false;
                }
                if word == "from" {
                    in_from = true;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

/// Parse the restricted P2 join shape `FROM <table> [alias] [INNER] JOIN
/// <table> [alias] ON <term> [AND <term>...]` at the top level, where each
/// term is `alias.column = alias.column` matching a declared join edge.
fn parse_join_shape(tokens: &[StatementToken]) -> Result<StatementShape, SemanticExecutionError> {
    let invalid = || SemanticExecutionError::InvalidPayload;
    let from = tokens
        .iter()
        .position(|token| word(Some(token)) == Some("from"))
        .ok_or_else(invalid)?;
    let (table_a, alias_a, mut cursor) = parse_table_ref(tokens, from + 1)?;
    if word(tokens.get(cursor)) == Some("inner") {
        cursor += 1;
    }
    if word(tokens.get(cursor)) != Some("join") {
        return Err(invalid());
    }
    let (table_b, alias_b, mut cursor) = parse_table_ref(tokens, cursor + 1)?;
    if table_a.name == table_b.name {
        // A self-join scans the same table under two aliases; the authorizer
        // cannot distinguish that from one scan, so reject it here.
        return Err(invalid());
    }
    if word(tokens.get(cursor)) != Some("on") {
        return Err(invalid());
    }
    cursor += 1;
    let terminator = [
        "where", "group", "order", "limit", "having", "window", "values",
    ];
    let mut terms = 0usize;
    loop {
        match word(tokens.get(cursor)) {
            None if cursor >= tokens.len() => break,
            Some(word) if terminator.contains(&word) => break,
            _ => {}
        }
        let (left_alias, left_column) = parse_qualified_ref(tokens, cursor)?;
        cursor += 3;
        if tokens.get(cursor) != Some(&StatementToken::Symbol('=')) {
            return Err(invalid());
        }
        cursor += 1;
        let (right_alias, right_column) = parse_qualified_ref(tokens, cursor)?;
        cursor += 3;
        validate_edge_term(
            table_a,
            &alias_a,
            table_b,
            &alias_b,
            (&left_alias, &left_column),
            (&right_alias, &right_column),
        )?;
        terms += 1;
        match tokens.get(cursor) {
            None => break,
            Some(StatementToken::Word(word)) if word == "and" => cursor += 1,
            Some(StatementToken::Word(word)) if terminator.contains(&word.as_str()) => break,
            _ => return Err(invalid()),
        }
    }
    if terms == 0 {
        return Err(invalid());
    }
    Ok(StatementShape::Join { table_a, table_b })
}

/// Parse `<catalog-table> [[AS] alias]` starting at `at`, returning the table,
/// its effective alias (the table name when no alias is given), and the next
/// token index.
fn parse_table_ref(
    tokens: &[StatementToken],
    at: usize,
) -> Result<(&'static TofuSqlTable, String, usize), SemanticExecutionError> {
    let invalid = || SemanticExecutionError::InvalidPayload;
    let name = word(tokens.get(at)).ok_or_else(invalid)?;
    let table = tofusql_table(name).ok_or_else(invalid)?;
    let mut next = at + 1;
    let mut alias = name.to_owned();
    if word(tokens.get(next)) == Some("as") {
        alias = word(tokens.get(next + 1)).ok_or_else(invalid)?.to_owned();
        next += 2;
    } else if let Some(candidate) = word(tokens.get(next)) {
        let reserved = matches!(
            candidate,
            "join"
                | "inner"
                | "left"
                | "right"
                | "full"
                | "outer"
                | "cross"
                | "natural"
                | "on"
                | "where"
                | "group"
                | "order"
                | "limit"
                | "having"
                | "window"
                | "values"
                | "and"
        );
        if !reserved {
            alias = candidate.to_owned();
            next += 1;
        }
    }
    Ok((table, alias, next))
}

/// Parse `alias.column` starting at `at` (three tokens).
fn parse_qualified_ref(
    tokens: &[StatementToken],
    at: usize,
) -> Result<(String, String), SemanticExecutionError> {
    let invalid = || SemanticExecutionError::InvalidPayload;
    let alias = word(tokens.get(at)).ok_or_else(invalid)?;
    if tokens.get(at + 1) != Some(&StatementToken::Symbol('.')) {
        return Err(invalid());
    }
    let column = word(tokens.get(at + 2)).ok_or_else(invalid)?;
    Ok((alias.to_owned(), column.to_owned()))
}

/// One ON term must equate columns of the two different joined tables, each
/// column must be join-visible (a key part or declared identity column), and
/// the oriented pair must match a catalog-declared join edge.
fn validate_edge_term(
    table_a: &'static TofuSqlTable,
    alias_a: &str,
    table_b: &'static TofuSqlTable,
    alias_b: &str,
    left: (&str, &str),
    right: (&str, &str),
) -> Result<(), SemanticExecutionError> {
    let invalid = || SemanticExecutionError::InvalidPayload;
    let resolve = |alias: &str| -> Result<&'static TofuSqlTable, SemanticExecutionError> {
        if alias == alias_a {
            Ok(table_a)
        } else if alias == alias_b {
            Ok(table_b)
        } else {
            Err(invalid())
        }
    };
    let left_table = resolve(left.0)?;
    let right_table = resolve(right.0)?;
    if left_table.name == right_table.name {
        return Err(invalid());
    }
    if !left_table.has_join_column(left.1) || !right_table.has_join_column(right.1) {
        return Err(invalid());
    }
    let matched = tofusql_join_edges_between(table_a.name, table_b.name).any(|edge| {
        (edge.left_table == left_table.name
            && edge.left_column == left.1
            && edge.right_table == right_table.name
            && edge.right_column == right.1)
            || (edge.left_table == right_table.name
                && edge.left_column == right.1
                && edge.right_table == left_table.name
                && edge.right_column == left.1)
    });
    if !matched {
        return Err(invalid());
    }
    Ok(())
}

struct RowEntry {
    key_parts: Vec<rusqlite::types::Value>,
    version: i64,
    updated_at_ms: i64,
    body: Option<String>,
    /// Values of the table's declared body-derived identity columns, aligned
    /// with `table.identity_columns`; empty when the table declares none.
    identity: Vec<rusqlite::types::Value>,
}

#[repr(C)]
struct TofuSqlCursor<'vtab> {
    base: rusqlite::ffi::sqlite3_vtab_cursor,
    table: &'static TofuSqlTable,
    rows: Vec<RowEntry>,
    position: usize,
    owner: std::marker::PhantomData<&'vtab TofuSqlTab>,
}

impl<'vtab> TofuSqlCursor<'vtab> {
    fn new(table: &'static TofuSqlTable) -> Self {
        TofuSqlCursor {
            base: rusqlite::ffi::sqlite3_vtab_cursor::default(),
            table,
            rows: Vec::new(),
            position: 0,
            owner: std::marker::PhantomData,
        }
    }
}

unsafe impl VTabCursor for TofuSqlCursor<'_> {
    fn filter(
        &mut self,
        idx_num: i32,
        _idx_str: Option<&str>,
        args: &Values<'_>,
    ) -> Result<(), RusqliteError> {
        let plan = ScanPlan {
            prefix_eq_len: (idx_num & 0x0F) as usize,
            range: idx_num & (1 << 4) != 0,
            lower: if idx_num & (1 << 5) != 0 {
                Some(args_position(idx_num, true))
            } else {
                None
            },
            lower_inclusive: idx_num & (1 << 8) != 0,
            upper: if idx_num & (1 << 6) != 0 {
                Some(args_position(idx_num, false))
            } else {
                None
            },
            upper_inclusive: idx_num & (1 << 9) != 0,
            descending: idx_num & (1 << 7) != 0,
        };
        self.rows.clear();
        self.position = 0;
        with_scope(|scope| self.run_scan(scope, plan, args))
    }

    fn next(&mut self) -> Result<(), RusqliteError> {
        self.position += 1;
        Ok(())
    }

    fn eof(&self) -> bool {
        self.position >= self.rows.len()
    }

    fn column(&self, context: &mut Context, column: i32) -> Result<(), RusqliteError> {
        let Some(row) = self.rows.get(self.position) else {
            return Ok(());
        };
        let column = column as usize;
        let key_parts = self.table.key_parts.len();
        if column < key_parts {
            context.set_result(&row.key_parts[column])?;
        } else if column == version_column(self.table) {
            context.set_result(&row.version)?;
        } else if column == updated_at_column(self.table) {
            context.set_result(&row.updated_at_ms)?;
        } else if column == body_column(self.table) {
            context.set_result(&row.body)?;
        } else {
            // Declared identity columns follow the value columns in the DDL.
            let identity_index = column - body_column(self.table) - 1;
            if let Some(value) = row.identity.get(identity_index) {
                context.set_result(value)?;
            }
        }
        Ok(())
    }

    fn rowid(&self) -> Result<i64, RusqliteError> {
        Ok(self.position as i64 + 1)
    }
}

/// Recompute the position of a range-bound argument among the pushed argv.
/// Equality parts occupy argv `0..prefix_eq_len`; a lower bound (when present)
/// follows, then an upper bound.
fn args_position(idx_num: i32, lower: bool) -> usize {
    let prefix_eq_len = (idx_num & 0x0F) as usize;
    let has_lower = idx_num & (1 << 5) != 0;
    if lower {
        prefix_eq_len
    } else if has_lower {
        prefix_eq_len + 1
    } else {
        prefix_eq_len
    }
}

impl TofuSqlCursor<'_> {
    fn run_scan(
        &mut self,
        scope: &mut ScanScope<'_>,
        plan: ScanPlan,
        args: &Values<'_>,
    ) -> Result<(), RusqliteError> {
        let table = self.table;
        if let Some((table_a, table_b)) = scope.join_tables {
            // P2 join mode: every cursor of the statement must scan one of the
            // two analyzer-validated tables, and each scan must pin its
            // leading key part by equality so the join can never degrade to a
            // whole-namespace nested loop.
            if table.name != table_a && table.name != table_b {
                return Err(module_error("join scan escaped its declared tables"));
            }
            if plan.prefix_eq_len == 0 {
                return Err(module_error(
                    "join scans require an equality-pinned leading key part",
                ));
            }
        }
        // Encode the leading equality-pinned composite key bytes. The physical
        // namespace is supplied to `EntityKey::new` separately; the logical
        // composite key is the concatenation of the encoded key parts only
        // (matching `versioned_document::record_composite_key`).
        let mut composite = Vec::new();
        for (argument, part) in table.key_parts.iter().take(plan.prefix_eq_len).enumerate() {
            let value: rusqlite::types::Value = args.get(argument)?;
            encode_key_part(&mut composite, part.encoding, &value)?;
        }
        // Derive the half-open scan window [start, end). Equality prefix
        // scans to the prefix successor; a range on the restText part narrows
        // it. restText encoding order equals BINARY collation, so the bound
        // text appends directly to the equality prefix: `>= L` starts at
        // prefix+L (inclusive), `> L` starts one byte past it, `< U` ends at
        // prefix+U (exclusive), and `<= U` ends one byte past it. The trailing
        // 0x00 byte is the smallest possible extension, so it splits a bound
        // from its strict extensions without skipping any sibling key.
        let start = composite.clone();
        // The unbounded window end is the successor of the fully-encoded scoped
        // start key. Deriving it through `EntityKey::prefix_range` (rather than
        // the raw composite bytes) is what lets an empty composite — a
        // whole-namespace scan — resolve to the namespace's own successor
        // inside owner scope instead of having no lexical successor.
        let (start_key, base_end_key) =
            EntityKey::prefix_range(scope.tenant_id, scope.owner_id, table.namespace, &start)
                .map_err(|error| module_error(&error.to_string()))?;
        let mut start_key = start_key;
        let mut end_key = base_end_key;
        if plan.range {
            if let Some(lower_position) = plan.lower {
                let lower: String = args.get(lower_position)?;
                let mut lower_key = composite.clone();
                lower_key.extend_from_slice(lower.as_bytes());
                if !plan.lower_inclusive {
                    lower_key.push(0x00);
                }
                if lower_key > start {
                    start_key = EntityKey::new(
                        scope.tenant_id,
                        scope.owner_id,
                        table.namespace,
                        &lower_key,
                    )
                    .map_err(|error| module_error(&error.to_string()))?;
                }
            }
            if let Some(upper_position) = plan.upper {
                let upper: String = args.get(upper_position)?;
                let mut upper_key = composite.clone();
                upper_key.extend_from_slice(upper.as_bytes());
                if plan.upper_inclusive {
                    upper_key.push(0x00);
                }
                // Narrow the end only when the bound stays inside the current
                // window. For a non-empty composite the base end is the raw
                // prefix successor, comparable as namespace-relative bytes;
                // for an empty composite the base end is the namespace
                // successor (no key bytes), which any same-namespace upper key
                // strictly precedes, so it always narrows.
                let base_end_key_bytes = end_key.key_bytes();
                if composite.is_empty() || upper_key.as_slice() < base_end_key_bytes {
                    end_key = EntityKey::new(
                        scope.tenant_id,
                        scope.owner_id,
                        table.namespace,
                        &upper_key,
                    )
                    .map_err(|error| module_error(&error.to_string()))?;
                }
            }
        }

        // The entity-read ceiling is statement-wide: a join's outer scan, its
        // per-row inner scans, and any re-scans all draw from one counter, so
        // the combined work of the statement stays bounded by the catalog's
        // maximumEntityReadsPerStatement.
        let remaining =
            TOFUSQL_MAXIMUM_ENTITY_READS_PER_STATEMENT.saturating_sub(scope.entity_reads);
        let limit = remaining.min(MAX_ENTITY_RANGE_ROWS);
        let mut rows = scope
            .database
            .entity_scan(scope.transaction, &start_key, &end_key, limit)
            .map_err(|error| module_error(&error.to_string()))?;
        if rows.len() >= remaining {
            return Err(module_error(
                "entity reads reached maximumEntityReadsPerStatement",
            ));
        }
        scope.entity_reads = scope.entity_reads.saturating_add(rows.len());
        let mut entries = Vec::with_capacity(rows.len());
        for (key, value) in rows.drain(..) {
            let key_parts = decode_key_bytes(table, key.key_bytes())?;
            if key_parts.len() != table.key_parts.len() {
                continue;
            }
            let entry = hydrate_row(scope, table, key_parts, &value)?;
            entries.push(entry);
        }
        if plan.descending {
            entries.reverse();
        }
        self.rows = entries;
        Ok(())
    }
}

fn encode_key_part(
    buffer: &mut Vec<u8>,
    encoding: TofuSqlKeyEncoding,
    value: &rusqlite::types::Value,
) -> Result<(), RusqliteError> {
    match (encoding, value) {
        (TofuSqlKeyEncoding::RestText, rusqlite::types::Value::Text(text))
        | (TofuSqlKeyEncoding::Text, rusqlite::types::Value::Text(text)) => {
            if encoding == TofuSqlKeyEncoding::Text {
                let length = u16::try_from(text.len())
                    .map_err(|_| module_error("key part exceeds the u16 length bound"))?;
                buffer.extend_from_slice(&length.to_be_bytes());
            }
            buffer.extend_from_slice(text.as_bytes());
        }
        // orderedBinary parts are addressed as raw bytes, normally through
        // the tofusql_key() hex function; their exact byte suffix pushes down.
        (TofuSqlKeyEncoding::OrderedBinary, rusqlite::types::Value::Blob(bytes)) => {
            buffer.extend_from_slice(bytes)
        }
        _ => return Err(module_error("key part predicate has the wrong value type")),
    }
    Ok(())
}

fn decode_key_bytes(
    table: &TofuSqlTable,
    key_bytes: &[u8],
) -> Result<Vec<rusqlite::types::Value>, RusqliteError> {
    // `EntityKey::key_bytes` has already stripped the tenant, owner, and
    // physical namespace; what remains is the logical composite key, the
    // concatenation of the encoded key parts. Decode them in catalog order.
    let mut cursor = 0usize;
    let mut parts = Vec::with_capacity(table.key_parts.len());
    for part in table.key_parts {
        match part.encoding {
            TofuSqlKeyEncoding::RestText => {
                let text = std::str::from_utf8(&key_bytes[cursor..])
                    .map_err(|_| module_error("restText key part is not UTF-8"))?;
                parts.push(rusqlite::types::Value::Text(text.to_owned()));
                cursor = key_bytes.len();
            }
            TofuSqlKeyEncoding::Text => {
                if key_bytes.len() < cursor + 2 {
                    return Err(module_error("text key part length is truncated"));
                }
                let length =
                    u16::from_be_bytes([key_bytes[cursor], key_bytes[cursor + 1]]) as usize;
                cursor += 2;
                if key_bytes.len() < cursor + length {
                    return Err(module_error("text key part is truncated"));
                }
                let text = std::str::from_utf8(&key_bytes[cursor..cursor + length])
                    .map_err(|_| module_error("text key part is not UTF-8"))?;
                parts.push(rusqlite::types::Value::Text(text.to_owned()));
                cursor += length;
            }
            // orderedBinary keeps the remaining bytes opaque: the caller must
            // not re-interpret the tag/text-prefix/integer suffix as one
            // logical value.
            TofuSqlKeyEncoding::OrderedBinary => {
                parts.push(rusqlite::types::Value::Blob(key_bytes[cursor..].to_vec()));
                cursor = key_bytes.len();
            }
        }
    }
    Ok(parts)
}

/// Materialize one scanned row under the unified catalog value codec. A
/// versioned-document envelope hydrates its document JSON through the bounded
/// `versioned_document` path charged against `maximumBodyHydrationBytes` and
/// surfaces its envelope version/timestamp; any other stored value (the P1
/// index and stream rows) surfaces its raw bytes as JSON when they decode and
/// null otherwise, with envelope columns zeroed.
/// Extract the declared identity-column values from one decoded body JSON. A
/// missing or non-string field surfaces NULL; an undecodable body surfaces
/// NULL for every identity column.
fn identity_values(table: &TofuSqlTable, body: Option<&str>) -> Vec<rusqlite::types::Value> {
    if table.identity_columns.is_empty() {
        return Vec::new();
    }
    let parsed: Option<JsonValue> = body.and_then(|text| serde_json::from_str(text).ok());
    table
        .identity_columns
        .iter()
        .map(|column| {
            match parsed
                .as_ref()
                .and_then(|value| value.get(column.path))
                .and_then(JsonValue::as_str)
            {
                Some(text) => rusqlite::types::Value::Text(text.to_owned()),
                None => rusqlite::types::Value::Null,
            }
        })
        .collect()
}

fn hydrate_row(
    scope: &mut ScanScope<'_>,
    table: &TofuSqlTable,
    key_parts: Vec<rusqlite::types::Value>,
    raw_value: &[u8],
) -> Result<RowEntry, RusqliteError> {
    if let Some((version, updated_at_ms)) = envelope_header(raw_value) {
        // The scan already physically scoped the row to the table's namespace
        // and the caller's owner, so hydrate from the scanned envelope
        // directly and validate only the recorded logical key. Envelope rows
        // reached through an index namespace were recorded under the document
        // table's logical key, so the candidates cover both recording
        // conventions the engine uses: the plain trailing key part (document
        // tables, attempt records) and the `{conversation}\u{1f}{turn}`
        // composite (turn documents duplicated into index namespaces).
        let mut candidates: Vec<String> = Vec::with_capacity(2);
        if let Some(rusqlite::types::Value::Text(text)) = key_parts.last() {
            candidates.push(text.clone());
        }
        // Index-namespace envelopes were recorded under the document table's
        // logical key, which the index key parts do not carry verbatim (a
        // lane row records `{conversation}\u{1f}{turn}`; a covering updated
        // row records the plain conversation id). The envelope itself is the
        // authoritative source of its recorded key.
        if let Ok(recorded) =
            crate::versioned_document::stored_document_logical_key_for_catalog(raw_value)
        {
            if !candidates.contains(&recorded) {
                candidates.push(recorded);
            }
        }
        let mut body_bytes = None;
        for candidate in &candidates {
            match crate::versioned_document::materialize_stored_document_for_catalog(
                scope.database,
                scope.tenant_id,
                scope.owner_id,
                raw_value,
                candidate,
                MAX_TRANSACTION_IR_LITERAL_BYTES,
            ) {
                Ok(bytes) => {
                    body_bytes = Some(bytes);
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::InvalidData => continue,
                Err(error) => return Err(module_error(&error.to_string())),
            }
        }
        let body_bytes =
            body_bytes.ok_or_else(|| module_error("versioned document identity mismatch"))?;
        scope.hydration_bytes = scope.hydration_bytes.saturating_add(body_bytes.len());
        if scope.hydration_bytes > TOFUSQL_MAXIMUM_BODY_HYDRATION_BYTES {
            return Err(module_error(
                "body hydration exceeded maximumBodyHydrationBytes",
            ));
        }
        let body_json: JsonValue = serde_json::from_slice(&body_bytes)
            .map_err(|_| module_error("entity body is not valid JSON"))?;
        let body = body_json.to_string();
        let identity = identity_values(table, Some(&body));
        return Ok(RowEntry {
            key_parts,
            version,
            updated_at_ms,
            body: Some(body),
            identity,
        });
    }
    // Raw value row: surface the stored bytes as JSON when they decode.
    let body = serde_json::from_slice::<JsonValue>(raw_value)
        .ok()
        .map(|value| value.to_string());
    let identity = identity_values(table, body.as_deref());
    Ok(RowEntry {
        key_parts,
        version: 0,
        updated_at_ms: 0,
        body,
        identity,
    })
}

/// Decode the version and update timestamp from the stored document envelope
/// header (see `versioned_document::Document::encode`): magic (8) + format (4)
/// + version (8) + updated_at_ms (8).
///
/// Returns `None` when the stored value is not a versioned document envelope
/// (the P1 raw index/stream value case).
fn envelope_header(raw: &[u8]) -> Option<(i64, i64)> {
    if raw.len() < 28 || &raw[..8] != b"TDBDOC01" {
        return None;
    }
    let version = u64::from_le_bytes(raw[12..20].try_into().unwrap());
    let updated_at_ms = u64::from_le_bytes(raw[20..28].try_into().unwrap());
    Some((version as i64, updated_at_ms as i64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    use crate::semantic_executor::execute_admitted_request;

    fn request(
        operation: &str,
        owner_id: u64,
        payload: JsonValue,
        command_id: Option<&str>,
    ) -> Request {
        Request {
            correlation_id: [3; 16],
            deadline_unix_ms: 10_000_000_000,
            owner_id,
            tenant_id: Some(7),
            command_id: command_id.map(str::to_owned),
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(),
            payload: serde_json::to_vec(&payload).unwrap(),
        }
    }

    fn execute(
        database: &mut AuthorityDatabase,
        request: &Request,
        now_unix_ms: u64,
    ) -> Result<JsonValue, SemanticExecutionError> {
        let negotiated = NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        };
        let scope = AuthenticatedScope {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
        };
        let admitted = admit_request(request, negotiated, scope, now_unix_ms).unwrap();
        let response = execute_admitted_request(database, admitted, now_unix_ms)?;
        serde_json::from_slice(&response)
            .map_err(|_| integrity_failure("TofuSQL response is not valid JSON"))
    }

    fn put_record(
        database: &mut AuthorityDatabase,
        owner_id: u64,
        namespace: &str,
        key: &str,
        value: JsonValue,
        now_unix_ms: u64,
    ) {
        let request = request(
            "record.put",
            owner_id,
            serde_json::json!({"namespace": namespace, "key": key, "value": value}),
            Some(&format!("put-{owner_id}-{namespace}-{key}")),
        );
        execute(database, &request, now_unix_ms).unwrap();
    }

    fn query(
        database: &mut AuthorityDatabase,
        owner_id: u64,
        sql: &str,
    ) -> Result<JsonValue, SemanticExecutionError> {
        let request = request(
            "tofusql.query",
            owner_id,
            serde_json::json!({"query": sql}),
            None,
        );
        execute(database, &request, 1_000)
    }

    #[test]
    fn key_prefix_pushdown_scans_only_matching_records() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        put_record(
            &mut database,
            11,
            "alpha",
            "one",
            serde_json::json!({"n": 1}),
            100,
        );
        put_record(
            &mut database,
            11,
            "alpha",
            "two",
            serde_json::json!({"n": 2}),
            200,
        );
        put_record(
            &mut database,
            11,
            "beta",
            "one",
            serde_json::json!({"n": 3}),
            300,
        );
        // A second owner's records share the physical namespace and must be
        // filtered out by the owner-scoped scan boundary.
        put_record(
            &mut database,
            22,
            "alpha",
            "one",
            serde_json::json!({"n": 99}),
            400,
        );

        let result = query(
            &mut database,
            11,
            "SELECT record_namespace, record_key, version, body FROM storage_records WHERE record_namespace = 'alpha'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        let keys: Vec<&str> = rows
            .iter()
            .map(|row| row["record_key"].as_str().unwrap())
            .collect();
        assert_eq!(keys, ["one", "two"]);
        assert!(rows
            .iter()
            .all(|row| row["record_namespace"].as_str() == Some("alpha")));
        // The envelope header version surfaces through the catalog value codec.
        assert!(rows.iter().all(|row| row["version"].as_i64() == Some(1)));
        assert_eq!(
            rows[0]["body"],
            JsonValue::String(serde_json::json!({"n": 1}).to_string())
        );
    }

    #[test]
    fn whole_namespace_scan_has_no_prefix_successor() {
        // A full-table scan has an empty equality prefix, so the window end
        // must resolve to the namespace's own successor inside owner scope
        // rather than a non-existent composite-key successor.
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        put_record(
            &mut database,
            11,
            "alpha",
            "one",
            serde_json::json!({"n": 1}),
            100,
        );
        put_record(
            &mut database,
            11,
            "beta",
            "two",
            serde_json::json!({"n": 2}),
            200,
        );
        put_record(
            &mut database,
            22,
            "alpha",
            "one",
            serde_json::json!({"n": 99}),
            300,
        );

        let result = query(
            &mut database,
            11,
            "SELECT record_namespace, record_key FROM storage_records",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        // Only owner 11's two records, both namespaces. record_namespace uses
        // length-major Text encoding, so "beta" (u16 length 4) sorts before
        // "alpha" (length 5) in physical key order.
        assert_eq!(rows.len(), 2);
        let keys: Vec<(&str, &str)> = rows
            .iter()
            .map(|row| {
                (
                    row["record_namespace"].as_str().unwrap(),
                    row["record_key"].as_str().unwrap(),
                )
            })
            .collect();
        assert_eq!(keys, [("beta", "two"), ("alpha", "one")]);
    }

    #[test]
    fn resttext_range_pushdown_bounds_the_scan() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for suffix in ["a1", "a2", "b1", "b2"] {
            put_record(
                &mut database,
                11,
                "ns",
                suffix,
                serde_json::json!(suffix),
                100,
            );
        }

        let result = query(
            &mut database,
            11,
            "SELECT record_key FROM storage_records WHERE record_namespace = 'ns' AND record_key >= 'a' AND record_key < 'b'",
        )
        .unwrap();
        let keys: Vec<&str> = result["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["record_key"].as_str().unwrap())
            .collect();
        assert_eq!(keys, ["a1", "a2"]);
    }

    #[test]
    fn authorizer_rejects_non_readonly_statement() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        put_record(&mut database, 11, "ns", "k", serde_json::json!({}), 100);

        let error = query(
            &mut database,
            11,
            "DELETE FROM storage_records WHERE record_namespace = 'ns'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    #[test]
    fn p0_rejects_join_across_two_catalog_tables() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        put_record(&mut database, 11, "ns", "k", serde_json::json!({}), 100);

        let error = query(
            &mut database,
            11,
            "SELECT a.record_key FROM storage_records a JOIN storage_records b ON a.record_key = b.record_key",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    #[test]
    fn unknown_catalog_table_is_rejected() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let error = query(&mut database, 11, "SELECT 1 FROM nonexistent_table").unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    // P1: lane/update/time index namespaces and the task-event stream family.

    fn put_raw_entity(
        database: &mut AuthorityDatabase,
        tenant_id: u64,
        owner_id: u64,
        namespace: &str,
        logical_key: &[u8],
        value: &[u8],
    ) {
        let key = EntityKey::new(tenant_id, owner_id, namespace, logical_key).unwrap();
        let mut transaction = database.begin(tenant_id, owner_id).unwrap();
        database
            .entity_put(&mut transaction, key, value.to_vec())
            .unwrap();
        database.commit(transaction).unwrap();
    }

    fn text_key(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u16).to_be_bytes());
        output.extend_from_slice(value.as_bytes());
    }

    /// The settled turn ingest path: one conversation with `ordinals`
    /// main-lane settled turns produces the lane ordinal, lane compaction,
    /// activity, and updated index rows plus the turn documents.
    /// Turn identities and conversation ids claim tenant-globally, so each
    /// owner seeds its own `{conversation_id}-o{owner_id}` conversation.
    fn seeded_conversation(conversation_id: &str, owner_id: u64) -> String {
        format!("{conversation_id}-o{owner_id}")
    }

    fn seed_turns(
        database: &mut AuthorityDatabase,
        owner_id: u64,
        conversation_id: &str,
        ordinals: u64,
    ) {
        let conversation_id = seeded_conversation(conversation_id, owner_id);
        let mut committed = 1_000;
        for ordinal in 0..ordinals {
            committed += 10;
            let turn_id = format!("{conversation_id}-t{ordinal}");
            let command_id = format!("turn-cmd-{turn_id}");
            let request = request(
                "turn.append_settled",
                owner_id,
                serde_json::json!({
                    "conversation_id": conversation_id,
                    "user_id": owner_id,
                    "command_id": command_id,
                    "actor": "assistant",
                    "kind": "reply",
                    "status": "completed",
                    "projection": {"content": "answer"},
                    "settlement": {"outcome": "completed", "cause": "ingested", "resumeOptions": []},
                    "turn_id": turn_id,
                    "lane_id": "main",
                    "conversation_defaults": {"allowCreate": true, "title": conversation_id},
                    "now": committed
                }),
                Some(&command_id),
            );
            execute(database, &request, committed).unwrap();
        }
    }

    fn row_keys(result: &JsonValue, column: &str) -> Vec<String> {
        result["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row[column].as_str().unwrap().to_owned())
            .collect()
    }

    #[test]
    fn lane_ordinal_index_round_trips_and_is_owner_isolated() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);
        seed_turns(&mut database, 22, "conv-a", 1);

        let result = query(
            &mut database,
            11,
            "SELECT conversation_id, lane_id, ordinal_be, version, body \
             FROM turns_by_lane_ordinal WHERE conversation_id = 'conv-a-o11' AND lane_id = 'main'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        // Only owner 11's two rows; the ordinal suffix surfaces as hex of the
        // eight-byte big-endian ordinal and the document envelope decodes.
        assert_eq!(
            row_keys(&result, "ordinal_be"),
            ["0000000000000000", "0000000000000001"],
            "rows: {result}"
        );
        assert!(rows
            .iter()
            .all(|row| row["conversation_id"] == "conv-a-o11"));
        assert!(rows.iter().all(|row| row["lane_id"] == "main"));
        assert!(rows.iter().all(|row| row["version"].as_i64() == Some(1)));
        let body: JsonValue = serde_json::from_str(rows[0]["body"].as_str().unwrap()).unwrap();
        assert_eq!(body["turnId"], "conv-a-o11-t0");
        assert_eq!(body["ordinal"], 0);
    }

    #[test]
    fn lane_ordinal_index_hex_equality_pushes_down_one_row() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 3);

        let result = query(
            &mut database,
            11,
            "SELECT ordinal_be, body FROM turns_by_lane_ordinal \
             WHERE conversation_id = 'conv-a-o11' AND lane_id = 'main' \
             AND ordinal_be = tofusql_key('0000000000000002')",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 1);
        let body: JsonValue = serde_json::from_str(rows[0]["body"].as_str().unwrap()).unwrap();
        assert_eq!(body["turnId"], "conv-a-o11-t2");
    }

    #[test]
    fn lane_compaction_index_surfaces_raw_json_value() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);

        let result = query(
            &mut database,
            11,
            "SELECT ordinal_be, version, body FROM turns_by_lane_compaction \
             WHERE conversation_id = 'conv-a-o11' AND lane_id = 'main'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        // The compaction index stores a raw JSON metadata record (not a
        // document envelope), so the body decodes directly and the envelope
        // columns read zero.
        let body: JsonValue = serde_json::from_str(rows[0]["body"].as_str().unwrap()).unwrap();
        assert_eq!(body["turnId"], "conv-a-o11-t0");
        assert_eq!(body["status"], "completed");
        assert!(rows.iter().all(|row| row["version"].as_i64() == Some(0)));
    }

    #[test]
    fn activity_and_updated_indexes_whole_namespace_scan() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);
        seed_turns(&mut database, 11, "conv-b", 1);
        seed_turns(&mut database, 22, "conv-a", 1);

        // The activity index rows are main-lane only, so owner 11 sees all
        // three rows across both conversations in physical key order, with
        // binary (non-JSON, non-envelope) values decoding to a null body.
        let result = query(
            &mut database,
            11,
            "SELECT conversation_id, lane_id, ordinal_be, body FROM turn_activity_timestamps",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(
            row_keys(&result, "conversation_id"),
            ["conv-a-o11", "conv-a-o11", "conv-b-o11"]
        );
        assert!(rows.iter().all(|row| row["body"].is_null()));

        // The updated index carries the per-turn revision record.
        let result = query(
            &mut database,
            11,
            "SELECT conversation_id, updated_at_ms_be, body FROM turns_by_updated \
             WHERE conversation_id = 'conv-a-o11'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|row| row["body"].is_null()));
    }

    #[test]
    fn turn_tombstone_layout_round_trips() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // The tombstone namespace is written by turn deletion; seed its exact
        // physical layout directly: text conversation prefix, eight-byte
        // big-endian deletion timestamp, raw turn identity suffix.
        let mut logical = Vec::new();
        text_key(&mut logical, "conv-tomb");
        logical.extend_from_slice(&1_700_u64.to_be_bytes());
        logical.extend_from_slice(b"turn-gone");
        put_raw_entity(&mut database, 7, 11, "turn_tombstones", &logical, b"[]");
        // A second owner's row shares the physical namespace.
        put_raw_entity(&mut database, 7, 22, "turn_tombstones", &logical, b"[]");

        let result = query(
            &mut database,
            11,
            "SELECT conversation_id, deleted_at_ms_be, body FROM turn_tombstones \
             WHERE conversation_id = 'conv-tomb'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert!(rows[0]["deleted_at_ms_be"]
            .as_str()
            .unwrap()
            .starts_with("00000000000006a4"));
        // The raw JSON tombstone payload decodes as the body.
        assert_eq!(rows[0]["body"], "[]");
    }

    #[test]
    fn task_event_indexes_whole_namespace_scan_and_owner_isolation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let append =
            |database: &mut AuthorityDatabase, owner_id: u64, task: &str, sequence: u64| {
                let request = request(
                    "event.append",
                    owner_id,
                    serde_json::json!({
                        "task_id": task,
                        "sequence": sequence,
                        "event": {"type": "progress", "sequence": sequence},
                    }),
                    Some(&format!("event-{owner_id}-{task}-{sequence}")),
                );
                execute(database, &request, 1_000).unwrap();
            };
        append(&mut database, 11, "task-sql", 1);
        append(&mut database, 11, "task-sql", 2);
        append(&mut database, 22, "task-sql", 1);

        // The event family multiplexes tagged sub-indexes inside one physical
        // namespace; the whole-namespace scan sees owner 11's rows only and
        // the opaque trailing part carries the tag/prefix/sequence bytes.
        let result = query(
            &mut database,
            11,
            "SELECT index_key, version, body FROM task_event_indexes",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        // Two logical index rows (tag 0) + two physical-position rows (tag 6)
        // + one metadata row (tag 1) + two retention rows (tag 4) + one
        // retirement queue row (tag 7).
        assert!(!rows.is_empty());
        assert!(rows.iter().all(|row| row["index_key"].is_string()));
        assert!(rows.iter().all(|row| row["version"].as_i64() == Some(0)));
        // The tag-0 logical index rows carry the binary 68-byte index entry,
        // which is not JSON and decodes to a null body.
        let logical_rows: Vec<&JsonValue> = rows
            .iter()
            .filter(|row| row["index_key"].as_str().unwrap().starts_with("00"))
            .collect();
        assert_eq!(logical_rows.len(), 2);
        assert!(logical_rows.iter().all(|row| row["body"].is_null()));
        // The tag-1 metadata row carries raw non-JSON bytes as well.
        let metadata_rows: Vec<&JsonValue> = rows
            .iter()
            .filter(|row| row["index_key"].as_str().unwrap().starts_with("01"))
            .collect();
        assert_eq!(metadata_rows.len(), 1);

        // Hex equality on the opaque part addresses one exact index row.
        let mut logical_key = vec![0x00];
        text_key(&mut logical_key, "task-sql");
        logical_key.extend_from_slice(&2_u64.to_be_bytes());
        let hex: String = logical_key
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let result = query(
            &mut database,
            11,
            &format!(
                "SELECT index_key FROM task_event_indexes WHERE index_key = tofusql_key('{hex}')"
            ),
        )
        .unwrap();
        assert_eq!(result["rows"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn conversation_indexes_round_trip_with_raw_values() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // A real create maintains the updated index with the header envelope
        // as its covering value; a real delete moves the row into the trash
        // age index.
        let create = request(
            "conversation.create",
            11,
            serde_json::json!({"conv_id": "conv-idx", "user_id": 11, "title": "Idx", "settings": {}}),
            Some("conv-idx-create"),
        );
        execute(&mut database, &create, 1_000).unwrap();
        let create_other = request(
            "conversation.create",
            22,
            serde_json::json!({"conv_id": "conv-idx-o22", "user_id": 22, "title": "Idx", "settings": {}}),
            Some("conv-idx-o22-create"),
        );
        execute(&mut database, &create_other, 1_000).unwrap();

        let result = query(
            &mut database,
            11,
            "SELECT index_key, version, body FROM conversations_by_updated",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        // Only owner 11's row; the covering header envelope hydrates and
        // surfaces its envelope version.
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["version"].as_i64(), Some(1));
        assert!(rows[0]["index_key"].as_str().unwrap().len() > 16);
        let body: JsonValue = serde_json::from_str(rows[0]["body"].as_str().unwrap()).unwrap();
        assert_eq!(body["id"], "conv-idx");
        assert_eq!(body["user_id"], 11);

        let delete = request(
            "conversation.delete",
            11,
            serde_json::json!({"conv_id": "conv-idx", "user_id": 11}),
            Some("conv-idx-delete"),
        );
        execute(&mut database, &delete, 2_000).unwrap();

        let result = query(
            &mut database,
            11,
            "SELECT deleted_at_ms_be, body FROM conversation_trash_by_age",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 1);
        // The deletion timestamp leads the key as eight big-endian bytes; the
        // trash metadata value is opaque engine state whose body surfaces only
        // when it decodes as JSON.
        let deletion_hex = rows[0]["deleted_at_ms_be"].as_str().unwrap();
        assert!(deletion_hex.len() >= 16, "row: {:?}", rows[0]);

        // After deletion the updated index row is rekeyed out of the owner's
        // active set.
        let result = query(
            &mut database,
            11,
            "SELECT index_key FROM conversations_by_updated",
        )
        .unwrap();
        assert_eq!(result["rows"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn ordered_binary_range_predicates_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);

        // Range predicates on orderedBinary parts are rejected outright
        // instead of degrading to a post-filtered scan.
        let error = query(
            &mut database,
            11,
            "SELECT ordinal_be FROM turns_by_lane_ordinal \
             WHERE conversation_id = 'conv-a-o11' AND lane_id = 'main' \
             AND ordinal_be >= tofusql_key('0000000000000001')",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // ORDER BY on an orderedBinary part is likewise unsupported (only the
        // restText part participates in order pushdown).
        let error = query(
            &mut database,
            11,
            "SELECT ordinal_be FROM turns_by_lane_ordinal \
             WHERE conversation_id = 'conv-a-o11' AND lane_id = 'main' ORDER BY ordinal_be",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    #[test]
    fn index_namespace_entity_read_ceiling_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // Overflow the 1,000-entity scan ceiling with raw index rows.
        for ordinal in 0..1_001_u64 {
            let mut logical = Vec::new();
            text_key(&mut logical, "conv-bulk");
            text_key(&mut logical, "main");
            logical.extend_from_slice(&ordinal.to_be_bytes());
            put_raw_entity(
                &mut database,
                7,
                11,
                "turns_by_lane_compaction",
                &logical,
                b"{}",
            );
        }

        let error = query(
            &mut database,
            11,
            "SELECT ordinal_be FROM turns_by_lane_compaction",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    // P2: constrained joins over declared identity edges.

    #[test]
    fn p2_identity_column_surfaces_attempt_turn_correlation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);

        // Every assistant turn append also records a generation attempt whose
        // document carries the turn identity under the camelCase `turnId`
        // field; the declared identity column surfaces it as nullable TEXT.
        let result = query(
            &mut database,
            11,
            "SELECT turn_id FROM generation_attempts WHERE conversation_id = 'conv-a-o11'",
        )
        .unwrap();
        let mut turn_ids = row_keys(&result, "turn_id");
        turn_ids.sort();
        assert_eq!(turn_ids, ["conv-a-o11-t0", "conv-a-o11-t1"]);
    }

    #[test]
    fn p2_join_correlates_turns_and_attempts_on_declared_edges() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 2);
        seed_turns(&mut database, 22, "conv-a", 1);

        // Two declared edges (conversation_id key part + turn_id body
        // identity) in one ON clause; the WHERE constant propagates through
        // the conversation edge so both sides scan key-prefixed.
        let result = query(
            &mut database,
            11,
            "SELECT t.turn_id, a.attempt_id FROM conversation_turns t \
             INNER JOIN generation_attempts a \
             ON a.conversation_id = t.conversation_id AND a.turn_id = t.turn_id \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap();
        let rows = result["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2, "rows: {result}");
        let mut turn_ids: Vec<&str> = rows
            .iter()
            .map(|row| row["turn_id"].as_str().unwrap())
            .collect();
        turn_ids.sort();
        assert_eq!(turn_ids, ["conv-a-o11-t0", "conv-a-o11-t1"]);
        assert!(rows
            .iter()
            .all(|row| row["attempt_id"].as_str().is_some_and(|id| !id.is_empty())));

        // The same edges match in the reversed operand orientation.
        let result = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             JOIN generation_attempts a \
             ON t.conversation_id = a.conversation_id AND t.turn_id = a.turn_id \
             WHERE t.conversation_id = 'conv-a-o11' AND a.conversation_id = 'conv-a-o11'",
        )
        .unwrap();
        assert_eq!(result["rows"].as_array().unwrap().len(), 2);

        // The conversation_id edge alone is a declared edge; it produces the
        // within-conversation cross product of 2 turns and 2 attempts.
        let result = query(
            &mut database,
            11,
            "SELECT t.turn_id, a.attempt_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.conversation_id = t.conversation_id \
             WHERE t.conversation_id = 'conv-a-o11' AND a.conversation_id = 'conv-a-o11'",
        )
        .unwrap();
        assert_eq!(result["rows"].as_array().unwrap().len(), 4);
    }

    #[test]
    fn p2_join_rejects_undeclared_edges_and_shapes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 1);

        // attempt_id is a key part on both sides but no declared edge joins
        // them, so the equality is rejected even though both columns exist.
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.attempt_id = t.turn_id \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // A non-equality or unqualified ON term fails closed.
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.turn_id = t.turn_id AND 1 = 1 \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // A self-join is indistinguishable from one scan to the authorizer.
        let error = query(
            &mut database,
            11,
            "SELECT a.turn_id FROM conversation_turns a \
             JOIN conversation_turns b ON a.conversation_id = b.conversation_id \
             WHERE a.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // A third table exceeds the two-table statement budget.
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.turn_id = t.turn_id \
             JOIN turn_tombstones x ON x.conversation_id = t.conversation_id \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // Comma-separated FROM lists, set operations, and outer joins stay
        // rejected.
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t, generation_attempts a \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
        let error = query(
            &mut database,
            11,
            "SELECT turn_id FROM conversation_turns WHERE conversation_id = 'conv-a-o11' \
             UNION SELECT turn_id FROM generation_attempts WHERE conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             LEFT JOIN generation_attempts a ON a.turn_id = t.turn_id \
             WHERE t.conversation_id = 'conv-a-o11'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);

        // A subquery reaching a second catalog table is denied even in
        // single-table statement shape.
        let error = query(
            &mut database,
            11,
            "SELECT turn_id FROM conversation_turns \
             WHERE conversation_id = 'conv-a-o11' AND turn_id IN \
             (SELECT turn_id FROM generation_attempts WHERE conversation_id = 'conv-a-o11')",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    #[test]
    fn p2_join_requires_both_sides_key_prefixed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_turns(&mut database, 11, "conv-a", 1);
        seed_turns(&mut database, 11, "conv-b", 1);

        // The turn_id edge is declared, but with no conversation prefix the
        // outer scan would be a whole-namespace nested loop; execution fails
        // closed instead.
        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.turn_id = t.turn_id",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }

    #[test]
    fn p2_join_entity_read_ceiling_is_statement_wide() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // Seed raw rows on both sides of the conversation_id edge: 600 turns
        // plus 600 attempts individually stay under the 1,000-entity ceiling,
        // but one join statement reads both sets.
        for ordinal in 0..600_u64 {
            let mut turn_key = Vec::new();
            text_key(&mut turn_key, "conv-bulk");
            text_key(&mut turn_key, &format!("turn-{ordinal}"));
            put_raw_entity(&mut database, 7, 11, "conversation_turns", &turn_key, b"{}");
            let mut attempt_key = Vec::new();
            text_key(&mut attempt_key, "conv-bulk");
            text_key(&mut attempt_key, &format!("attempt-{ordinal}"));
            put_raw_entity(
                &mut database,
                7,
                11,
                "generation_attempts",
                &attempt_key,
                b"{}",
            );
        }

        let error = query(
            &mut database,
            11,
            "SELECT t.turn_id, a.attempt_id FROM conversation_turns t \
             JOIN generation_attempts a ON a.conversation_id = t.conversation_id \
             WHERE t.conversation_id = 'conv-bulk' AND a.conversation_id = 'conv-bulk'",
        )
        .unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload);
    }
}
