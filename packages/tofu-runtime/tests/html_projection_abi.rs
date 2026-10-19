#![cfg(feature = "html-projection-abi")]
use std::{ffi::c_void, ptr};
use tofu_runtime::html_projection_abi::*;

unsafe extern "C" fn collect(context: *mut c_void, input: *const u8, length: usize) -> u32 {
    let output = unsafe { &mut *context.cast::<Vec<String>>() };
    let bytes = unsafe { std::slice::from_raw_parts(input, length) };
    output.push(std::str::from_utf8(bytes).unwrap().to_owned());
    0
}

#[test]
fn borrowed_complete_results_and_refusal_never_publish_partial_output() {
    let mut values: Vec<String> = vec![];
    let ctx = (&mut values as *mut Vec<String>).cast();
    assert_eq!(tofu_html_projection_abi_version(), ABI_VERSION);
    let text = "<h1>世界</h1><p>a\0b</p>";
    unsafe {
        assert_eq!(
            tofu_html_projection_project(text.as_ptr(), text.len(), usize::MAX, Some(collect), ctx),
            STATUS_OK
        );
        assert_eq!(values, vec!["# 世界\na\0b"]);
        values.clear();
        assert_eq!(
            tofu_html_projection_project(ptr::null(), 0, 0, Some(collect), ctx),
            STATUS_OK
        );
        assert_eq!(values, vec![""]);
        values.clear();
        assert_eq!(
            tofu_html_projection_project(ptr::null(), 1, 20, Some(collect), ctx),
            STATUS_INVALID_BUFFER
        );
        assert_eq!(
            tofu_html_projection_project(text.as_ptr(), usize::MAX, 20, Some(collect), ctx),
            STATUS_INVALID_BUFFER
        );
        assert_eq!(
            tofu_html_projection_project(text.as_ptr(), text.len(), 1, Some(collect), ctx),
            STATUS_PROJECTION_BUDGET
        );
        assert_eq!(
            tofu_html_projection_project(b"\xff".as_ptr(), 1, 20, Some(collect), ctx),
            STATUS_INVALID_UTF8
        );
        let numeric = format!("&#{};", "9".repeat(4301));
        assert_eq!(
            tofu_html_projection_project(
                numeric.as_ptr(),
                numeric.len(),
                usize::MAX,
                Some(collect),
                ctx
            ),
            STATUS_NUMERIC_REFERENCE
        );
        assert_eq!(
            tofu_html_projection_project(ptr::null(), 0, 0, None, ctx),
            STATUS_INVALID_BUFFER
        );
    }
    assert!(values.is_empty());
}

#[test]
fn profile_is_observable_and_sink_rejection_is_explicit() {
    let mut values: Vec<String> = vec![];
    unsafe {
        assert_eq!(
            tofu_html_projection_profile(Some(collect), (&mut values as *mut Vec<String>).cast()),
            STATUS_OK
        );
    }
    let profile: serde_json::Value = serde_json::from_str(&values[0]).unwrap();
    assert_eq!(
        profile["python_version"],
        tofu_runtime::html_projection::PROFILE.python_version
    );
    assert_eq!(profile["native_sources"].as_object().unwrap().len(), 5);
    unsafe extern "C" fn refuse(_: *mut c_void, _: *const u8, _: usize) -> u32 {
        1
    }
    unsafe {
        assert_eq!(
            tofu_html_projection_project(ptr::null(), 0, 0, Some(refuse), ptr::null_mut()),
            STATUS_SINK
        );
        assert_eq!(
            tofu_html_projection_profile(Some(refuse), ptr::null_mut()),
            STATUS_SINK
        );
        assert_eq!(
            tofu_html_projection_profile(None, ptr::null_mut()),
            STATUS_INVALID_BUFFER
        );
    }
}
