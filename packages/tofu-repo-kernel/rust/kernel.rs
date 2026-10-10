//! Owned-call CSR PageRank kernel; no database, Python objects or retained state.
//! Entry: tofu_pagerank_csr_v1. Caller owns valid disjoint input/output buffers
//! for the whole call. Weights and personalization are normalized here; iteration
//! order, defaults, dangling redistribution and stopping rule follow NetworkX's
//! SciPy implementation. Return: 0 success, 1 invalid input, 2 nonconvergence,
//! 3 contained panic. Output is written only on success.
use std::{panic::{catch_unwind, AssertUnwindSafe}, slice};

const DAMPING: f64 = 0.85;

/// # Safety
/// Nonnull pointers must reference aligned arrays of the declared lengths.
/// Inputs stay immutable and output does not alias any input for this call.
#[no_mangle]
pub unsafe extern "C" fn tofu_pagerank_csr_v1(
    nodes: usize, edges: usize, offsets: *const usize, targets: *const usize,
    weights: *const f64, personal: *const f64, output: *mut f64,
    tolerance: f64, maximum_iterations: usize,
) -> i32 {
    if nodes == 0 || nodes >= isize::MAX as usize / 8
        || edges > isize::MAX as usize / 8 || offsets.is_null()
        || personal.is_null() || output.is_null()
        || (edges > 0 && (targets.is_null() || weights.is_null()))
        || !tolerance.is_finite() || tolerance <= 0.0 || maximum_iterations == 0 {
        return 1;
    }
    // Buffer validity/alignment is the caller's ABI obligation. Length overflow
    // and nulls were checked above, including the nodes+1 offset sentinel.
    let offsets = unsafe { slice::from_raw_parts(offsets, nodes + 1) };
    let personal = unsafe { slice::from_raw_parts(personal, nodes) };
    let (targets, weights) = if edges == 0 { (&[][..], &[][..]) } else {
        (unsafe { slice::from_raw_parts(targets, edges) },
         unsafe { slice::from_raw_parts(weights, edges) })
    };
    if offsets[0] != 0 || offsets[nodes] != edges
        || offsets.windows(2).any(|pair| pair[0] > pair[1])
        || targets.iter().any(|target| *target >= nodes)
        || weights.iter().any(|weight| !weight.is_finite() || *weight < 0.0)
        || personal.iter().any(|value| !value.is_finite() || *value < 0.0) {
        return 1;
    }
    catch_unwind(AssertUnwindSafe(|| {
        let personal_sum: f64 = personal.iter().sum();
        if personal_sum <= 0.0 || !personal_sum.is_finite() { return 1; }
        let personal: Vec<f64> = personal.iter().map(|v| v / personal_sum).collect();
        let mut normalized = weights.to_vec();
        let mut dangling = Vec::new();
        for row in 0..nodes {
            let range = offsets[row]..offsets[row + 1];
            let total: f64 = weights[range.clone()].iter().sum();
            if !total.is_finite() { return 1; }
            if total == 0.0 { dangling.push(row); }
            else {
                let reciprocal = 1.0 / total;
                for weight in &mut normalized[range] { *weight *= reciprocal; }
            }
        }
        let mut previous = vec![1.0 / nodes as f64; nodes];
        let mut next = vec![0.0; nodes];
        for _ in 0..maximum_iterations {
            next.fill(0.0);
            for row in 0..nodes {
                for edge in offsets[row]..offsets[row + 1] {
                    next[targets[edge]] += previous[row] * normalized[edge];
                }
            }
            let dangling_mass: f64 = dangling.iter().map(|&row| previous[row]).sum();
            let mut error = 0.0;
            for row in 0..nodes {
                next[row] = DAMPING * (next[row] + dangling_mass * personal[row])
                    + (1.0 - DAMPING) * personal[row];
                error += (next[row] - previous[row]).abs();
            }
            if error < nodes as f64 * tolerance {
                // The caller supplies a separate, writable nodes-element buffer.
                unsafe { slice::from_raw_parts_mut(output, nodes) }.copy_from_slice(&next);
                return 0;
            }
            std::mem::swap(&mut previous, &mut next);
        }
        2
    })).unwrap_or(3)
}
