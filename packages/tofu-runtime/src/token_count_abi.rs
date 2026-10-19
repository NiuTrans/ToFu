//! Feature-gated pure token-count C ABI. Caller buffers are borrowed per call.
//! Handles own lazy immutable encoders; close withdraws and joins accepted calls.
//! No task/account/store authority, prompt cache, thread pool or subprocess.
use crate::token_ingress::{Encoder, Encoding};
#[path = "generated_token_count_abi.rs"]
mod contract;
pub use contract::*;
use std::{
    collections::BTreeMap,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Condvar, Mutex, OnceLock,
    },
};

#[repr(C)]
#[derive(Clone, Copy)]
pub struct TextSpan {
    pub data: *const u8,
    pub length: usize,
}
struct Calls {
    closed: bool,
    active: usize,
}
struct Handle {
    encoder: Arc<Encoder>,
    calls: Mutex<Calls>,
    changed: Condvar,
}
static HANDLES: OnceLock<Mutex<BTreeMap<u64, Arc<Handle>>>> = OnceLock::new();
static NEXT: AtomicU64 = AtomicU64::new(1);
fn handles() -> &'static Mutex<BTreeMap<u64, Arc<Handle>>> {
    HANDLES.get_or_init(|| Mutex::new(BTreeMap::new()))
}
fn lock<T>(value: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    value.lock().unwrap_or_else(|error| error.into_inner())
}
struct Lease(Arc<Handle>);
impl Drop for Lease {
    fn drop(&mut self) {
        let mut calls = lock(&self.0.calls);
        calls.active -= 1;
        self.0.changed.notify_all();
    }
}
fn acquire(id: u64) -> Result<Lease, u32> {
    let handle = lock(handles()).get(&id).cloned().ok_or(STATUS_CLOSED)?;
    {
        let mut calls = lock(&handle.calls);
        if calls.closed {
            return Err(STATUS_CLOSED);
        }
        calls.active = calls.active.checked_add(1).ok_or(STATUS_CAPACITY)?;
    }
    Ok(Lease(handle))
}
fn guarded(operation: impl FnOnce() -> Result<(), u32>) -> u32 {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => STATUS_OK,
        Ok(Err(status)) => status,
        Err(_) => STATUS_PANIC,
    }
}
fn aligned<T>(pointer: *const T) -> bool {
    !pointer.is_null() && (pointer as usize) % std::mem::align_of::<T>() == 0
}

#[no_mangle]
pub extern "C" fn tofu_token_count_abi_version() -> u32 {
    ABI_VERSION
}

/// # Safety
/// output must be writable/aligned for one u64 and remain alive until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_token_count_open(encoding: u32, output: *mut u64) -> u32 {
    guarded(|| {
        if !aligned(output) {
            return Err(STATUS_INVALID_BUFFER);
        }
        let encoding = match encoding {
            ENCODING_CL100K => Encoding::Cl100k,
            ENCODING_O200K => Encoding::O200k,
            _ => return Err(STATUS_INVALID_ENCODING),
        };
        let id = NEXT
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |current| {
                current.checked_add(1)
            })
            .map_err(|_| STATUS_CAPACITY)?;
        let handle = Arc::new(Handle {
            encoder: Encoder::new(encoding),
            calls: Mutex::new(Calls {
                closed: false,
                active: 0,
            }),
            changed: Condvar::new(),
        });
        lock(handles()).insert(id, handle);
        unsafe { output.write(id) };
        Ok(())
    })
}

