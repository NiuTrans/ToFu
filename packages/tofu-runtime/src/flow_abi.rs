//! Pure native Flow ABI. Registry handles withdraw and join accepted calls.
//! No pointer escapes, per-node process, worker, provider or persistent state.
use crate::flow_graph::{contract::*, Cursor, Graph, Input, Step};
use std::{
    collections::HashMap,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, OnceLock,
    },
};
type Joined<T> = crate::joined_calls::Joined<T, STATUS_UNKNOWN_HANDLE, STATUS_INTERNAL>;
type Lease<T> = crate::joined_calls::Lease<T, STATUS_UNKNOWN_HANDLE, STATUS_INTERNAL>;
struct GraphData {
    graph: Graph,
    cursors: Mutex<usize>,
}
struct CursorData {
    graph_id: u64,
    graph: Arc<Joined<GraphData>>,
    cursor: Mutex<Cursor>,
}
type GraphHandle = Arc<Joined<GraphData>>;
type CursorHandle = Arc<Joined<CursorData>>;
static GRAPHS: OnceLock<Mutex<HashMap<u64, GraphHandle>>> = OnceLock::new();
static CURSORS: OnceLock<Mutex<HashMap<u64, CursorHandle>>> = OnceLock::new();
static NEXT: AtomicU64 = AtomicU64::new(1);
fn graphs() -> &'static Mutex<HashMap<u64, GraphHandle>> {
    GRAPHS.get_or_init(|| Mutex::new(HashMap::new()))
}
fn cursors() -> &'static Mutex<HashMap<u64, CursorHandle>> {
    CURSORS.get_or_init(|| Mutex::new(HashMap::new()))
}
fn lock<T>(value: &Mutex<T>) -> Result<std::sync::MutexGuard<'_, T>, u32> {
    value.lock().map_err(|_| STATUS_INTERNAL)
}
fn id() -> Result<u64, u32> {
    NEXT.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
        .map_err(|_| STATUS_INTERNAL)
}
fn call(work: impl FnOnce() -> Result<(), u32>) -> u32 {
    catch_unwind(AssertUnwindSafe(work))
        .unwrap_or(Err(STATUS_INTERNAL))
        .err()
        .unwrap_or(STATUS_OK)
}
fn graph(id: u64) -> Result<Lease<GraphData>, u32> {
    lock(graphs())?
        .get(&id)
        .cloned()
        .ok_or(STATUS_UNKNOWN_HANDLE)?
        .enter()
}
fn cursor(graph_id: u64, id: u64) -> Result<Lease<CursorData>, u32> {
    let value = lock(cursors())?
        .get(&id)
        .cloned()
        .ok_or(STATUS_UNKNOWN_HANDLE)?;
    if value.value.graph_id != graph_id {
        return Err(STATUS_UNKNOWN_HANDLE);
    }
    value.enter()
}
fn index(value: u64, graph: &Graph) -> Result<Option<usize>, u32> {
    if value == NONE {
        return Ok(None);
    }
    let value = usize::try_from(value).map_err(|_| STATUS_INVALID_INPUT)?;
    if value >= graph.nodes.len() {
        return Err(STATUS_INVALID_INPUT);
    }
    Ok(Some(value))
}
fn required(value: u64, graph: &Graph) -> Result<usize, u32> {
    index(value, graph)?.ok_or(STATUS_INVALID_INPUT)
}
fn encoded(value: Option<usize>) -> u64 {
    value.map_or(NONE, |i| i as u64)
}
fn aligned<T>(pointer: *const T) -> bool {
    !pointer.is_null() && (pointer as usize) % std::mem::align_of::<T>() == 0
}

