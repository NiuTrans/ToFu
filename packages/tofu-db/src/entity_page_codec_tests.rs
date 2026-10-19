//! Executable format and integration specifications for compact entity pages.
use super::*;
use crate::block::BlockId;
use crate::entity::{
    DecodedPageCache, EntityDatabase, EntityKey, RootNode, TombstoneReclaim, Tree,
};
use std::sync::Arc;

fn legacy_encode(page: &Page) -> Vec<u8> {
    let mut bytes = PAGE_MAGIC.to_vec();
    bytes.extend_from_slice(&1u32.to_le_bytes());
    match page {
        Page::Leaf(entries) => {
            bytes.extend_from_slice(&[0, 0]);
            push_u16(&mut bytes, entries.len()).unwrap();
            for (key, entity) in entries {
                push_u16(&mut bytes, key.len()).unwrap();
                push_u16(&mut bytes, entity.value.as_ref().map_or(0, Vec::len)).unwrap();
                bytes.extend_from_slice(&entity.version.to_le_bytes());
                bytes.push(u8::from(entity.value.is_none()));
                bytes.extend_from_slice(key);
                if let Some(value) = &entity.value {
                    bytes.extend_from_slice(value);
                }
            }
        }
        Page::Internal { level, children } => {
            bytes.extend_from_slice(&[1, *level]);
            push_u16(&mut bytes, children.len()).unwrap();
            for child in children {
                push_u16(&mut bytes, child.lower_bound.len()).unwrap();
                bytes.extend_from_slice(&child.lower_bound);
                bytes.extend_from_slice(&child.block_id.0);
            }
        }
    }
    bytes
}

fn leaf() -> Page {
    Page::Leaf(
        (0..40u16)
            .map(|index| {
                let key = EntityKey::new(7, 11, "codec_binary", &index.to_be_bytes())
                    .unwrap()
                    .0;
                (
                    key,
                    EntityValue {
                        version: 42 + u64::from(index),
                        value: (index % 3 != 0).then(|| vec![index as u8; 19]),
                    },
                )
            })
            .collect(),
    )
}

#[test]
fn legacy_and_compact_leaf_internal_pages_keep_exact_keys_values_and_versions() {
    let leaf = leaf();
    let children = match &leaf {
        Page::Leaf(entries) => entries
            .iter()
            .map(|(key, _)| Child {
                lower_bound: key.clone(),
                block_id: BlockId::for_payload(key),
            })
            .collect(),
        _ => unreachable!(),
    };
    for page in [leaf, Page::Internal { level: 1, children }] {
        let old = legacy_encode(&page);
        let new = page.encode().unwrap();
        assert!(new.len() < old.len());
        assert_eq!(page.encoded_bytes().unwrap(), new.len());
        assert_eq!(Page::decode(&old).unwrap(), page);
        assert_eq!(Page::decode(&new).unwrap(), page);
        for length in 0..new.len() {
            assert!(
                Page::decode(&new[..length]).is_err(),
                "truncation at {length}"
            );
        }
    }
}

#[test]
fn invalid_prefix_counts_versions_and_order_fail_closed_before_unbounded_allocation() {
    let bytes = leaf().encode().unwrap();
    for (range, replacement) in [
        (8..12, 3u32.to_le_bytes().to_vec()),
        (14..16, u16::MAX.to_le_bytes().to_vec()),
        (16..18, 1u16.to_le_bytes().to_vec()),
        (18..20, u16::MAX.to_le_bytes().to_vec()),
    ] {
        let mut corrupt = bytes.clone();
        corrupt[range].copy_from_slice(&replacement);
        assert!(Page::decode(&corrupt).is_err());
    }
    let mut trailing = bytes.clone();
    trailing.push(0);
    assert!(Page::decode(&trailing).is_err());
    // First leaf entry is a tombstone: a nonzero declared value cannot be hidden.
    let mut tombstone = bytes.clone();
    tombstone[20..22].copy_from_slice(&1u16.to_le_bytes());
    assert!(Page::decode(&tombstone).is_err());
    let mut unsorted = match leaf() {
        Page::Leaf(entries) => entries,
        _ => unreachable!(),
    };
    unsorted.swap(0, 1);
    assert!(Page::Leaf(unsorted).encode().is_err());
}

#[test]
fn compact_expansion_is_bounded_and_cache_charges_owned_keys() {
    let mut frame = LeafFrame::new();
    let mut index = 0u16;
    loop {
        let mut key = vec![b'x'; 700];
        key.extend_from_slice(&index.to_be_bytes());
        if frame.would_overflow(&key, Some(&[])) {
            break;
        }
        frame.push(&key, u64::from(index), Some(&[])).unwrap();
        index += 1;
    }
    assert!(index > 10);
    let (_, bytes) = frame.finish().unwrap();
    assert!(bytes.len() < PAGE_PACK_TARGET_BYTES);
    let node = Arc::new(RootNode::Page(Page::decode(&bytes).unwrap()));
    let owned = node.owned_allocation_bytes();
    assert!(
        owned > bytes.len() * 4,
        "prefix expansion must exceed the old cache estimate"
    );
    let mut cache = DecodedPageCache::new(owned + 256);
    cache.insert(BlockId::for_payload(&bytes), Arc::clone(&node), bytes.len());
    assert_eq!(cache.resident_bytes, owned + 256);
    cache.set_capacity_bytes(0);
    assert!(cache.entries.is_empty());
    assert_eq!(cache.resident_bytes, 0);
    // A physically tiny forged page may not expand past the independent ceiling.
    let Page::Leaf(mut entries) = Page::decode(&bytes).unwrap() else {
        unreachable!()
    };
    let mut key = vec![b'x'; 700];
    key.extend_from_slice(&index.to_be_bytes());
    entries.push((
        key,
        EntityValue {
            version: 99,
            value: Some(vec![]),
        },
    ));
    assert!(Page::Leaf(entries.clone()).encode().is_err());
    // Construct the unchecked compact frame to exercise the reader independently.
    let (_, entity) = entries.last().unwrap();
    let mut forged = bytes;
    let previous = &entries[entries.len() - 2].0;
    let key = &entries.last().unwrap().0;
    let prefix = write_key_lengths(&mut forged, previous, key).unwrap();
    forged.extend_from_slice(&0u16.to_le_bytes());
    forged.extend_from_slice(&entity.version.to_le_bytes());
    forged.push(0);
    forged.extend_from_slice(&key[prefix..]);
    forged[14..16].copy_from_slice(&(entries.len() as u16).to_le_bytes());
    assert!(Page::decode(&forged).is_err());
}

