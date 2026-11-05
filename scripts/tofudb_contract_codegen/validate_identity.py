"""Validate identity table contracts.
Entries: named validate_* functions, called in the original order by validation.load_ir. No I/O."""

def validate_billing(billing) -> None:
    if (
        not isinstance(billing, dict)
        or billing.get("ownerKey") != "billing_subject_user_id"
        or billing.get("physicalFamily") != "entity+blob"
        or billing.get("ledgerKinds") != sorted(set(billing.get("ledgerKinds", [])))
        or billing.get("nativeAdmission", {}).get("ledgerKind") not in billing.get("ledgerKinds", [])
        or billing.get("nativeAdmission", {}).get("refType") != "reserve"
        or billing.get("nativeAdmission", {}).get("amountMicro") != 0
        or billing.get("walletNamespace") != "billing_wallets"
        or billing.get("ledgerNamespace") != "billing_ledger_entries"
        or billing.get("ledgerIdClaimNamespace") != "billing_ledger_id_claims"
        or billing.get("idempotencyNamespace") != "billing_ledger_idempotency"
        or billing.get("userTimeIndexNamespace") != "billing_ledger_by_user_time_desc"
        or billing.get("userAggregateNamespace") != "billing_ledger_user_aggregates"
        or billing.get("reserveStateNamespace") != "billing_reserve_states"
        or billing.get("reserveAgeIndexNamespace") != "billing_reserves_by_age"
        or billing.get("paymentDocumentNamespace") != "billing_payment_documents"
        or billing.get("paymentIdClaimNamespace") != "billing_payment_id_claims"
        or billing.get("paymentProviderClaimNamespace") != "billing_payment_provider_claims"
        or billing.get("paymentCreatedIndexNamespace") != "billing_payments_by_created_desc"
        or billing.get("paymentCountNamespace") != "billing_payment_counts"
        or billing.get("redeemBatchDocumentNamespace") != "billing_redeem_batches"
        or billing.get("redeemCodeLocatorNamespace") != "billing_redeem_code_locators"
        or billing.get("redeemCodeStateNamespace") != "billing_redeem_code_states"
        or billing.get("redeemCreatedIndexNamespace") != "billing_redeem_batches_by_created_desc"
        or billing.get("redeemBatchCreatedIndexNamespace")
        != "billing_redeem_batches_by_label_created_desc"
        or billing.get("redeemCountNamespace") != "billing_redeem_code_counts"
        or billing.get("maximumLedgerEntriesPerUser") != 100_000
        or billing.get("maximumPayments") != 100_000
        or billing.get("maximumPaymentDocumentBytes") != 8 * 1024 * 1024
        or billing.get("maximumRedeemCodes") != 100_000
        or billing.get("maximumRedeemCodesPerMint") != 10_000
        or billing.get("maximumRedeemBatchDocumentBytes") != 8 * 1024 * 1024
        or billing.get("redeemBatchShards") != 4096
        or billing.get("maximumRedeemBatchShardBytes") != 64 * 1024 * 1024
        or billing.get("redeemLocatorShards") != 4096
        or billing.get("maximumRedeemLocatorShardBytes") != 64 * 1024 * 1024
        or billing.get("maximumRedeemStateShardBytes") != 64 * 1024 * 1024
        or billing.get("maximumListRows") != 1000
        or billing.get("maximumListScanRows") != 10_000
        or billing.get("maximumResponseBytes") != 8 * 1024 * 1024
        or billing.get("maximumNoteCharacters") != 4000
    ):
        raise ValueError("Tofu-DB billing ledger must be bounded and exact")