#[no_mangle]
pub extern "C" fn tofu_flow_abi_version() -> u32 {
    ABI_VERSION
}
/// Caller memory must remain readable/writable for the declared call lifetime.
/// # Safety
/// Input must be readable for length bytes; output must be aligned and writable
/// for one u64. Both buffers must remain valid and nonoverlapping until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_graph_open(
    data: *const u8,
    length: usize,
    output: *mut u64,
) -> u32 {
    call(|| {
        if data.is_null() || length > isize::MAX as usize || !aligned(output) {
            return Err(STATUS_INVALID_INPUT);
        }
        let input = Input::parse(
            serde_json::from_slice(unsafe { std::slice::from_raw_parts(data, length) })
                .map_err(|_| STATUS_INVALID_INPUT)?,
        )?;
        let value = Arc::new(Joined::new(GraphData {
            graph: Graph::new(input)?,
            cursors: Mutex::new(0),
        }));
        let id = id()?;
        lock(graphs())?.insert(id, value);
        unsafe { output.write(id) };
        Ok(())
    })
}
#[no_mangle]
pub extern "C" fn tofu_flow_graph_close(id: u64) -> u32 {
    call(|| {
        let value = {
            let mut registry = lock(graphs())?;
            let value = registry.get(&id).cloned().ok_or(STATUS_UNKNOWN_HANDLE)?;
            // Cursors are declared borrowers; the owner must close them first.
            let mut calls = lock(&value.calls)?;
            if *lock(&value.value.cursors)? != 0 {
                return Err(STATUS_BUSY);
            }
            calls.closed = true;
            drop(calls);
            registry.remove(&id);
            value
        };
        value.close()
    })
}
/// # Safety
/// Nonempty input/output arrays must be aligned and valid for length/capacity
/// u64s, respectively. written must be aligned and writable for one usize.
/// Borrowed buffers must remain valid and nonoverlapping until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_query(
    graph_id: u64,
    op: u32,
    a: u64,
    b: u64,
    c: u64,
    input: *const u64,
    length: usize,
    output: *mut u64,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        if !aligned(written)
            || capacity > isize::MAX as usize / 8
            || (capacity != 0 && !aligned(output))
            || length > isize::MAX as usize / 8
            || (length != 0 && !aligned(input))
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = graph(graph_id)?;
        let graph = &lease.0.value.graph;
        let values = match op {
            QUERY_SINGLE_NEXT => vec![encoded(graph.next(required(a, graph)?))],
            QUERY_FIND_START => vec![graph.start()? as u64],
            QUERY_LOOP_PARTS => {
                let (body, exit) = graph.loop_parts(required(a, graph)?);
                vec![encoded(body), encoded(exit)]
            }
            QUERY_LOOP_PLANNER => vec![encoded(
                graph.loop_planner(required(a, graph)?, index(b, graph)?),
            )],
            QUERY_COMMON_BARRIER => {
                let values = if length == 0 {
                    &[][..]
                } else {
                    unsafe { std::slice::from_raw_parts(input, length) }
                };
                let branches = values
                    .iter()
                    .map(|&i| required(i, graph))
                    .collect::<Result<Vec<_>, _>>()?;
                vec![encoded(graph.barrier(&branches))]
            }
            QUERY_PARALLEL_PLAN => {
                let node = required(a, graph)?;
                if graph.nodes[node].node_type != "control" || graph.nodes[node].kind != "parallel"
                {
                    return Err(STATUS_INVALID_INPUT);
                }
                let branches = &graph.forward[node];
                let barrier = graph.barrier(branches);
                let mut values = vec![
                    encoded(barrier),
                    encoded(barrier.and_then(|i| graph.next(i))),
                ];
                values.extend(branches.iter().map(|&i| i as u64));
                values
            }
            QUERY_REACHABLE => graph
                .reachable(required(a, graph)?, None)
                .into_iter()
                .enumerate()
                .filter_map(|(i, reached)| reached.then_some(i as u64))
                .collect(),
            QUERY_CAN_REACH => vec![u64::from(graph.can_reach(
                required(a, graph)?,
                required(b, graph)?,
                index(c, graph)?,
            ))],
            QUERY_DISTANCE => vec![graph.distance(required(a, graph)?, required(b, graph)?)],
            _ => return Err(STATUS_INVALID_INPUT),
        };
        if values.len() > capacity {
            return Err(STATUS_BUFFER_SMALL);
        }
        if !values.is_empty() {
            unsafe { std::ptr::copy_nonoverlapping(values.as_ptr(), output, values.len()) };
        }
        unsafe { written.write(values.len()) };
        Ok(())
    })
}
/// # Safety
/// output must be aligned, writable for one u64 and remain valid until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_cursor_open(
    graph_id: u64,
    start: u64,
    stop: u64,
    budget: u64,
    output: *mut u64,
) -> u32 {
    call(|| {
        if !aligned(output) {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = graph(graph_id)?;
        let state = Cursor::new(
            index(start, &lease.0.value.graph)?,
            index(stop, &lease.0.value.graph)?,
            budget,
        );
        let calls = lock(&lease.0.calls)?;
        if calls.closed {
            return Err(STATUS_UNKNOWN_HANDLE);
        }
        let mut count = lock(&lease.0.value.cursors)?;
        *count = count.checked_add(1).ok_or(STATUS_INTERNAL)?;
        let id = match id() {
            Ok(id) => id,
            Err(error) => {
                *count -= 1;
                return Err(error);
            }
        };
        lock(cursors())?.insert(
            id,
            Arc::new(Joined::new(CursorData {
                graph_id,
                graph: lease.0.clone(),
                cursor: Mutex::new(state),
            })),
        );
        drop(count);
        drop(calls);
        unsafe { output.write(id) };
        Ok(())
    })
}
/// # Safety
/// output must be aligned, writable for one Step and remain valid until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_cursor_next(
    graph_id: u64,
    cursor_id: u64,
    output: *mut Step,
) -> u32 {
    call(|| {
        if !aligned(output) {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = cursor(graph_id, cursor_id)?;
        let step = lock(&lease.0.value.cursor)?.step(&lease.0.value.graph.value.graph)?;
        unsafe { output.write(step) };
        Ok(())
    })
}
#[no_mangle]
pub extern "C" fn tofu_flow_cursor_advance(graph_id: u64, cursor_id: u64, target: u64) -> u32 {
    call(|| {
        let lease = cursor(graph_id, cursor_id)?;
        let graph = &lease.0.value.graph.value.graph;
        let target = index(target, graph)?;
        let result = lock(&lease.0.value.cursor)?.advance(graph, target);
        result
    })
}
/// Complete a returned effect and obtain its next action under one cursor lock.
/// Rejected targets/output pointers leave the pending effect unchanged.
/// A next-action budget failure still acknowledges the already returned effect.
/// # Safety
/// output must be aligned, writable for one Step and remain valid until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_cursor_transition(
    graph_id: u64,
    cursor_id: u64,
    target: u64,
    output: *mut Step,
) -> u32 {
    call(|| {
        if !aligned(output) {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = cursor(graph_id, cursor_id)?;
        let graph = &lease.0.value.graph.value.graph;
        let target = index(target, graph)?;
        let mut cursor = lock(&lease.0.value.cursor)?;
        cursor.advance(graph, target)?;
        let step = cursor.step(graph)?;
        unsafe { output.write(step) };
        Ok(())
    })
}
/// Classify borrowed UTF-8 text without retaining it or emitting audit effects.
/// On buffer_small, only the required byte count is published for a retry.
/// # Safety
/// Nonempty input/output buffers must be readable/writable for length/capacity
/// bytes. written must be aligned and writable for one usize. All buffers must
/// remain valid and nonoverlapping until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_classify_verdict(
    graph_id: u64,
    data: *const u8,
    length: usize,
    flags: u32,
    digit_limit: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        if (length != 0 && data.is_null())
            || length > isize::MAX as usize
            || capacity > isize::MAX as usize
            || (capacity != 0 && output.is_null())
            || !aligned(written)
            || flags & !VERDICT_FLAGS_MASK != 0
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let _lease = graph(graph_id)?;
        let input = if length == 0 {
            &[][..]
        } else {
            unsafe { std::slice::from_raw_parts(data, length) }
        };
        let text = std::str::from_utf8(input).map_err(|_| STATUS_INVALID_INPUT)?;
        let result = crate::verdict_policy::classify(
            text,
            flags & VERDICT_VIRTUAL_USER != 0,
            flags & VERDICT_LOOSE_FALLBACK != 0,
            flags & VERDICT_STRIP_FEEDBACK != 0,
            flags & VERDICT_REPLAN_ENABLED != 0,
            digit_limit,
        )
        .map_err(|_| STATUS_INVALID_INPUT)?;
        let raw = serde_json::to_vec(&result).map_err(|_| STATUS_INTERNAL)?;
        if raw.len() > capacity {
            unsafe { written.write(raw.len()) };
            return Err(STATUS_BUFFER_SMALL);
        }
        if !raw.is_empty() {
            unsafe { std::ptr::copy_nonoverlapping(raw.as_ptr(), output, raw.len()) };
        }
        unsafe { written.write(raw.len()) };
        Ok(())
    })
}
#[no_mangle]
pub extern "C" fn tofu_flow_cursor_close(graph_id: u64, cursor_id: u64) -> u32 {
    call(|| {
        let value = {
            let mut registry = lock(cursors())?;
            if registry
                .get(&cursor_id)
                .is_none_or(|value| value.value.graph_id != graph_id)
            {
                return Err(STATUS_UNKNOWN_HANDLE);
            }
            registry.remove(&cursor_id).ok_or(STATUS_UNKNOWN_HANDLE)?
        };
        value.close()?;
        lock(&value.value.cursor)?.closed = true;
        *lock(&value.value.graph.value.cursors)? -= 1;
        Ok(())
    })
}

