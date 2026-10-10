//! Optional requested-byte attribution for this library's Rust allocator.
//! Entry: AccountingMiMalloc global allocator and current/peak snapshots.
//! Dependencies: mimalloc::MiMalloc, std::alloc and lock-free atomic counters.
//! Counts live Layout sizes, not arenas/RSS, C allocations or host allocators.
//! This module is absent unless allocator-diagnostics is explicitly selected.
use std::alloc::{GlobalAlloc, Layout};
use std::sync::atomic::{AtomicUsize, Ordering};

pub(crate) struct AccountingMiMalloc {
    current: AtomicUsize,
    peak: AtomicUsize,
}
impl AccountingMiMalloc {
    pub(crate) const fn new() -> Self {
        Self { current: AtomicUsize::new(0), peak: AtomicUsize::new(0) }
    }
    fn add(&self, bytes: usize) {
        let current = self.current.fetch_add(bytes, Ordering::Relaxed) + bytes;
        self.peak.fetch_max(current, Ordering::Relaxed);
    }
    pub(crate) fn current_bytes(&self) -> u64 { self.current.load(Ordering::Relaxed) as u64 }
    pub(crate) fn peak_bytes(&self) -> u64 { self.peak.load(Ordering::Relaxed) as u64 }
}
// SAFETY: every allocation is delegated unchanged to MiMalloc. Counters are
// nonallocating observations; failed allocation/reallocation leaves accounting
// unchanged. Successful realloc transfers the exact old/new Layout charge.
unsafe impl GlobalAlloc for AccountingMiMalloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { mimalloc::MiMalloc.alloc(layout) };
        if !pointer.is_null() { self.add(layout.size()); }
        pointer
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { mimalloc::MiMalloc.alloc_zeroed(layout) };
        if !pointer.is_null() { self.add(layout.size()); }
        pointer
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { mimalloc::MiMalloc.dealloc(pointer, layout); }
        self.current.fetch_sub(layout.size(), Ordering::Relaxed);
    }
    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        let replacement = unsafe { mimalloc::MiMalloc.realloc(pointer, layout, size) };
        if !replacement.is_null() {
            if size >= layout.size() { self.add(size - layout.size()); }
            else { self.current.fetch_sub(layout.size() - size, Ordering::Relaxed); }
        }
        replacement
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn requested_bytes_follow_alignment_reallocation_and_cross_thread_release() {
        let allocator = std::sync::Arc::new(AccountingMiMalloc::new());
        let layout = Layout::from_size_align(513, 64).unwrap();
        let pointer = unsafe { allocator.alloc_zeroed(layout) };
        assert!(!pointer.is_null());
        assert_eq!(pointer as usize % 64, 0);
        assert!(unsafe { std::slice::from_raw_parts(pointer, 513) }.iter().all(|byte| *byte == 0));
        assert_eq!(allocator.current_bytes(), 513);
        unsafe { std::ptr::write_bytes(pointer, 17, 513); }
        let pointer = unsafe { allocator.realloc(pointer, layout, 1025) };
        assert!(!pointer.is_null());
        assert!(unsafe { std::slice::from_raw_parts(pointer, 513) }.iter().all(|byte| *byte == 17));
        assert_eq!(allocator.current_bytes(), 1025);
        let address = pointer as usize;
        let owner = std::sync::Arc::clone(&allocator);
        std::thread::spawn(move || unsafe {
            owner.dealloc(address as *mut u8, Layout::from_size_align(1025, 64).unwrap());
        }).join().unwrap();
        assert_eq!(allocator.current_bytes(), 0);
        assert_eq!(allocator.peak_bytes(), 1025);
    }
}