/// # Safety
/// spans must be readable/aligned for length TextSpans; every nonempty span must
/// be readable for its byte length. output must be writable/aligned for capacity
/// u64s. All buffers are nonoverlapping and remain alive until return. Zero-length
/// spans may use null data; zero-length batches may use null array/output pointers.
#[no_mangle]
pub unsafe extern "C" fn tofu_token_count_batch(
    id: u64,
    spans: *const TextSpan,
    length: usize,
    output: *mut u64,
    capacity: usize,
) -> u32 {
    guarded(|| {
        if capacity < length {
            return Err(STATUS_CAPACITY);
        }
        if length > (isize::MAX as usize) / std::mem::size_of::<TextSpan>()
            || capacity > (isize::MAX as usize) / std::mem::size_of::<u64>()
        {
            return Err(STATUS_INVALID_BUFFER);
        }
        if length > 0 && (!aligned(spans) || !aligned(output)) {
            return Err(STATUS_INVALID_BUFFER);
        }
        let lease = acquire(id)?;
        let source = if length == 0 {
            &[][..]
        } else {
            unsafe { std::slice::from_raw_parts(spans, length) }
        };
        let mut texts = Vec::new();
        texts
            .try_reserve_exact(length)
            .map_err(|_| STATUS_CAPACITY)?;
        for span in source {
            if span.length > isize::MAX as usize || (span.length > 0 && span.data.is_null()) {
                return Err(STATUS_INVALID_BUFFER);
            }
            let bytes = if span.length == 0 {
                &[][..]
            } else {
                unsafe { std::slice::from_raw_parts(span.data, span.length) }
            };
            texts.push(std::str::from_utf8(bytes).map_err(|_| STATUS_INVALID_UTF8)?);
        }
        let mut counts = Vec::new();
        counts
            .try_reserve_exact(length)
            .map_err(|_| STATUS_CAPACITY)?;
        let mut scratch = None;
        for text in texts {
            counts.push(
                lease
                    .0
                    .encoder
                    .count(text, &mut scratch)
                    .map_err(|_| STATUS_ENCODER)?,
            );
        }
        if length > 0 {
            unsafe { std::ptr::copy_nonoverlapping(counts.as_ptr(), output, length) }
        }
        Ok(())
    })
}