struct LoopData {
    graph_id: u64,
    graph: Arc<Joined<GraphData>>,
    state: Mutex<crate::flow_loop::State>,
}
type LoopHandle = Arc<Joined<LoopData>>;
static LOOPS: OnceLock<Mutex<HashMap<u64, LoopHandle>>> = OnceLock::new();
fn loops() -> &'static Mutex<HashMap<u64, LoopHandle>> {
    LOOPS.get_or_init(|| Mutex::new(HashMap::new()))
}
fn loop_handle(graph_id: u64, id: u64) -> Result<Lease<LoopData>, u32> {
    let value = lock(loops())?
        .get(&id)
        .cloned()
        .ok_or(STATUS_UNKNOWN_HANDLE)?;
    if value.value.graph_id != graph_id {
        return Err(STATUS_UNKNOWN_HANDLE);
    }
    value.enter()
}
unsafe fn loop_input(
    data: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> Result<serde_json::Value, u32> {
    if (length != 0 && data.is_null())
        || length > isize::MAX as usize
        || capacity > isize::MAX as usize
        || (capacity != 0 && output.is_null())
        || !aligned(written)
    {
        return Err(STATUS_INVALID_INPUT);
    }
    let raw = if length == 0 {
        &[][..]
    } else {
        unsafe { std::slice::from_raw_parts(data, length) }
    };
    serde_json::from_slice(raw).map_err(|_| STATUS_INVALID_INPUT)
}
unsafe fn loop_output(
    raw: &[u8],
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> Result<(), u32> {
    if raw.len() > capacity {
        unsafe { written.write(raw.len()) };
        return Err(STATUS_BUFFER_SMALL);
    }
    if !raw.is_empty() {
        unsafe { std::ptr::copy_nonoverlapping(raw.as_ptr(), output, raw.len()) }
    }
    unsafe { written.write(raw.len()) };
    Ok(())
}
/// # Safety
/// For this call, `data` must be readable for `length` bytes when nonzero,
/// `output` writable for `capacity` bytes when nonzero, and `written` an
/// aligned writable usize. Buffers must remain valid and writable regions
/// must not overlap readable input or each other.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_loop_open(
    graph_id: u64,
    node: u64,
    data: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        let input = unsafe { loop_input(data, length, output, capacity, written) }?;
        let lease = graph(graph_id)?;
        let g = &lease.0.value.graph;
        let node = required(node, g)?;
        if g.nodes[node].node_type != "control" || g.nodes[node].kind != "loop" {
            return Err(STATUS_INVALID_INPUT);
        }
        let (body, exit) = g.loop_parts(node);
        let planner = g
            .loop_planner(node, body)
            .map(|p| (p, g.nodes[p].id.clone()));
        let state =
            crate::flow_loop::State::new(g.nodes[node].id.clone(), body, exit, planner, &input)?;
        let id = id()?;
        let mut response = state.metadata();
        response["handle"] = id.into();
        let raw = serde_json::to_vec(&response).map_err(|_| STATUS_INTERNAL)?;
        if raw.len() > capacity {
            unsafe { written.write(raw.len()) };
            return Err(STATUS_BUFFER_SMALL);
        }
        let calls = lock(&lease.0.calls)?;
        if calls.closed {
            return Err(STATUS_UNKNOWN_HANDLE);
        }
        let mut count = lock(&lease.0.value.cursors)?;
        *count = count.checked_add(1).ok_or(STATUS_INTERNAL)?;
        lock(loops())?.insert(
            id,
            Arc::new(Joined::new(LoopData {
                graph_id,
                graph: lease.0.clone(),
                state: Mutex::new(state),
            })),
        );
        unsafe { loop_output(&raw, output, capacity, written) }
    })
}
/// # Safety
/// For this call, `data` must be readable for `length` bytes when nonzero,
/// `output` writable for `capacity` bytes when nonzero, and `written` an
/// aligned writable usize. Buffers must remain valid and writable regions
/// must not overlap readable input or each other.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_loop_advance(
    graph_id: u64,
    loop_id: u64,
    data: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        let input = unsafe { loop_input(data, length, output, capacity, written) }?;
        let lease = loop_handle(graph_id, loop_id)?;
        let _graph = lease.0.value.graph.enter()?;
        let mut state = lock(&lease.0.value.state)?;
        let mut candidate = state.clone();
        let result = candidate.advance(&input)?;
        let raw = serde_json::to_vec(&result).map_err(|_| STATUS_INTERNAL)?;
        unsafe { loop_output(&raw, output, capacity, written) }?;
        *state = candidate;
        Ok(())
    })
}
/// # Safety
/// `data` must be readable for `length` bytes when nonzero; `output` writable
/// for `capacity` bytes when nonzero; `written` aligned and writable as usize.
/// All buffers stay valid during the call; writable regions must not overlap
/// readable input or each other.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_loop_advance_text(
    graph_id: u64,
    loop_id: u64,
    data: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        let input = unsafe { loop_input(data, length, output, capacity, written) }?;
        let lease = loop_handle(graph_id, loop_id)?;
        let _graph = lease.0.value.graph.enter()?;
        let mut state = lock(&lease.0.value.state)?;
        let mut candidate = state.clone();
        let result = candidate.advance_text(&input)?;
        let raw = serde_json::to_vec(&result).map_err(|_| STATUS_INTERNAL)?;
        unsafe { loop_output(&raw, output, capacity, written) }?;
        *state = candidate;
        Ok(())
    })
}
#[no_mangle]
pub extern "C" fn tofu_flow_loop_close(graph_id: u64, loop_id: u64) -> u32 {
    call(|| {
        let value = {
            let mut registry = lock(loops())?;
            if registry
                .get(&loop_id)
                .is_none_or(|v| v.value.graph_id != graph_id)
            {
                return Err(STATUS_UNKNOWN_HANDLE);
            }
            registry.remove(&loop_id).ok_or(STATUS_UNKNOWN_HANDLE)?
        };
        value.close()?;
        *lock(&value.value.graph.value.cursors)? -= 1;
        Ok(())
    })
}

