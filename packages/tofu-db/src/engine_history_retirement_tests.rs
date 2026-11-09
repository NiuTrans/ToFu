// History retirement and routing pages (docs/STORAGE_TOFUDB.md, "Lazy history
// routing retirement"). Live 2026-11-08: one automatic slice retired eight of
// 167,184 retained sources and rewrote ~2,580 of the 2,781 routing pages
// (~10.8 GB) to drop ~7,300 hash-spread locations; routing pages were 78% of
// the authority's new bytes.
use super::*;
use crate::history_payload::RoutingRoot;

const PAYLOADS_PER_COMMIT: usize = 1_000;

fn routed_payload(source: usize, index: usize) -> Vec<u8> {
    format!("routed history payload {source}:{index}").into_bytes()
}

/// Commits `payloads` carried blocks as one indexed history source.
fn commit_routed_source(engine: &mut Engine, source: usize, payloads: usize) {
    for first in (0..payloads).step_by(PAYLOADS_PER_COMMIT) {
        let owned: Vec<Vec<u8>> = (first..payloads.min(first + PAYLOADS_PER_COMMIT))
            .map(|index| routed_payload(source, index))
            .collect();
        let borrowed: Vec<&[u8]> = owned.iter().map(Vec::as_slice).collect();
        engine.commit_transaction(format!("routed {source}:{first}").as_bytes(), &borrowed).unwrap();
    }
    indexed_rotate(engine);
}

/// `sources` indexed history sources of `payloads` carried blocks each, behind
/// an authority root (history compaction needs one).
fn routed_history(sources: usize, payloads: usize) -> (Arc<DeterministicVfs>, Engine) {
    let (vfs, mut engine) = initialized_simulated_engine();
    let root = engine.write_block(b"routed history root").unwrap();
    engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
    for source in 0..sources {
        commit_routed_source(&mut engine, source, payloads);
    }
    (vfs, engine)
}

fn routing_root(engine: &Engine) -> (BlockId, RoutingRoot) {
    let id = engine.history_manifest.as_ref().unwrap().payload_index_root_id.unwrap();
    (id, RoutingRoot::decode(&engine.blocks.get(id).unwrap()).unwrap())
}

/// Locations across every run, read from the root's canonical encoding.
fn routing_entries(root: &RoutingRoot) -> u64 {
    let bytes = root.encode().unwrap();
    let levels = u32::from_le_bytes(bytes[12..16].try_into().unwrap());
    let (mut offset, mut entries) = (16, 0);
    for _ in 0..levels {
        let present = bytes[offset] == 1;
        offset += 1;
        if present {
            entries += u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap());
            let pages = u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap()) as usize;
            offset += 12 + pages * 100;
        }
    }
    entries
}

/// One automatic slice (`AuthorityDatabase::try_prepare_history_compaction`
/// shape) retiring the oldest `retire` sources.
fn retire_oldest(engine: &mut Engine, retire: usize) {
    let retained = engine.history_segment_count() - retire;
    let prepared = engine.try_prepare_history_compaction(retained).unwrap().unwrap();
    let outcome = encode_history_compaction(&prepared);
    engine.finish_history_compaction(prepared, outcome).unwrap();
    assert_eq!(engine.history_segment_count(), retained);
}

fn assert_routed_payloads_read(engine: &Engine, sources: std::ops::Range<usize>, payloads: usize, context: &str) {
    for source in sources {
        for index in (0..payloads).step_by(997).chain([payloads - 1]) {
            let payload = routed_payload(source, index);
            assert_eq!(engine.read_block(BlockId::for_payload(&payload)).unwrap(), payload,
                "{context}: source {source} payload {index}");
        }
    }
}

/// A slice retires sources from the history manifest only. The routing run
/// holding their locations is not rewritten: its pages index every retained
/// source too (block IDs interleave every source), so dropping the retired
/// locations eagerly rewrote nearly the whole routing table per slice.
#[test]
fn automatic_history_compaction_retires_sources_without_rewriting_routing_pages() {
    // Four sources of 14,000 locations merge into one 56,000-entry run, more
    // than one routing page holds, so every page mixes all four sources.
    const SOURCES: usize = 4;
    const PAYLOADS: usize = 14_000;
    let (vfs, mut engine) = routed_history(SOURCES, PAYLOADS);
    assert_eq!(engine.history_segment_count(), SOURCES);
    let (root_id, root) = routing_root(&engine);
    let pages: Vec<BlockId> = root.page_ids().collect();
    assert!(pages.len() >= 2, "the fixture must spread routing over several pages");
    retire_oldest(&mut engine, 1);
    let (lazy_id, lazy) = routing_root(&engine);
    assert_eq!(lazy.page_ids().collect::<Vec<_>>(), pages,
        "retiring a source must leave the routing pages of the retained sources untouched");
    // Engines before lazy retirement read a retired location as corruption;
    // the root's version makes them refuse it at open instead.
    assert_eq!(root_version(&root), 1);
    assert_eq!(root_version(&lazy), 2);
    assert_ne!(lazy_id, root_id);
    retire_oldest(&mut engine, 1);
    assert_eq!(routing_root(&engine).0, lazy_id, "a root already marked is republished as is");
    assert_routed_payloads_read(&engine, 0..SOURCES, PAYLOADS, "after the slices");
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(routing_root(&reopened).0, lazy_id);
    assert_routed_payloads_read(&reopened, 0..SOURCES, PAYLOADS, "after reopen");
}

fn root_version(root: &RoutingRoot) -> u32 {
    u32::from_le_bytes(root.encode().unwrap()[8..12].try_into().unwrap())
}

