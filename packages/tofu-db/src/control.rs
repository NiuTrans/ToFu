//! Alternating, self-checking CONTROL slots.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::BlockId;
use crate::vfs::{sync_all_barrier, OpenRequest, RealVfs, Vfs, VfsFile};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 8] = b"TOFUDB01";
const LEGACY_CONTROL_VERSION: u32 = FORMAT_VERSION;
const CONTROL_VERSION: u32 = 2;
const SLOT_BYTES: usize = 4096;
const SLOT_COUNT: usize = 2;
const CHECKSUM_BYTES: usize = 32;
const CHECKSUM_OFFSET: usize = SLOT_BYTES - CHECKSUM_BYTES;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControlState {
    pub generation: u64,
    pub durable_sequence: u64,
    pub authority_uuid: Uuid,
    pub root_hash: [u8; 32],
    pub checkpoint_sequence: u64,
    pub checkpoint_hash: [u8; 32],
    pub history_manifest_block_id: Option<BlockId>,
    /// `None` identifies a legacy slot without a state-root witness;
    /// `Some(None)` is a known empty authority and `Some(Some(root))` is the
    /// exact current immutable authority root.
    pub authority_state_root: Option<Option<BlockId>>,
    /// Content-addressed catalog for immutable payload segments. Zeroed bytes
    /// in legacy slots decode as no catalog, leaving loose blocks authoritative.
    pub payload_manifest_block_id: Option<BlockId>,
    pub active_log_generation: u64,
}

pub struct ControlFile {
    path: PathBuf,
    file: Box<dyn VfsFile>,
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn encode(state: &ControlState) -> [u8; SLOT_BYTES] {
    let mut slot = [0_u8; SLOT_BYTES];
    slot[0..8].copy_from_slice(MAGIC);
    slot[8..12].copy_from_slice(&CONTROL_VERSION.to_le_bytes());
    slot[12..20].copy_from_slice(&state.generation.to_le_bytes());
    slot[20..28].copy_from_slice(&state.durable_sequence.to_le_bytes());
    slot[28..44].copy_from_slice(state.authority_uuid.as_bytes());
    slot[44..76].copy_from_slice(&state.root_hash);
    slot[76..84].copy_from_slice(&state.checkpoint_sequence.to_le_bytes());
    slot[84..116].copy_from_slice(&state.checkpoint_hash);
    slot[116..124].copy_from_slice(&state.active_log_generation.to_le_bytes());
    slot[124] = u8::from(state.history_manifest_block_id.is_some());
    if let Some(block_id) = state.history_manifest_block_id {
        slot[125..157].copy_from_slice(&block_id.0);
    }
    match state.authority_state_root {
        None => {}
        Some(None) => slot[157] = 1,
        Some(Some(root)) => {
            slot[157] = 2;
            slot[158..190].copy_from_slice(&root.0);
        }
    }
    slot[190] = u8::from(state.payload_manifest_block_id.is_some());
    if let Some(block_id) = state.payload_manifest_block_id {
        slot[191..223].copy_from_slice(&block_id.0);
    }
    let checksum = blake3::hash(&slot[..CHECKSUM_OFFSET]);
    slot[CHECKSUM_OFFSET..].copy_from_slice(checksum.as_bytes());
    slot
}

fn decode(slot: &[u8; SLOT_BYTES]) -> io::Result<Option<ControlState>> {
    if slot.iter().all(|byte| *byte == 0) {
        return Ok(None);
    }
    if &slot[0..8] != MAGIC {
        return Ok(None);
    }
    let expected = blake3::hash(&slot[..CHECKSUM_OFFSET]);
    if expected.as_bytes() != &slot[CHECKSUM_OFFSET..] {
        return Ok(None);
    }
    let version = u32::from_le_bytes(slot[8..12].try_into().unwrap());
    if version != LEGACY_CONTROL_VERSION && version != CONTROL_VERSION {
        return Err(invalid_data("unsupported CONTROL format version"));
    }
    let generation = u64::from_le_bytes(slot[12..20].try_into().unwrap());
    if generation == 0 {
        return Ok(None);
    }
    let durable_sequence = u64::from_le_bytes(slot[20..28].try_into().unwrap());
    let authority_uuid = Uuid::from_bytes(slot[28..44].try_into().unwrap());
    let root_hash = slot[44..76].try_into().unwrap();
    let checkpoint_sequence = u64::from_le_bytes(slot[76..84].try_into().unwrap());
    let checkpoint_hash = slot[84..116].try_into().unwrap();
    let encoded_active_generation = u64::from_le_bytes(slot[116..124].try_into().unwrap());
    let manifest_presence = slot[124];
    let history_manifest_block_id = match manifest_presence {
        0 => None,
        1 => Some(BlockId(slot[125..157].try_into().unwrap())),
        _ => return Err(invalid_data("invalid CONTROL history manifest presence")),
    };
    let encoded_authority_root = BlockId(slot[158..190].try_into().unwrap());
    let authority_state_root = match slot[157] {
        0 if encoded_authority_root == BlockId([0; 32]) => None,
        1 if encoded_authority_root == BlockId([0; 32]) => Some(None),
        2 => Some(Some(encoded_authority_root)),
        _ => return Err(invalid_data("invalid CONTROL authority root discriminant")),
    };
    let encoded_payload_manifest = BlockId(slot[191..223].try_into().unwrap());
    let payload_manifest_block_id = match (version, slot[190]) {
        (LEGACY_CONTROL_VERSION, 0) if encoded_payload_manifest == BlockId([0; 32]) => None,
        (CONTROL_VERSION, 0) if encoded_payload_manifest == BlockId([0; 32]) => None,
        (CONTROL_VERSION, 1) => Some(encoded_payload_manifest),
        _ => return Err(invalid_data("invalid CONTROL payload manifest presence")),
    };
    let active_log_generation = if encoded_active_generation == 0
        && checkpoint_sequence == 0
        && checkpoint_hash == [0; 32]
        && history_manifest_block_id.is_none()
    {
        1
    } else {
        encoded_active_generation
    };
    let state = ControlState {
        generation,
        durable_sequence,
        authority_uuid,
        root_hash,
        checkpoint_sequence,
        checkpoint_hash,
        history_manifest_block_id,
        authority_state_root,
        payload_manifest_block_id,
        active_log_generation,
    };
    validate_state(&state)?;
    Ok(Some(state))
}

fn validate_state(state: &ControlState) -> io::Result<()> {
    if state.active_log_generation == 0 || state.checkpoint_sequence > state.durable_sequence {
        return Err(invalid_data(
            "invalid CONTROL checkpoint or active generation",
        ));
    }
    if state.durable_sequence == 0 && matches!(state.authority_state_root, Some(Some(_))) {
        return Err(invalid_data("empty CONTROL has a nonempty authority root"));
    }
    if state.checkpoint_sequence == 0 {
        if state.checkpoint_hash != [0; 32] || state.history_manifest_block_id.is_some() {
            return Err(invalid_data("empty CONTROL checkpoint has history state"));
        }
    } else if state.history_manifest_block_id.is_none() {
        return Err(invalid_data(
            "CONTROL checkpoint is missing its history manifest",
        ));
    }
    Ok(())
}

impl ControlFile {
    pub fn initialize(data_dir: &Path, authority_uuid: Uuid) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, authority_uuid, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        authority_uuid: Uuid,
        vfs: Arc<dyn Vfs>,
    ) -> io::Result<Self> {
        Self::initialize_state_with_vfs(
            data_dir,
            ControlState {
                generation: 1,
                durable_sequence: 0,
                authority_uuid,
                root_hash: [0; 32],
                checkpoint_sequence: 0,
                checkpoint_hash: [0; 32],
                history_manifest_block_id: None,
                authority_state_root: Some(None),
                payload_manifest_block_id: None,
                active_log_generation: 1,
            },
            vfs,
        )
    }