/// Merge successful/settled outputs in definition order after the host joins
/// workers. No callback, audit, journal or failure classification occurs here.
/// # Safety
/// Input/output must be valid for their lengths until return, written aligned
/// and writable; writable regions may not overlap input or each other.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_parallel_merge(
    graph_id: u64,
    node: u64,
    data: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        let input = unsafe { loop_input(data, length, output, capacity, written) }?;
        let lease = graph(graph_id)?;
        let g = &lease.0.value.graph;
        let node = required(node, g)?;
        if g.nodes[node].node_type != "control" || g.nodes[node].kind != "parallel" {
            return Err(STATUS_INVALID_INPUT);
        }
        let object = input.as_object().ok_or(STATUS_INVALID_INPUT)?;
        if object.len() != PARALLEL_MERGE_FIELDS.len()
            || PARALLEL_MERGE_FIELDS
                .iter()
                .any(|field| !object.contains_key(*field))
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let context = input["context"].as_str().ok_or(STATUS_INVALID_INPUT)?;
        let outputs = input["outputs"].as_array().ok_or(STATUS_INVALID_INPUT)?;
        if outputs.len() != g.forward[node].len() {
            return Err(STATUS_INVALID_INPUT);
        }
        let mut parts = Vec::with_capacity(outputs.len() + 1);
        let mut size = context.len();
        if !context.is_empty() {
            parts.push(context);
        }
        for output in outputs {
            let output = output.as_str().ok_or(STATUS_INVALID_INPUT)?;
            if parts.is_empty() && output.is_empty() {
                continue;
            }
            size = size
                .checked_add(output.len())
                .and_then(|v| v.checked_add(if parts.is_empty() { 0 } else { 2 }))
                .ok_or(STATUS_INVALID_INPUT)?;
            parts.push(output);
        }
        let mut merged = String::new();
        merged
            .try_reserve_exact(size)
            .map_err(|_| STATUS_INTERNAL)?;
        for (i, part) in parts.into_iter().enumerate() {
            if i != 0 {
                merged.push_str("\n\n");
            }
            merged.push_str(part);
        }
        unsafe { loop_output(merged.as_bytes(), output, capacity, written) }
    })
}

