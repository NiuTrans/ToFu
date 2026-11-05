"""Validate storage geometry, maintenance, cache and filesystem contracts.
Entry: validate_engine(document, catalog_names). The operation catalog owns the
executable-operation count; these existing checks do not change admission policy."""

def validate_engine(document: dict, catalog_names: list[str]) -> None:
    bounds = document.get("bounds")
    turn_read = document.get('turnProjectionRead')
    if (not isinstance(turn_read, dict)
            or turn_read.get('operation') != 'turn.get'
            or turn_read.get('flag') != 'include_projection'
            or turn_read.get('default') is not True
            or any(not isinstance(turn_read.get(key), str) or not turn_read[key]
                   for key in ('authorization', 'delivery', 'metadata', 'failure'))):
        raise ValueError('Turn projection-read authority differs')
    if document.get("attemptExecutionConfig") != {
        "operation":"turn.attempt.get","flag":"include_execution_config","default":False,
        "authorization":"existing owner locator/live parent/turn proof applies before private config is returned",
        "delivery":"internal lifecycle port only; public endpoints do not forward the flag",
    }:
        raise ValueError("attempt execution config authority differs")
    expected_bounds = {
        "stepAdmission": "complete-ordered-transaction-without-step-count-quota",
        "slotAdmission": "sparse-usize-identifiers-without-slot-count-quota",
        "literalAdmission": "checked-size-without-aggregate-byte-quota",
        "maximumLegacyDocumentBytes": 8 * 1024 * 1024,
        "maximumExecutableOperations": len(catalog_names),
    }
    if bounds != expected_bounds:
        raise ValueError("Tofu-DB IR bounds differ from the certified v1 bounds")
    expected_physical_bounds = {
        "maximumEntityKeyBytes": 3072,
        "maximumEntityInlineValueBytes": 8 * 1024,
        "maximumEntityRangeRows": 1000,
        "entityOverlayDeltaCountEncoding": "u32-little-endian",
        "versionedDocumentAdmission": "blob-backed-without-private-byte-quota",
        "indexedEventPayloadAdmission": "blob-backed-complete-replay-without-private-byte-quota",
        "maximumEntityReachabilityPages": 20_000_000,
        "maximumEntityReachabilityFrontier": 8192,
        "maximumEntityRetiredRanges": 64,
        "maximumEntityRootRangeMounts": 64,
        "maximumEntityMountConsolidationRows": 999,
        "maximumEntityMountConsolidationBytes": 8 * 1024 * 1024,
        "maximumStreamEventBytes": 256 * 1024,
        "streamAppendEventCountEncoding": "u32-little-endian",
        "streamAppendAdmission": "complete-atomic-segmented-append-without-byte-quota",
        "maximumStreamReadEvents": 1000,
        "maximumStreamReadBytes": 8 * 1024 * 1024,
        "transactionStreamAdmission": "complete-batch-without-stream-count-quota",
    }
    if document.get("physicalBounds") != expected_physical_bounds:
        raise ValueError("Tofu-DB physical bounds differ from the certified v1 bounds")
    if (
        expected_physical_bounds["maximumEntityMountConsolidationRows"] + 1
        > expected_physical_bounds["maximumEntityRangeRows"]
    ):
        raise ValueError("entity mount consolidation bounds are inconsistent")
    if document.get("physicalFamilies") != ["entity", "stream", "blob"]:
        raise ValueError("Tofu-DB IR physical families are incomplete")
    expected_persistent_snapshots = {
        "rootPinNamespace": "entity_root_pins",
        "catalogNamespace": "entity_root_pin_catalog",
        "maximumPinsPerAuthority": 1_000_000,
        "maximumLegacyPinsWithoutCatalog": 64,
        "maximumPinIdBytes": 128,
        "maximumRangesPerCapsule": 64,
    }
    if document.get("persistentSnapshots") != expected_persistent_snapshots:
        raise ValueError("Tofu-DB persistent snapshot bounds are incomplete")
    if (
        expected_persistent_snapshots["rootPinNamespace"]
        == expected_persistent_snapshots["catalogNamespace"]
        or len(expected_persistent_snapshots["catalogNamespace"].encode("utf-8")) > 63
        or expected_persistent_snapshots["maximumPinsPerAuthority"]
        > expected_physical_bounds["maximumEntityReachabilityPages"]
        or expected_persistent_snapshots["maximumLegacyPinsWithoutCatalog"]
        >= expected_persistent_snapshots["maximumPinsPerAuthority"]
    ):
        raise ValueError("persistent snapshot catalog bounds are inconsistent")
    if (
        expected_persistent_snapshots["maximumRangesPerCapsule"]
        != expected_physical_bounds["maximumEntityRetiredRanges"]
    ):
        raise ValueError("snapshot capsule and retirement range bounds differ")
    expected_wal_rotation = {
        "synchronousCheckpointBytes": 67108864,
        "softThresholdBytes": 62914560,
        "recoveryReadBufferBytes": 131072,
        "appendAdmission": "checked-u64-offset-without-maintenance-byte-quota",
        "foregroundMaintenancePolicy": "notify-existing-rotation-driver;never-wait-for-history-encoding-or-publication",
        "recoveryPolicy": "buffered-frame-scan;validate-record-size-before-allocation;preserve-exact-durable-prefix-and-tail-truncation",
        "maximumRecoveryPackedLocations": 65536,
        "foregroundMaterializeTargetBytes": 8388608,
        "historyPreparationTargetBytes": 262144,
        "maximumHistoryPreparationBytes": 4194304,
        "historyPreparationPolicy": "one-partial-or-pending-segment;whole-record-steps-may-exceed-target;finish-before-checkpoint;worker-absence-is-synchronous",
        "backgroundWaitRoundMilliseconds": 250,
        "maximumBackgroundWaitRounds": 240,
        "softPolicy": "existing-driver-prepares-and-publishes-checkpoint;successor-writes-continue-while-maintenance-is-delayed",
        "synchronousPolicy": "without-driver-incrementally-materialize;complete-checkpoint-at-synchronous-threshold",
        "waitPolicy": "recheck-background-creating-under-condition-mutex-before-wait;bounded-synchronous-fallback",
        "maximumPendingRetiredWals": 1,
        "retiredWalBytes": "durable-backlog-without-byte-quota;reclaim-only-after-checkpoint-publication",
        "retirementPolicy": "publish-checkpoint-before-queue;existing-background-packer-removes-and-syncs-parent-outside-authority;worker-absence-is-synchronous",
        "retirementBackpressure": "defer-next-checkpoint-only;retain-slot-on-delete-or-sync-error;never-refuse-new-appends",
        "retirementRecovery": "reconstruct-only-immediate-predecessor-from-current-control-generation;no-startup-directory-scan",
        "retirementShutdown": "finish-pending-retirement-before-worker-registration-release;surface-cleanup-error"
    }
    if document.get("activeWalRotation") != expected_wal_rotation:
        raise ValueError("active WAL rotation contract mismatch")
    expected_page_packing = {
        "targetBytes": 4096,
        "maximumBytes": 16384,
        "minimumChildrenPerSplitPage": 2,
        "encodingAllocation": "exact-page-length;prefix-key-frames;expanded-size-bounded;transfer-to-existing-pending-store",
        "modifiedLeafRuns": "repack-adjacent-written-leaves-with-one-page-buffer;retain-tombstones-and-untouched-references",
        "tailRebalance": "redistribute-or-merge-within-maximum-page-bytes",
    }
    if document.get("entityPagePacking") != expected_page_packing:
        raise ValueError("Tofu-DB entity page packing bounds differ")
    expected_page_codec = {
        "writeVersion": 2,
        "readVersions": [1, 2],
        "keyEncoding": "previous-key-u16-prefix-u16-suffix;first-prefix-zero;strictly-increasing-reconstructed-keys",
        "maximumExpandedBytes": 16384,
        "packingPolicy": "encoded-target-with-independent-expanded-ceiling",
        "decodedCacheAccounting": "max-legacy-encoded-charge-and-owned-decoded-allocations",
        "upgradePolicy": "lazy-copy-on-write;read-legacy-pages;old-readers-reject-version-two;no-startup-rewrite",
        "authority": "content-addressed-pages;existing-wal-control-publication-gc-and-backup",
    }
    if document.get("entityPageCodec") != expected_page_codec:
        raise ValueError("Tofu-DB entity page codec contract differs")
    expected_maintenance_scheduler = {
        "maximumWorkers": 1,
        "maximumScopes": 64,
        "workerStackBytes": 512 * 1024,
        "observedIdleIntervalMilliseconds": 250,
        "leanIdleIntervalMilliseconds": 1000,
        "maximumIdleIntervalMilliseconds": 60_000,
        "observedHistoryRetainedSegments": 64,
        "leanHistoryRetainedSegments": 16,
        "maximumHistoryRetainedSegments": 64,
        "historyRetirementPolicy": "trim-checkpointed-history-over-budget-with-active-wal-and-staged-prefix-preserved;never-force-foreground-checkpoint",
        "payloadPublicationPolicy": "idle-round-adopts-one-completed-background-pack-only-with-catalog-capacity;no-merge-or-wal-checkpoint",
        "backgroundPackShutdownPolicy": "reclaim-completed-unpublished-segment-before-worker-registration-release;preserve-staged-payloads-and-in-progress-publication;surface-cleanup-errors",
        "toolResultPruneRowsPerRound": 128,
        "authorityAdmission": "nonblocking-try-lock-one-bounded-transaction-per-round",
        "searchProjectionAdmission": "at-most-16-dirty-conversations-with-8-row-2MiB-source-pages-releasing-authority-between-pages",
        "searchProjectionMaximumSourcePagesPerConversation": 1024,
        "searchProjectionPageBudgetPolicy": "publish-explicit-partial-generation-and-ack-observed-tokens;source-mutation-rebuilds",
        "searchProjectionBackfillPageRows": 16,
        "searchProjectionMaximumDirtyConversations": 16,
        "searchProjectionBackfillPolicy": "enqueue-before-rebuild;defer-dirty-keys-beyond-pending-backfill-cursor",
        "searchProjectionFailurePolicy": "nonterminal-rebuildable-capability-degradation",
        "startupPolicy": "first-round-after-readiness-plus-one-idle-interval",
        "failurePolicy": "terminal-maintenance-error-stops-daemon-admission",
    }
    if document.get("maintenanceScheduler") != expected_maintenance_scheduler:
        raise ValueError("Tofu-DB maintenance scheduler contract is incomplete")
    expected_search_projection = {
        "minimumBytesPerOwner": 128 * 1024 * 1024,
        "maximumBytesPerOwner": 4 * 1024 * 1024 * 1024,
        "observedVolumeFreePercent": 2,
        "pathPolicy": "explicit-absolute-dedicated-persistent-directory-outside-authority",
        "queryAdmission": "independent-deadline-bounded-projection-mutex",
        "unavailablePolicy": "retryable-capability-error-without-authority-failure",
    }
    if document.get("searchProjection") != expected_search_projection:
        raise ValueError("Tofu-DB search projection contract is incomplete")
    expected_filesystem_certification = {
        "maximumRetainedEntries": 4096,
        # Must exceed the WAL carried ceiling (2 MiB) so the certification
        # commit spills its block to a loose file for the reclaim stage.
        "payloadBlockBytes": 2 * 1024 * 1024 + 1,
        "requiredMeasurements": [
            "write-process-micros",
            "first-reopen-micros",
            "post-reopen-commit-micros",
            "second-reopen-process-micros",
            "total-micros",
            "retained-file-count",
            "retained-file-bytes",
        ],
        "targetPolicy": "explicit-empty-persistent-path-retained-as-auditable-store",
        "requiredStages": [
            "exclusive-lock",
            "immutable-block",
            "group-commit",
            "checkpoint-rotation",
            "payload-segment-publication",
            "payload-loose-reclaim",
            "destructor-free-reopen",
            "cross-process-segment-random-read",
        ],
        "matrix": {
            "format": "tofu-db.filesystem-certification-matrix.v1",
            "operatorCommand": "certify-filesystem-matrix",
            "mountDirectoryEnvironment": "TOFU_DB_CERT_MATRIX_MOUNT_DIR",
            "maximumTraceOperations": 4096,
            "maximumDetailCharacters": 1024,
            "requiredCells": [
                "primary.path", "primary.path.at-rest",
                "mount.opt-in", "mount.opt-in.at-rest",
                "vfs-fault.syscall-error.interrupted",
                "vfs-fault.syscall-error.storage-full",
                "vfs-fault.short-write", "vfs-fault.lost-sync",
                "vfs-fault.lost-sync.correlated-all",
                "vfs-fault.syscall-error.interrupted.at-rest",
                "vfs-fault.syscall-error.storage-full.at-rest",
                "vfs-fault.short-write.at-rest", "vfs-fault.lost-sync.at-rest",
                "vfs-fault.lost-sync.correlated-all.at-rest",
            ],
            "summaryOutcomes": ["pass", "partial", "fail"],
        },
        "resourceGate": {
            "format": "tofu-db.resource-gate.v1",
            "operatorCommand": "certify-resource-gate",
            # The fixed gate workload: sequential inline commits plus 1 MiB
            # block-carried commits that cross the 60 MiB rotation window
            # with one midpoint checkpoint, one explicit commit+checkpoint
            # stage, and one reopen. Block bytes stay below the WAL carried
            # ceiling so the checkpoint stage exercises segment rotation
            # rather than loose-block spill.
            "inlineCommits": 512,
            "blockCommits": 24,
            "blockBytes": 1024 * 1024,
            "maximumInlineBytes": 64,
            # Observational reference budgets for the personal-computer
            # contract (8 GiB RAM / 500 GB disk) on one operator-recorded
            # volume class; the runner pins build and volume and compares
            # the emitted exceeded flags.
            "referenceVolumeBytes": 500_000_000_000,
            "referenceMemoryBytes": 8 * 1024**3,
            "referencePeakRssBytes": 512 * 1024**2,
            "referenceRetainedBytes": 1024**3,
            "referenceCommitMeanMicros": 5000,
            "referenceTotalMicros": 300_000_000,
            "budgetPolicy": "observational-reference-budgets-for-one-operator-recorded-volume-class;runner-pins-build-and-volume-and-compares-exceeded-flags",
        },
    }
    if document.get("filesystemCertification") != expected_filesystem_certification:
        raise ValueError("Tofu-DB filesystem certification contract is incomplete")
    expected_block_payload_cache = {
        "entryOverheadBytes": 256,
        "residentBytesPolicy": "existing-launch-derived-byte-cap-charges-vector-capacity-plus-fixed-entry-overhead;no-independent-entry-or-recency-cap;zero-disables-all-population",
        "evictionPolicy": "exact-lru-intrusive-list-constant-time-access-and-eviction;disable-releases-payload-and-metadata-allocations",
        "catalogInvalidation": "when-a-replacement-catalog-drops-a-previously-referenced-segment-evict-its-verified-index-ids-absent-from-every-added-segment;clear-on-any-uncertainty;pure-additions-keep-verified-payloads",
    }
    if document.get("blockPayloadReadCache") != expected_block_payload_cache:
        raise ValueError("Tofu-DB block payload read cache policy is incomplete")
    expected_payload_read_cache = {
        "maximumResidentIndexAndFilterBytes": 67108864,
        "maximumResidentSegments": 4096,
        "maximumRecencyRecords": 8192,
        "membershipFilterBitsPerBlock": 16,
        "membershipFilterMinimumBits": 64,
        "membershipFilterHashRounds": 4,
        "maximumFilterBytesPerSegment": 131072,
        "membershipPolicy": "verified-index-derived-negative-filter-retained-for-every-catalogued-segment-outside-the-index-byte-budget;forget-when-an-installed-catalog-drops-the-segment;positive-requires-exact-id;payload-crc-and-hash-on-every-read",
        "evictionPolicy": "bounded-access-history-with-exact-lru;forget-on-segment-removal"
    }
    if document.get("payloadSegmentReadCache") != expected_payload_read_cache:
        raise ValueError("Tofu-DB payload-segment read cache policy is incomplete")
    expected_authority_gc = {
        "maximumSessionCalls": 4096,
        "maximumOperatorMilliseconds": 600000,
        "defaultOperatorMilliseconds": 60000,
        "sessionPolicy": "exclusive-authority-borrow;stable-data-marks-plus-current-payload-manifest;no-cross-session-cache;poison-on-error;checked-finish-and-best-effort-drop-cleanup",
        "sessionTemporarySpacePolicy": "retained-marks-and-rewrite-share-existing-budget;release-marks-before-rewrite-when-combined-space-would-exceed-it",
        "sessionCompletionPolicy": "all-unchanged-catalog-windows-checked-before-idle;reset-on-manifest-change;plans-do-not-advance-coverage",
        "operatorBatchPolicy": "execute-only-max-rounds-and-budget-ms;soft-time-bound-between-rounds;stream-rounds-and-final-idle-or-budget-summary",
        "maximumVictimBytesPerRound": 256 * 1024 * 1024,
        "maximumTemporaryBytes": 1024 * 1024 * 1024,
        "temporaryFreeSpacePercent": 2,
        "leanTemporaryBytes": 64 * 1024 * 1024,
        "maximumBlocksPerRound": 65_536,
        "rpcMaximumPhysicalBlocksPerRequestedMillisecond": 256,
        "maximumTemporaryBlockFilesRemovedPerRound": 1,
        "maximumPayloadSegmentFilesScannedPerRound": 4163,
        "maximumOrphanPayloadSegmentFilesRemovedPerRound": 1,
        "maximumPayloadSegmentsScannedPerRound": 16,
        "minimumPayloadCompactionBlocks": 128,
        "snapshotPolicy": "defer-before-io-while-any-in-memory-mvcc-handle-exists",
        "temporaryBlockFilePolicy": "stream-match-strict-new-uuid-name-before-marking-remove-one-and-sync-shard",
        "controlPolicy": "republish-unchanged-state-before-first-delete-so-both-slots-share-roots",
        "payloadSegmentPolicy": "one-bounded-catalog-window-per-round-with-durable-control-cursor-and-at-most-one-segment-rewrite-or-retirement-after-loose-orphans",
        "payloadSegmentLivenessPolicy": "exact-block-id-intersection-across-all-hash-partitions;one-mark-run-and-one-segment-index-at-a-time",
        "orphanPayloadSegmentPolicy": "explicit-only-bounded-directory-window-stabilize-control-before-one-strictly-named-unreferenced-file-removal",
        "payloadCompactionPlanningPolicy": "one-bounded-loose-block-set-excluding-all-already-segmented-live-blocks-after-all-reclamation",
        "payloadCompactionMembershipPolicy": "bounded-filter-negative-or-exact-catalogued-index-membership;never-retain-catalog-wide-block-id-set",
        "looseMarkLookupPolicy": "skip-empty-shards;read-marks-only-for-populated-shards",
        "rpcOperation": "system.reclaim",
        "rpcBudgetPolicy": "validate-legacy-request-bounds;online-rpc-performs-no-physical-io",
        "rpcResponsePolicy": "online-collector-cumulative-progress;offline-not-required;statistics-after-first-epoch",
        "rpcTransactionPolicy": "explicit-physical-maintenance-without-business-transaction-or-receipt",
        "rpcPhysicalWalkPolicy": "online-two-mark-epoch-collector-reclaims;offline-exclusive-cli-remains-certified-heavy-hammer",
        "operatorPolicy": "existing-authority-plan-by-default-requires-execute-for-deletion",
    }
    if document.get("authorityGarbageCollection") != expected_authority_gc:
        raise ValueError("Tofu-DB authority garbage-collection contract is incomplete")
    expected_physical_rules = [
        "Entity B+Tree order is tenant ID, owner ID, physical namespace, then raw key bytes; key length never precedes key content.",
        "Every authority commit carries a tri-state entity-root update in its v2 transaction envelope and publishes the resulting current root in checksummed CONTROL; normal entity open verifies that CONTROL root without reading history, while legacy slots use one exact bounded transaction lookup.",
        "Every live entity transaction pins its immutable MVCC root in a database-instance registry; handles have no count admission quota, release is tied to handle lifetime, and handles cannot cross reopen boundaries.",
        "Entity transactions retain all distinct point and range witnesses needed for OCC without a witness-count admission quota; repeated point reads reuse their existing witness.",
        "The per-open entity transaction registry accounts actual retained bytes across point witnesses, worst-case range witnesses, and staged writes without an aggregate admission quota; arithmetic overflow fails closed and reservations release with transaction lifetime.",
        "Normal entity open reads and verifies only the content-addressed current root node; fully materialized roots without restored ranges remain direct B+Tree pages; partial materialization uses a version-2 directory with exact WAL coverage, while a versioned directory may overlay at most 64 sorted non-overlapping capsule range mounts on one base root. Descendant pages are verified lazily, base records and tombstones override mounted values, writes COW only the base, and one transaction may retire at most 64 sorted non-overlapping ranges or change mounts under an exact root witness. Explicit lazy consolidation materializes at most 999 rows and 8388608 bytes per commit by atomically replacing a mounted prefix in the base; it never runs during open and eventually collapses an exhausted directory to its direct base root.",
        "Explicit entity reachability walks are streaming and reject level or key-range transplants without a database-sized visited set; they admit at most 20000000 pages and an 8192-node frontier, follow root-directory bases and mounts plus at most 1000000 authority-global monotonic persistent full roots or 64-range capsules declared by the current CONTROL root, and require the discovered pin count to equal its transactional catalog. Catalogued pin admission uses bounded point reads; an absent catalog may upgrade at most 64 legacy pins.",
        "Explicit backup and garbage-collection reachability follows persisted stream segment references plus blob-backed versioned-document, command-receipt, and logical-outbox leaf values as owner-bound semantic graph edges without consulting transaction history; ordinary open and foreground range retirement do not scan values.",
        "Stream cursors and immutable segment references live in the current entity root alongside retained-prefix watermarks; metadata v2 decodes legacy v1 with a retained base of one, normal authority open never rebuilds catalogs from historical commits, retired cursors fail explicitly, and reads resolve at most 1000 events and 8 MiB from the caller's MVCC snapshot.",
        "Automatic history maintenance retains at most 64 complete immutable 4 MiB segments through a suffix-only v2 manifest, never rewrites transaction payloads or checkpoints an active WAL, and publishes only after one launch-probed low-priority maintenance admission.",
        "Explicit authority garbage collection first stream-matches every loose-directory entry with constant retained memory and removes at most one strictly named bounded .new temporary block file before reachability marking. It then streams current Entity, persistent capsule, semantic external-block, retained-history, and active-WAL reachability into bounded on-volume mark runs; it refuses live MVCC handles, republishes unchanged CONTROL state before referenced-file deletion, removes at most 65536 loose blocks or 268435456 bytes, scans at most 4163 payload-segment directory entries (4096 catalog slots + 2 in-flight background publications + 64 deferred retirements + 1) and removes at most one strictly named unreferenced complete or temporary segment, then inspects one generation-selected catalog window of at most 16 manifest segments and rewrites or retires at most one through a double-stabilized catalog per repeatable round. When the selected round has no reclaimable victim, it may compact at least 128 and at most 65536 reachable, not-already-segmented loose blocks in one bounded cross-shard batch within both the 268435456-byte segment ceiling and the launch-derived temporary-space budget; an executed no-victim catalog window advances the durable CONTROL cursor.",
    ]
    if document.get("physicalRules") != expected_physical_rules:
        raise ValueError("Tofu-DB physical ordering rule is incomplete")