/// Retired locations leave routing when a merge rewrites their run anyway:
/// the cascade of later rotations drops every location whose source the new
/// manifest no longer names, so routing stays bounded by retained history.
#[test]
fn routing_merges_drop_locations_of_retired_sources() {
    // 5,000 locations per source: rotations 1-4 cascade into one level-2 run
    // of 20,000; rotation 8 merges the next 20,000 into it.
    const PAYLOADS: usize = 5_000;
    let (_vfs, mut engine) = routed_history(4, PAYLOADS);
    retire_oldest(&mut engine, 2);
    for source in 4..8 {
        commit_routed_source(&mut engine, source, PAYLOADS);
    }
    let (_, root) = routing_root(&engine);
    assert_eq!(routing_entries(&root), (6 * PAYLOADS) as u64,
        "the merge that rewrote the retired sources' run must drop their locations");
    assert_routed_payloads_read(&engine, 0..8, PAYLOADS, "after the cascade");
}

/// What an engine before lazy retirement needs from routing: it opens only
/// version 1, and it reads a location whose source its manifest's indexed
/// segments do not name as corruption ("history payload source is outside
/// the published manifest").
fn assert_readable_by_version_1_engines(engine: &Engine, context: &str) {
    let (_, root) = routing_root(engine);
    assert_eq!(root_version(&root), 1, "{context}: routing root version");
    let indexed: std::collections::BTreeSet<BlockId> = engine.history_manifest.as_ref().unwrap().segments.iter()
        .filter(|reference| reference.payload_index_block_id.is_some())
        .map(|reference| reference.block_id)
        .collect();
    let named = root.location_sources(&engine.blocks).unwrap();
    assert!(named.is_subset(&indexed), "{context}: a location names a source outside the manifest");
}

/// The downgrade converter (`tofu-db rewrite-history-routing`, docs/STORAGE_OPERATIONS.md
/// "Engine rollback"): a lazily retired root becomes a version-1 root of the
/// retained locations only, nothing is retired, and every read answers as before.
#[test]
fn routing_rewrite_publishes_a_root_engines_before_lazy_retirement_open() {
    const SOURCES: usize = 4;
    const PAYLOADS: usize = 14_000;
    let (vfs, mut engine) = routed_history(SOURCES, PAYLOADS);
    retire_oldest(&mut engine, 1);
    let (lazy_id, lazy) = routing_root(&engine);
    assert_eq!(root_version(&lazy), 2);
    assert_eq!(lazy.location_sources(&engine.blocks).unwrap().len(), SOURCES,
        "the lazy root still holds the retired source's locations");
    let segments = engine.history_manifest.as_ref().unwrap().segments.clone();
    assert_eq!(engine.history_routing().unwrap().unwrap().version, 2);

    assert!(engine.rewrite_history_routing().unwrap());
    assert_eq!(engine.history_manifest.as_ref().unwrap().segments, segments, "the rewrite retires nothing");
    let (id, root) = routing_root(&engine);
    assert_ne!(id, lazy_id);
    assert_readable_by_version_1_engines(&engine, "after the rewrite");
    assert_eq!(routing_entries(&root), ((SOURCES - 1) * PAYLOADS) as u64);
    let routing = engine.history_routing().unwrap().unwrap();
    assert_eq!((routing.version, routing.entries, routing.retained_sources), (1, ((SOURCES - 1) * PAYLOADS) as u64, SOURCES - 1));
    assert_routed_payloads_read(&engine, 0..SOURCES, PAYLOADS, "after the rewrite");
    assert!(!engine.rewrite_history_routing().unwrap(), "a version-1 root is left as is");
    assert_eq!(routing_root(&engine).0, id);

    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(routing_root(&reopened).0, id);
    assert_readable_by_version_1_engines(&reopened, "after reopen");
    assert_routed_payloads_read(&reopened, 0..SOURCES, PAYLOADS, "after reopen");
}

/// Every I/O error, short write and dropped sync of the rewrite leaves a
/// store that reopens with the lazy root or the rewritten one, never another,
/// and every payload, retired source included, still reads.
#[test]
fn every_routing_rewrite_fault_recovers_the_lazy_or_the_rewritten_root() {
    const SOURCES: usize = 3;
    const PAYLOADS: usize = 2_000;
    let (seed, mut engine) = routed_history(SOURCES, PAYLOADS);
    retire_oldest(&mut engine, 1);
    let (lazy_id, _) = routing_root(&engine);
    std::mem::forget(engine);
    seed.crash().unwrap();
    let baseline = Arc::new(seed.fork_durable().unwrap());
    let mut engine = Engine::open_with_vfs(Path::new("/data"), baseline.clone()).unwrap();
    baseline.arm_fault(None).unwrap();
    assert!(engine.rewrite_history_routing().unwrap());
    let trace = baseline.trace().unwrap();
    let rewritten_id = routing_root(&engine).0;
    assert!(trace.contains(&Operation::Write));
    assert!(trace.iter().any(is_sync));
    let mut cases = 0;
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        if is_sync(operation) { actions.push(FaultAction::DropSync); }
        for action in actions {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let _ = engine.rewrite_history_routing();
            assert!(vfs.trace().unwrap().len() > index, "fault {index} {operation:?} was not reached");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let id = routing_root(&recovered).0;
            assert!(id == lazy_id || id == rewritten_id, "fault {index} {action:?} published another root");
            if id == rewritten_id {
                assert_readable_by_version_1_engines(&recovered, &format!("fault {index} {action:?}"));
            }
            assert_routed_payloads_read(&recovered, 0..SOURCES, PAYLOADS, &format!("fault {index} {action:?}"));
            cases += 1;
        }
    }
    assert!(cases > 8, "the fault matrix must exercise real cases, got {cases}");
}