def validate_tenant_users(tenant_users) -> None:
    if (
        not isinstance(tenant_users, dict)
        or tenant_users.get("ownerKey") != "tenant_admin_boundary"
        or tenant_users.get("physicalFamily") != "entity+blob"
        or tenant_users.get("profileNamespace") != "tenant_user_profiles"
        or tenant_users.get("stateNamespace") != "tenant_user_states"
        or tenant_users.get("emailIndexNamespace") != "tenant_users_by_email_digest"
        or tenant_users.get("createdIndexNamespace") != "tenant_users_by_created_desc"
        or tenant_users.get("statusIndexNamespace")
        != "tenant_users_by_status_created_desc"
        or tenant_users.get("ownerSequenceNamespace") != "tenant_user_owner_sequence"
        or tenant_users.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or tenant_users.get("maximumListRows") != 1000
        or tenant_users.get("maximumListScanRows") != 10_000
        or tenant_users.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB tenant users must be bounded and tenant-global")


def validate_credentials(credentials) -> None:
    if (
        not isinstance(credentials, dict)
        or credentials.get("ownerKey") != "owner_user_id+tenant_label"
        or credentials.get("physicalFamily") != "entity+blob"
        or credentials.get("coreNamespace") != "credential_cores"
        or credentials.get("settingsNamespace") != "credential_settings"
        or credentials.get("stateNamespace") != "credential_states"
        or credentials.get("secretIndexNamespace") != "credentials_by_secret_hash"
        or credentials.get("ownerIndexNamespace")
        != "credentials_by_owner_created_desc"
        or credentials.get("ownerCountNamespace") != "credential_owner_counts"
        or credentials.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or "maximumCredentialsPerOwnerBoundary" in credentials
        or credentials.get("ownerCountIsIntegrityObservation") is not True
        or credentials.get("listHasNoAggregateResponseQuota") is not True
        or credentials.get("maximumScopes") != 128
        or credentials.get("maximumResponseBytes") != 8 * 1024 * 1024
        or credentials.get("maximumTimestampSeconds") != 32_503_680_000
    ):
        raise ValueError("Tofu-DB credentials must be bounded and owner scoped")


def validate_providers(providers) -> None:
    if (
        not isinstance(providers, dict)
        or providers.get("ownerKey") != "owner_user_id"
        or providers.get("physicalFamily") != "entity+blob"
        or providers.get("physicalNamespace") != "byo_provider_documents"
        or providers.get("createdIndexNamespace") != "byo_providers_by_created"
        or providers.get("ownerCountNamespace") != "byo_provider_counts"
        or providers.get("tenantGlobalClaimNamespace") != "byo_provider_id_claims"
        or providers.get("countPolicy") != "exact-owner-label-count-without-admission-quota"
        or providers.get("maximumTenantLabelCharacters") != 256
    ):
        raise ValueError("Tofu-DB providers must preserve owner-scoped count and document validation")


def validate_model_routing(model_routing) -> None:
    if (
        not isinstance(model_routing, dict)
        or model_routing.get("ownerKey") != "owner_user_id"
        or model_routing.get("physicalFamily") != "entity+blob"
        or model_routing.get("authorityNamespace") != "model_routing_authorities"
        or model_routing.get("authorityMetadataNamespace")
        != "model_routing_authority_metadata"
        or model_routing.get("backupNamespace") != "model_routing_backups"
        or model_routing.get("migrationReceiptNamespace")
        != "model_routing_migration_receipts"
        or model_routing.get("secretNamespace") != "model_routing_secrets"
        or model_routing.get("secretCountNamespace") != "model_routing_secret_counts"
        or model_routing.get("secretReferenceIndexNamespace")
        != "model_routing_secrets_by_reference"
        or model_routing.get("secretUpdatedIndexNamespace")
        != "model_routing_secrets_by_updated"
        or model_routing.get("maximumMigrationCiphertextBytes") != 8 * 1024 * 1024
        or model_routing.get("documentsHaveNoAdmissionByteQuota") is not True
        or model_routing.get("maximumSecretReferenceCharacters") != 256
        or model_routing.get("maximumCiphertextCharacters") != 32768
        or model_routing.get("maximumKeyHintCharacters") != 64
        or model_routing.get("maximumPrunedPerCommand") != 256
        or model_routing.get("maximumTimestampSeconds") != 32_503_680_000
    ):
        raise ValueError("Tofu-DB model routing must validate records and preserve owner scope")