    pub(crate) fn initialize_state_with_vfs(
        data_dir: &Path,
        initial: ControlState,
        vfs: Arc<dyn Vfs>,
    ) -> io::Result<Self> {
        if initial.generation == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "initial CONTROL generation must be positive",
            ));
        }
        validate_state(&initial)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
        let path = data_dir.join("CONTROL");
        let mut file = vfs.open(
            &path,
            OpenRequest {
                read: true,
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.set_len((SLOT_BYTES * SLOT_COUNT) as u64)?;
        file.write_all_at(0, &encode(&initial))?;
        sync_all_barrier(file.as_mut())?;
        Ok(Self { path, file })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let path = data_dir.join("CONTROL");
        let file = vfs.open(
            &path,
            OpenRequest {
                read: true,
                write: true,
                ..OpenRequest::default()
            },
        )?;
        if file.len()? != (SLOT_BYTES * SLOT_COUNT) as u64 {
            return Err(invalid_data("CONTROL has an invalid fixed size"));
        }
        Ok(Self { path, file })
    }

    pub fn read_current(&mut self) -> io::Result<ControlState> {
        let bytes = self.file.read_all(SLOT_BYTES * SLOT_COUNT)?;
        if bytes.len() != SLOT_BYTES * SLOT_COUNT {
            return Err(invalid_data("CONTROL has an invalid fixed size"));
        }
        let mut states = Vec::with_capacity(SLOT_COUNT);
        for index in 0..SLOT_COUNT {
            let slot: [u8; SLOT_BYTES] = bytes[index * SLOT_BYTES..(index + 1) * SLOT_BYTES]
                .try_into()
                .unwrap();
            if let Some(state) = decode(&slot)? {
                states.push(state);
            }
        }
        states.sort_by_key(|state| state.generation);
        let newest = states
            .pop()
            .ok_or_else(|| invalid_data("no valid CONTROL slot"))?;
        if states
            .last()
            .is_some_and(|old| old.generation == newest.generation && old != &newest)
        {
            return Err(invalid_data("conflicting CONTROL slots at one generation"));
        }
        Ok(newest)
    }

    pub fn publish(&mut self, next: &ControlState) -> io::Result<()> {
        let current = self.read_current()?;
        if next.generation
            != current
                .generation
                .checked_add(1)
                .ok_or_else(|| invalid_data("CONTROL generation overflow"))?
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "CONTROL publication must advance exactly one generation",
            ));
        }
        if next.authority_uuid != current.authority_uuid
            || next.durable_sequence < current.durable_sequence
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "CONTROL authority changed or durable sequence regressed",
            ));
        }
        validate_state(next)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
        if next.active_log_generation == current.active_log_generation {
            let stable_authority_republication = next.durable_sequence == current.durable_sequence
                && next.root_hash == current.root_hash
                && next.authority_state_root == current.authority_state_root
                && next.checkpoint_sequence == current.checkpoint_sequence
                && next.checkpoint_hash == current.checkpoint_hash;
            let history_change_allowed = next.history_manifest_block_id
                == current.history_manifest_block_id
                || (stable_authority_republication
                    && next.history_manifest_block_id.is_some()
                    && current.history_manifest_block_id.is_some());
            let payload_change_allowed = next.payload_manifest_block_id
                == current.payload_manifest_block_id
                || (stable_authority_republication && next.payload_manifest_block_id.is_some());
            if (next.history_manifest_block_id != current.history_manifest_block_id
                && !history_change_allowed)
                || (next.payload_manifest_block_id != current.payload_manifest_block_id
                    && !payload_change_allowed)
                || next.checkpoint_sequence != current.checkpoint_sequence
                || next.checkpoint_hash != current.checkpoint_hash
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "ordinary CONTROL publication changed checkpoint state",
                ));
            }
        } else {
            let expected_generation = current
                .active_log_generation
                .checked_add(1)
                .ok_or_else(|| invalid_data("active WAL generation overflow"))?;
            if next.active_log_generation != expected_generation
                || next.durable_sequence != current.durable_sequence
                || next.root_hash != current.root_hash
                || next.checkpoint_sequence != current.durable_sequence
                || next.checkpoint_hash != current.root_hash
                || next.history_manifest_block_id.is_none()
                || next.payload_manifest_block_id != current.payload_manifest_block_id
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "CONTROL rotation does not checkpoint the complete durable prefix",
                ));
            }
        }
        let slot_index = ((next.generation - 1) as usize) % SLOT_COUNT;
        self.file
            .write_all_at((slot_index * SLOT_BYTES) as u64, &encode(next))?;
        sync_all_barrier(self.file.as_mut())
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    #[test]
    fn legacy_control_slot_decodes_with_an_unknown_authority_root() {
        let state = ControlState {
            generation: 2,
            durable_sequence: 1,
            authority_uuid: Uuid::from_u128(9),
            root_hash: [7; 32],
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            authority_state_root: Some(Some(BlockId([8; 32]))),
            payload_manifest_block_id: None,
            active_log_generation: 1,
        };
        let mut legacy = encode(&state);
        legacy[8..12].copy_from_slice(&LEGACY_CONTROL_VERSION.to_le_bytes());
        legacy[157..190].fill(0);
        legacy[190..223].fill(0);
        let checksum = blake3::hash(&legacy[..CHECKSUM_OFFSET]);
        legacy[CHECKSUM_OFFSET..].copy_from_slice(checksum.as_bytes());
        let decoded = decode(&legacy).unwrap().unwrap();
        assert_eq!(decoded.authority_state_root, None);
        assert_eq!(decoded.payload_manifest_block_id, None);
    }

    #[test]
    fn corrupt_new_slot_falls_back_to_previous_generation() {
        let directory = tempfile::tempdir().unwrap();
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize(directory.path(), authority).unwrap();
        control
            .publish(&ControlState {
                generation: 2,
                durable_sequence: 1,
                authority_uuid: authority,
                root_hash: [7; 32],
                checkpoint_sequence: 0,
                checkpoint_hash: [0; 32],
                history_manifest_block_id: None,
                authority_state_root: Some(None),
                payload_manifest_block_id: None,
                active_log_generation: 1,
            })
            .unwrap();
        control
            .file
            .write_all_at(SLOT_BYTES as u64 + 100, &[0xff])
            .unwrap();
        control.file.sync_all().unwrap();
        let state = control.read_current().unwrap();
        assert_eq!(state.generation, 1);
        assert_eq!(state.durable_sequence, 0);
    }

    fn simulated_control(vfs: &DeterministicVfs, authority: Uuid) -> ControlFile {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let control =
            ControlFile::initialize_with_vfs(Path::new("/data"), authority, Arc::new(vfs.clone()))
                .unwrap();
        vfs.sync_directory(Path::new("/data")).unwrap();
        control
    }

    #[test]
    fn every_control_publish_fault_selects_one_complete_generation() -> io::Result<()> {
        let authority = Uuid::from_u128(7);
        let next = ControlState {
            generation: 2,
            durable_sequence: 1,
            authority_uuid: authority,
            root_hash: [9; 32],
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            authority_state_root: Some(Some(BlockId([8; 32]))),
            payload_manifest_block_id: None,
            active_log_generation: 1,
        };
        let baseline_vfs = DeterministicVfs::new(None);
        let mut baseline = simulated_control(&baseline_vfs, authority);
        baseline_vfs.arm_fault(None)?;
        baseline.publish(&next)?;
        let trace = baseline_vfs.trace()?;
        assert_eq!(
            trace,
            vec![
                Operation::Read,
                Operation::Write,
                Operation::SyncAll,
                Operation::SyncAll
            ]
        );

        for operation_number in 1..=trace.len() as u64 {
            let vfs = DeterministicVfs::new(None);
            let mut control = simulated_control(&vfs, authority);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))?;
            let _ = control.publish(&next);
            vfs.crash()?;
            let mut recovered =
                ControlFile::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            let state = recovered.read_current()?;
            assert!(state.generation == 1 || state == next);
        }

        for (index, operation) in trace.iter().enumerate() {
            let action = match operation {
                Operation::Write => Some(FaultAction::ShortWrite(37)),
                Operation::SyncAll => Some(FaultAction::DropSync),
                _ => None,
            };
            let Some(action) = action else {
                continue;
            };
            let vfs = DeterministicVfs::new(None);
            let mut control = simulated_control(&vfs, authority);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))?;
            let _ = control.publish(&next);
            vfs.crash()?;
            let mut recovered =
                ControlFile::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            let generation = recovered.read_current()?.generation;
            if matches!(action, FaultAction::DropSync) {
                assert_eq!(generation, 2);
            } else {
                assert_eq!(generation, 1);
            }
        }
        Ok(())
    }

    #[test]
    fn rotation_must_checkpoint_the_complete_durable_prefix() {
        let directory = tempfile::tempdir().unwrap();
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize(directory.path(), authority).unwrap();
        let committed = ControlState {
            generation: 2,
            durable_sequence: 1,
            authority_uuid: authority,
            root_hash: [9; 32],
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            authority_state_root: Some(Some(BlockId([8; 32]))),
            payload_manifest_block_id: None,
            active_log_generation: 1,
        };
        control.publish(&committed).unwrap();
        let rotated = ControlState {
            generation: 3,
            durable_sequence: 1,
            authority_uuid: authority,
            root_hash: [9; 32],
            checkpoint_sequence: 1,
            checkpoint_hash: [9; 32],
            history_manifest_block_id: Some(BlockId([3; 32])),
            authority_state_root: Some(Some(BlockId([8; 32]))),
            payload_manifest_block_id: None,
            active_log_generation: 2,
        };
        control.publish(&rotated).unwrap();
        assert_eq!(control.read_current().unwrap(), rotated);

        let invalid = ControlState {
            generation: 4,
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            ..rotated
        };
        assert_eq!(
            control.publish(&invalid).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn payload_manifest_changes_only_as_stable_forward_maintenance() {
        let directory = tempfile::tempdir().unwrap();
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize(directory.path(), authority).unwrap();
        let initial = control.read_current().unwrap();
        let manifest_id = BlockId([6; 32]);
        let published = ControlState {
            generation: 2,
            payload_manifest_block_id: Some(manifest_id),
            ..initial
        };
        control.publish(&published).unwrap();
        assert_eq!(
            control.read_current().unwrap().payload_manifest_block_id,
            Some(manifest_id)
        );

        let removal = ControlState {
            generation: 3,
            payload_manifest_block_id: None,
            ..published.clone()
        };
        assert_eq!(
            control.publish(&removal).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let coupled_commit = ControlState {
            generation: 3,
            durable_sequence: 1,
            root_hash: [5; 32],
            payload_manifest_block_id: Some(BlockId([7; 32])),
            ..published
        };
        assert_eq!(
            control.publish(&coupled_commit).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }
}