#[no_mangle]
pub extern "C" fn tofu_token_count_close(id: u64) -> u32 {
    guarded(|| {
        let handle = lock(handles()).remove(&id).ok_or(STATUS_CLOSED)?;
        let mut calls = lock(&handle.calls);
        calls.closed = true;
        while calls.active > 0 {
            calls = handle
                .changed
                .wait(calls)
                .unwrap_or_else(|error| error.into_inner());
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn open(encoding: u32) -> u64 {
        let mut id = 0;
        assert_eq!(
            unsafe { tofu_token_count_open(encoding, &mut id) },
            STATUS_OK
        );
        id
    }
    #[test]
    fn errors_preserve_output_and_stale_handles_refuse() {
        let id = open(ENCODING_CL100K);
        let invalid = [255u8];
        let spans = [
            TextSpan {
                data: b"hello".as_ptr(),
                length: 5,
            },
            TextSpan {
                data: invalid.as_ptr(),
                length: 1,
            },
        ];
        let mut output = [77, 88];
        assert_eq!(
            unsafe { tofu_token_count_batch(id, spans.as_ptr(), 2, output.as_mut_ptr(), 2) },
            STATUS_INVALID_UTF8
        );
        assert_eq!(output, [77, 88]);
        assert_eq!(
            unsafe { tofu_token_count_batch(id, spans.as_ptr(), 2, output.as_mut_ptr(), 1) },
            STATUS_CAPACITY
        );
        assert_eq!(output, [77, 88]);
        assert_eq!(tofu_token_count_close(id), STATUS_OK);
        assert_eq!(tofu_token_count_close(id), STATUS_CLOSED);
        assert_eq!(
            unsafe { tofu_token_count_batch(id, std::ptr::null(), 0, std::ptr::null_mut(), 0) },
            STATUS_CLOSED
        );
        let next = open(ENCODING_CL100K);
        assert!(next > id);
        assert_eq!(tofu_token_count_close(next), STATUS_OK);
    }
    #[test]
    fn null_empty_and_invalid_encoding_leave_publication_safe() {
        let mut output = 99;
        assert_eq!(
            unsafe { tofu_token_count_open(99, &mut output) },
            STATUS_INVALID_ENCODING
        );
        assert_eq!(output, 99);
        assert_eq!(
            unsafe { tofu_token_count_open(ENCODING_CL100K, std::ptr::null_mut()) },
            STATUS_INVALID_BUFFER
        );
        let id = open(ENCODING_CL100K);
        assert_eq!(
            unsafe { tofu_token_count_batch(id, std::ptr::null(), 0, std::ptr::null_mut(), 0) },
            STATUS_OK
        );
        assert!(!lock(handles()).get(&id).unwrap().encoder.loaded());
        assert_eq!(
            unsafe { tofu_token_count_batch(id, std::ptr::null(), 1, &mut output, 1) },
            STATUS_INVALID_BUFFER
        );
        assert_eq!(output, 99);
        assert_eq!(tofu_token_count_close(id), STATUS_OK);
    }
    #[test]
    fn panic_is_translated_without_crossing_c_boundary() {
        assert_eq!(guarded(|| panic!("injected")), STATUS_PANIC);
        assert_eq!(guarded(|| Err(STATUS_ENCODER)), STATUS_ENCODER);
    }
    #[test]
    fn counts_match_pinned_reference_for_both_encodings() {
        for encoding in [ENCODING_CL100K, ENCODING_O200K] {
            let reference = if encoding == ENCODING_CL100K {
                tiktoken_rs::cl100k_base().unwrap()
            } else {
                tiktoken_rs::o200k_base().unwrap()
            };
            let id = open(encoding);
            let texts = [
                "",
                "hello 世界 café",
                "<|endoftext|>",
                "🙂🙂 abc",
                "a\n\t b",
            ];
            let spans: Vec<_> = texts
                .iter()
                .map(|text| TextSpan {
                    data: text.as_ptr(),
                    length: text.len(),
                })
                .collect();
            let mut output = [0; 5];
            assert_eq!(
                unsafe {
                    tofu_token_count_batch(
                        id,
                        spans.as_ptr(),
                        spans.len(),
                        output.as_mut_ptr(),
                        output.len(),
                    )
                },
                STATUS_OK
            );
            assert_eq!(
                output,
                texts.map(|text| reference.encode_ordinary(text).len() as u64)
            );
            assert_eq!(tofu_token_count_close(id), STATUS_OK);
        }
    }
    #[test]
    fn close_releases_loaded_encoder_and_registry_entry() {
        let id = open(ENCODING_CL100K);
        let encoder = {
            let registry = lock(handles());
            Arc::downgrade(&registry.get(&id).unwrap().encoder)
        };
        let text = b"loaded vocabulary";
        let spans = [TextSpan {
            data: text.as_ptr(),
            length: text.len(),
        }];
        let mut count = 0;
        assert_eq!(
            unsafe { tofu_token_count_batch(id, spans.as_ptr(), 1, &mut count, 1) },
            STATUS_OK
        );
        assert!(encoder.upgrade().unwrap().loaded());
        assert_eq!(tofu_token_count_close(id), STATUS_OK);
        assert!(!lock(handles()).contains_key(&id));
        assert!(encoder.upgrade().is_none());
    }

    #[test]
    fn close_waits_for_accepted_call_and_revokes_new_lookup() {
        let id = open(ENCODING_CL100K);
        let lease = acquire(id).unwrap();
        let (done, received) = std::sync::mpsc::channel();
        let thread = std::thread::spawn(move || {
            done.send(tofu_token_count_close(id)).unwrap();
        });
        while lock(handles()).contains_key(&id) {
            std::thread::yield_now();
        }
        assert!(acquire(id).is_err());
        assert!(received.try_recv().is_err());
        drop(lease);
        assert_eq!(
            received
                .recv_timeout(std::time::Duration::from_secs(2))
                .unwrap(),
            STATUS_OK
        );
        thread.join().unwrap();
    }
}