/// Borrow caller-owned UTF-8 spans and copy the final ordered result directly
/// into the caller output. Validate every span and capacity before publication.
/// # Safety
/// `input` is aligned and readable for count Spans, whose nonempty data regions
/// are valid for length bytes until return. Output/written are writable and
/// nonoverlapping with all input regions and each other during this call.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_parallel_merge_spans(
    graph_id: u64,
    node: u64,
    input: *const Span,
    count: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        if !aligned(written)
            || !aligned(input)
            || count > isize::MAX as usize / std::mem::size_of::<Span>()
            || capacity > isize::MAX as usize
            || (capacity != 0 && output.is_null())
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = graph(graph_id)?;
        let g = &lease.0.value.graph;
        let node = required(node, g)?;
        if g.nodes[node].node_type != "control"
            || g.nodes[node].kind != "parallel"
            || count
                != g.forward[node]
                    .len()
                    .checked_add(1)
                    .ok_or(STATUS_INVALID_INPUT)?
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let spans = unsafe { std::slice::from_raw_parts(input, count) };
        let mut start = None;
        let mut size = 0usize;
        for (index, span) in spans.iter().enumerate() {
            if span.length > isize::MAX as usize || (span.length != 0 && span.data.is_null()) {
                return Err(STATUS_INVALID_INPUT);
            }
            let bytes = if span.length == 0 {
                &[][..]
            } else {
                unsafe { std::slice::from_raw_parts(span.data, span.length) }
            };
            std::str::from_utf8(bytes).map_err(|_| STATUS_INVALID_INPUT)?;
            if start.is_none() && bytes.is_empty() {
                continue;
            }
            size = size
                .checked_add(bytes.len())
                .and_then(|size| size.checked_add(if start.is_some() { 2 } else { 0 }))
                .ok_or(STATUS_INVALID_INPUT)?;
            if size > isize::MAX as usize {
                return Err(STATUS_INVALID_INPUT);
            }
            start.get_or_insert(index);
        }
        if size > capacity {
            unsafe { written.write(size) };
            return Err(STATUS_BUFFER_SMALL);
        }
        let mut offset = 0;
        if let Some(start) = start {
            for (i, span) in spans[start..].iter().enumerate() {
                if i != 0 {
                    unsafe {
                        std::ptr::copy_nonoverlapping(b"\n\n".as_ptr(), output.add(offset), 2)
                    };
                    offset += 2;
                }
                if span.length != 0 {
                    unsafe {
                        std::ptr::copy_nonoverlapping(span.data, output.add(offset), span.length)
                    };
                    offset += span.length;
                }
            }
        }
        unsafe { written.write(offset) };
        Ok(())
    })
}

