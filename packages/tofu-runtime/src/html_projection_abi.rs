//! Synchronous HTML projection ABI. Results are borrowed only during the sink;
//! there is no handle registry, retained output, interpreter or worker owner.
use crate::html_projection::{self, Error, PROFILE};
#[path = "generated_html_projection_abi.rs"]
#[rustfmt::skip]
mod contract;
pub use contract::*;
use sha2::{Digest, Sha256};
use std::{
    ffi::c_void,
    panic::{catch_unwind, AssertUnwindSafe},
};

pub type Sink = Option<unsafe extern "C" fn(*mut c_void, *const u8, usize) -> u32>;

fn guarded(operation: impl FnOnce() -> Result<(), u32>) -> u32 {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => STATUS_OK,
        Ok(Err(status)) => status,
        Err(_) => STATUS_PANIC,
    }
}

#[no_mangle]
pub extern "C" fn tofu_html_projection_abi_version() -> u32 {
    ABI_VERSION
}

/// # Safety
/// sink must remain callable until return, accept context and borrow the supplied
/// bytes only during its call. It must not unwind across C. context may be null.
#[no_mangle]
pub unsafe extern "C" fn tofu_html_projection_profile(sink: Sink, context: *mut c_void) -> u32 {
    guarded(|| {
        let sink = sink.ok_or(STATUS_INVALID_BUFFER)?;
        let hash = |bytes: &[u8]| format!("{:x}", Sha256::digest(bytes));
        let value = serde_json::json!({
            "python_version": PROFILE.python_version,
            "tokenizer_sha256": PROFILE.tokenizer_sha256,
            "owner_sha256": PROFILE.owner_sha256,
            "native_sources": {
                "html_projection.rs": hash(include_bytes!("html_projection.rs")),
                "html_tokenizer.rs": hash(include_bytes!("html_tokenizer.rs")),
                "generated_html_projection.rs": hash(include_bytes!("generated_html_projection.rs")),
                "html_projection_abi.rs": hash(include_bytes!("html_projection_abi.rs")),
                "generated_html_projection_abi.rs": hash(include_bytes!("generated_html_projection_abi.rs")),
            }
        }).to_string();
        if unsafe { sink(context, value.as_ptr(), value.len()) } != 0 {
            return Err(STATUS_SINK);
        }
        Ok(())
    })
}

/// # Safety
/// input must be readable for length bytes until return; null is allowed only
/// for zero length. sink/context follow profile's safety contract. The sink is
/// called once only after successful projection; its bytes are valid UTF-8.
#[no_mangle]
pub unsafe extern "C" fn tofu_html_projection_project(
    input: *const u8,
    length: usize,
    projection_budget: usize,
    sink: Sink,
    context: *mut c_void,
) -> u32 {
    guarded(|| {
        let sink = sink.ok_or(STATUS_INVALID_BUFFER)?;
        if length > isize::MAX as usize || (length > 0 && input.is_null()) {
            return Err(STATUS_INVALID_BUFFER);
        }
        let bytes = if length == 0 {
            &[][..]
        } else {
            unsafe { std::slice::from_raw_parts(input, length) }
        };
        let text = std::str::from_utf8(bytes).map_err(|_| STATUS_INVALID_UTF8)?;
        let result = html_projection::project_bounded(text, projection_budget).map_err(
            |error| match error {
                Error::NumericReference => STATUS_NUMERIC_REFERENCE,
                Error::ProjectionBudget => STATUS_PROJECTION_BUDGET,
                Error::Allocation => STATUS_ALLOCATION,
            },
        )?;
        if unsafe { sink(context, result.as_ptr(), result.len()) } != 0 {
            return Err(STATUS_SINK);
        }
        Ok(())
    })
}
