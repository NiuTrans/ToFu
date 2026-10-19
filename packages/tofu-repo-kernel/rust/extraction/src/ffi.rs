//! Experimental coarse C boundary, sharing the CLI's exact request and extraction.
//! Caller owns buffers. Loaded grammars remain resident until process exit.
//! No hard timeout, cancellation or installed-artifact admission is claimed here.
use crate::wire;
use std::{path::PathBuf, ptr, slice, str};

fn response(grammar: &[u8], request: &[u8], limit: usize) -> Result<Vec<u8>, ()> {
    let grammar = str::from_utf8(grammar).map_err(|_| ())?;
    wire::response(PathBuf::from(grammar), request, limit).map_err(|_| ())
}

/// Extract one JSON batch into caller memory: 0 success, 2 refusal, 3 panic.
/// Refusal/panic leave output bytes unchanged and set written to zero.
/// The grammar directory is explicit trusted UTF-8; no implicit downloads.
///
/// # Safety
/// Non-null inputs must reference readable regions of the supplied sizes.
/// Output and written must reference distinct writable regions, disjoint from
/// both inputs, valid for the full call. No region may be concurrently mutated.
/// Regions must fit isize::MAX. Null/empty buffers are refused. Caller ignores
/// output on nonzero status. Rust panic containment cannot contain native faults,
/// allocator aborts or a grammar that blocks; invoke in an owned process when
/// hard cancellation or grammar isolation is required.
#[no_mangle]
pub unsafe extern "C" fn tofu_repository_extract_json_v1(
    grammar: *const u8,
    grammar_len: usize,
    request: *const u8,
    request_len: usize,
    output: *mut u8,
    output_capacity: usize,
    written: *mut usize,
) -> i32 {
    if written.is_null() {
        return 2;
    }
    // SAFETY: required writable, aligned scalar supplied by the caller.
    unsafe { written.write(0) };
    if grammar.is_null()
        || request.is_null()
        || output.is_null()
        || [grammar_len, request_len, output_capacity]
            .iter()
            .any(|&size| size == 0 || size > isize::MAX as usize)
    {
        return 2;
    }
    let result = std::panic::catch_unwind(|| {
        // SAFETY: caller owns the two nonempty readable regions for this call.
        let grammar = unsafe { slice::from_raw_parts(grammar, grammar_len) };
        let request = unsafe { slice::from_raw_parts(request, request_len) };
        response(grammar, request, output_capacity)
    });
    match result {
        Ok(Ok(bytes)) => {
            // SAFETY: serialization bounded bytes to output_capacity; caller
            // supplies disjoint output/written regions, still alive here.
            unsafe {
                ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len());
                written.write(bytes.len());
            }
            0
        }
        Ok(Err(())) => 2,
        Err(_) => 3,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn malformed_or_oversized_response_does_not_publish_partial_output() {
        let grammar = b"unused-no-files";
        let empty = br#"{"format":"tofu.repository-extraction/v1","files":[]}"#;
        for (request, capacity) in [(b"{".as_slice(), 64), (empty.as_slice(), 1)] {
            let mut buffer = [0x55; 64];
            let mut written = 999;
            let status = unsafe {
                tofu_repository_extract_json_v1(
                    grammar.as_ptr(),
                    grammar.len(),
                    request.as_ptr(),
                    request.len(),
                    buffer.as_mut_ptr(),
                    capacity,
                    &mut written,
                )
            };
            assert_eq!(status, 2);
            assert_eq!(written, 0);
            assert_eq!(buffer, [0x55; 64]);
        }
    }
}