#[test]
fn tree_reads_mixed_formats_and_rewrites_only_modified_branches() {
    let directory = tempfile::tempdir().unwrap();
    let database = EntityDatabase::initialize(directory.path()).unwrap();
    let tree = Tree {
        backend: database.backend(),
        page_cache: None,
    };
    let Page::Leaf(entries) = leaf() else {
        unreachable!()
    };
    let left = Page::Leaf(entries[..20].to_vec());
    let right = Page::Leaf(entries[20..].to_vec());
    let left_id = tree.backend.write_fresh(&legacy_encode(&left)).unwrap();
    let right_id = tree.backend.write_fresh(&legacy_encode(&right)).unwrap();
    let root = Page::Internal {
        level: 1,
        children: vec![
            Child {
                lower_bound: entries[0].0.clone(),
                block_id: left_id,
            },
            Child {
                lower_bound: entries[20].0.clone(),
                block_id: right_id,
            },
        ],
    };
    let legacy_root = tree.backend.write_fresh(&legacy_encode(&root)).unwrap();
    let (new_root, written) = tree
        .apply_batch(
            Some(legacy_root),
            vec![(entries[2].0.clone(), Some(b"updated".to_vec()), 123)],
            TombstoneReclaim {
                below: 0,
                mounts: &[],
            },
        )
        .unwrap();
    assert!(!written.contains(&right_id));
    assert_eq!(
        tree.get(Some(legacy_root), &entries[2].0)
            .unwrap()
            .unwrap()
            .version,
        44
    );
    assert_eq!(
        tree.get(new_root, &entries[2].0).unwrap().unwrap().value,
        Some(b"updated".to_vec())
    );
    for (key, expected) in &entries[20..] {
        assert_eq!(tree.get(new_root, key).unwrap(), Some(expected.clone()));
    }
    let Page::Internal { children, .. } = tree.load(new_root.unwrap()).unwrap() else {
        unreachable!()
    };
    assert_eq!(children.last().unwrap().block_id, right_id);
    for id in written {
        let encoded = tree.backend.read_verified(id).unwrap();
        assert_eq!(u32::from_le_bytes(encoded[8..12].try_into().unwrap()), 2);
        assert!(Page::decode(&encoded).is_ok());
    }
}

#[test]
fn legacy_authority_upgrades_lazily_and_keeps_snapshots_after_power_loss() {
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database =
        EntityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let Page::Leaf(entries) = leaf() else {
        unreachable!()
    };
    let tree = Tree {
        backend: database.backend(),
        page_cache: None,
    };
    let left = tree
        .backend
        .write_fresh(&legacy_encode(&Page::Leaf(entries[..20].to_vec())))
        .unwrap();
    let right = tree
        .backend
        .write_fresh(&legacy_encode(&Page::Leaf(entries[20..].to_vec())))
        .unwrap();
    let root = tree
        .backend
        .write_fresh(&legacy_encode(&Page::Internal {
            level: 1,
            children: vec![
                Child {
                    lower_bound: entries[0].0.clone(),
                    block_id: left,
                },
                Child {
                    lower_bound: entries[20].0.clone(),
                    block_id: right,
                },
            ],
        }))
        .unwrap();
    let engine = database.writer_engine_mut().unwrap();
    let record = crate::entity::encode_root(engine.next_sequence().unwrap(), Some(root));
    engine
        .commit_references_with_authority_state(&record, &[left, right, root], Some(root))
        .unwrap();
    drop(database);
    vfs.crash().unwrap();
    let mut database = EntityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let mut old = database.begin(7, 11).unwrap();
    let target = EntityKey::new(7, 11, "codec_binary", &2u16.to_be_bytes()).unwrap();
    assert_eq!(database.get(&mut old, &target).unwrap(), entries[2].1.value);
    let mut write = database.begin(7, 11).unwrap();
    database
        .put(&mut write, target.clone(), b"new compact value".to_vec())
        .unwrap();
    database.commit(write).unwrap();
    database.merge_overlay_fully().unwrap();
    database.writer_engine_mut().unwrap().checkpoint().unwrap();
    assert_eq!(database.get(&mut old, &target).unwrap(), entries[2].1.value);
    drop(old);
    drop(database);
    vfs.crash().unwrap();
    let database = EntityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
    let mut read = database.begin(7, 11).unwrap();
    assert_eq!(
        database.get(&mut read, &target).unwrap(),
        Some(b"new compact value".to_vec())
    );
    let unchanged = EntityKey::new(7, 11, "codec_binary", &22u16.to_be_bytes()).unwrap();
    assert_eq!(
        database.get(&mut read, &unchanged).unwrap(),
        entries[22].1.value
    );
}