/// # Safety
/// Output is aligned and writable for capacity u64s; written is aligned and
/// writable for a usize; writable regions do not overlap and live until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_branch_plan(
    graph_id: u64,
    node: u64,
    enabled: u32,
    output: *mut u64,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        if enabled > 1
            || !aligned(written)
            || capacity > isize::MAX as usize / 8
            || (capacity != 0 && !aligned(output))
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = graph(graph_id)?;
        let g = &lease.0.value.graph;
        let node = required(node, g)?;
        if g.nodes[node].node_type != "control" || g.nodes[node].kind != "branch" {
            return Err(STATUS_INVALID_INPUT);
        }
        let branches = &g.forward[node];
        let needed = branches.len().checked_add(1).ok_or(STATUS_INVALID_INPUT)?;
        if needed > capacity {
            unsafe { written.write(needed) };
            return Err(STATUS_BUFFER_SMALL);
        }
        unsafe {
            output.write(u64::from(enabled != 0 && branches.len() > 1));
        }
        for (i, &branch) in branches.iter().enumerate() {
            unsafe {
                output.add(i + 1).write(branch as u64);
            }
        }
        unsafe { written.write(needed) };
        Ok(())
    })
}
/// # Safety
/// Input is aligned and readable for count Spans; nonempty span data is valid
/// and immutable for its length until return. Output/written are aligned,
/// writable for their capacities and nonoverlapping with all readable regions.
#[no_mangle]
pub unsafe extern "C" fn tofu_flow_branch_choose(
    graph_id: u64,
    node: u64,
    input: *const Span,
    count: usize,
    output: *mut u64,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    call(|| {
        if !aligned(input)
            || !aligned(written)
            || count > isize::MAX as usize / std::mem::size_of::<Span>()
            || capacity > isize::MAX as usize / 8
            || (capacity != 0 && !aligned(output))
        {
            return Err(STATUS_INVALID_INPUT);
        }
        let lease = graph(graph_id)?;
        let g = &lease.0.value.graph;
        let node = required(node, g)?;
        if g.nodes[node].node_type != "control" || g.nodes[node].kind != "branch" {
            return Err(STATUS_INVALID_INPUT);
        }
        let mut seen = std::collections::HashSet::new();
        let branches = g.forward[node]
            .iter()
            .copied()
            .filter(|branch| seen.insert(*branch))
            .collect::<Vec<_>>();
        if count != 1 && count != branches.len().checked_add(1).ok_or(STATUS_INVALID_INPUT)? {
            return Err(STATUS_INVALID_INPUT);
        }
        let spans = unsafe { std::slice::from_raw_parts(input, count) };
        let text = |span: &Span| -> Result<&str, u32> {
            if span.length > isize::MAX as usize || (span.length != 0 && span.data.is_null()) {
                return Err(STATUS_INVALID_INPUT);
            }
            let bytes = if span.length == 0 {
                &[][..]
            } else {
                unsafe { std::slice::from_raw_parts(span.data, span.length) }
            };
            std::str::from_utf8(bytes).map_err(|_| STATUS_INVALID_INPUT)
        };
        let verdict = text(&spans[0])?;
        let labels = spans[1..]
            .iter()
            .zip(&branches)
            .map(|(span, &branch)| Ok((branch, text(span)?)))
            .collect::<Result<Vec<_>, u32>>()?;
        let selected = if count > 1 {
            crate::flow_branch::choose(verdict, &labels)
        } else {
            None
        };
        let values = [
            encoded(selected.or_else(|| g.forward[node].first().copied())),
            u64::from(selected.is_some()),
        ];
        if capacity < values.len() {
            unsafe { written.write(values.len()) };
            return Err(STATUS_BUFFER_SMALL);
        }
        unsafe {
            std::ptr::copy_nonoverlapping(values.as_ptr(), output, values.len());
            written.write(values.len())
        };
        Ok(())
    })
}
