"""Project identity constants into the native IR output.
Entry: render_identity. Pure ordered projection of validated IR and shared input documents."""

import json


def render_identity(tables) -> list[str]:
    table = tables["desktop_egress_preferences"]
    tenant_users = tables["tenant_users"]
    credentials = tables["auth_credentials"]
    billing = tables["billing_wallets_and_ledger"]
    return [
        f'pub const DESKTOP_PREFERENCE_NAMESPACE: &str = "{table["physicalNamespace"]}";',
        'pub const BILLING_LEDGER_KINDS: &[&str] = &[' + ', '.join(json.dumps(kind) for kind in billing['ledgerKinds']) + '];',
        f'pub const BILLING_NATIVE_ADMISSION_KIND: &str = "{billing["nativeAdmission"]["ledgerKind"]}";',
        f'pub const BILLING_REQUEST_ADMISSION_KIND: &str = "{billing["requestSettlement"]["admissionKind"]}";',
        f'pub const BILLING_REQUEST_INVOICE_KIND: &str = "{billing["requestSettlement"]["invoiceKind"]}";',
        f'pub const BILLING_WALLET_NAMESPACE: &str = "{billing["walletNamespace"]}";',
        f'pub const BILLING_LEDGER_NAMESPACE: &str = "{billing["ledgerNamespace"]}";',
        f'pub const BILLING_LEDGER_ID_CLAIM_NAMESPACE: &str = "{billing["ledgerIdClaimNamespace"]}";',
        f'pub const BILLING_IDEMPOTENCY_NAMESPACE: &str = "{billing["idempotencyNamespace"]}";',
        f'pub const BILLING_USER_TIME_INDEX_NAMESPACE: &str = "{billing["userTimeIndexNamespace"]}";',
        f'pub const BILLING_USER_AGGREGATE_NAMESPACE: &str = "{billing["userAggregateNamespace"]}";',
        f'pub const BILLING_RESERVE_STATE_NAMESPACE: &str = "{billing["reserveStateNamespace"]}";',
        f'pub const BILLING_RESERVE_AGE_INDEX_NAMESPACE: &str = "{billing["reserveAgeIndexNamespace"]}";',
        f'pub const BILLING_PAYMENT_DOCUMENT_NAMESPACE: &str = "{billing["paymentDocumentNamespace"]}";',
        f'pub const BILLING_PAYMENT_ID_CLAIM_NAMESPACE: &str = "{billing["paymentIdClaimNamespace"]}";',
        f'pub const BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE: &str = "{billing["paymentProviderClaimNamespace"]}";',
        f'pub const BILLING_PAYMENT_CREATED_INDEX_NAMESPACE: &str = "{billing["paymentCreatedIndexNamespace"]}";',
        f'pub const BILLING_PAYMENT_COUNT_NAMESPACE: &str = "{billing["paymentCountNamespace"]}";',
        f'pub const BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE: &str = "{billing["redeemBatchDocumentNamespace"]}";',
        f'pub const BILLING_REDEEM_CODE_LOCATOR_NAMESPACE: &str = "{billing["redeemCodeLocatorNamespace"]}";',
        f'pub const BILLING_REDEEM_CODE_STATE_NAMESPACE: &str = "{billing["redeemCodeStateNamespace"]}";',
        f'pub const BILLING_REDEEM_CREATED_INDEX_NAMESPACE: &str = "{billing["redeemCreatedIndexNamespace"]}";',
        "pub const BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE: &str =",
        f'    "{billing["redeemBatchCreatedIndexNamespace"]}";',
        f'pub const BILLING_REDEEM_COUNT_NAMESPACE: &str = "{billing["redeemCountNamespace"]}";',
        "pub const MAX_BILLING_LEDGER_ENTRIES_PER_USER: usize = "
        f"{billing['maximumLedgerEntriesPerUser']};",
        f"pub const MAX_BILLING_PAYMENTS: usize = {billing['maximumPayments']};",
        "pub const MAX_BILLING_PAYMENT_DOCUMENT_BYTES: usize = "
        f"{billing['maximumPaymentDocumentBytes']};",
        f"pub const MAX_BILLING_REDEEM_CODES: usize = {billing['maximumRedeemCodes']};",
        "pub const MAX_BILLING_REDEEM_CODES_PER_MINT: usize = "
        f"{billing['maximumRedeemCodesPerMint']};",
        "pub const MAX_BILLING_REDEEM_BATCH_DOCUMENT_BYTES: usize = "
        f"{billing['maximumRedeemBatchDocumentBytes']};",
        f"pub const BILLING_REDEEM_BATCH_SHARDS: usize = {billing['redeemBatchShards']};",
        "pub const MAX_BILLING_REDEEM_BATCH_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemBatchShardBytes']};",
        f"pub const BILLING_REDEEM_LOCATOR_SHARDS: usize = {billing['redeemLocatorShards']};",
        "pub const MAX_BILLING_REDEEM_LOCATOR_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemLocatorShardBytes']};",
        "pub const MAX_BILLING_REDEEM_STATE_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemStateShardBytes']};",
        f"pub const MAX_BILLING_LIST_ROWS: usize = {billing['maximumListRows']};",
        "pub const MAX_BILLING_LIST_SCAN_ROWS: usize = "
        f"{billing['maximumListScanRows']};",
        "pub const MAX_BILLING_RESPONSE_BYTES: usize = "
        f"{billing['maximumResponseBytes']};",
        "pub const MAX_BILLING_NOTE_CHARACTERS: usize = "
        f"{billing['maximumNoteCharacters']};",
        f'pub const TENANT_USER_PROFILE_NAMESPACE: &str = "{tenant_users["profileNamespace"]}";',
        f'pub const TENANT_USER_STATE_NAMESPACE: &str = "{tenant_users["stateNamespace"]}";',
        f'pub const TENANT_USER_EMAIL_INDEX_NAMESPACE: &str = "{tenant_users["emailIndexNamespace"]}";',
        f'pub const TENANT_USER_CREATED_INDEX_NAMESPACE: &str = "{tenant_users["createdIndexNamespace"]}";',
        f'pub const TENANT_USER_STATUS_INDEX_NAMESPACE: &str = "{tenant_users["statusIndexNamespace"]}";',
        f'pub const TENANT_USER_OWNER_SEQUENCE_NAMESPACE: &str = "{tenant_users["ownerSequenceNamespace"]}";',
        "pub const MAX_TENANT_USER_DOCUMENT_BYTES: usize = "
        f"{tenant_users['maximumDocumentBytes']};",
        "pub const MAX_TENANT_USER_LIST_ROWS: usize = "
        f"{tenant_users['maximumListRows']};",
        "pub const MAX_TENANT_USER_LIST_SCAN_ROWS: usize = "
        f"{tenant_users['maximumListScanRows']};",
        "pub const MAX_TENANT_USER_RESPONSE_BYTES: usize = "
        f"{tenant_users['maximumResponseBytes']};",
        f'pub const CREDENTIAL_CORE_NAMESPACE: &str = "{credentials["coreNamespace"]}";',
        f'pub const CREDENTIAL_SETTINGS_NAMESPACE: &str = "{credentials["settingsNamespace"]}";',
        f'pub const CREDENTIAL_STATE_NAMESPACE: &str = "{credentials["stateNamespace"]}";',
        f'pub const CREDENTIAL_SECRET_INDEX_NAMESPACE: &str = "{credentials["secretIndexNamespace"]}";',
        f'pub const CREDENTIAL_OWNER_INDEX_NAMESPACE: &str = "{credentials["ownerIndexNamespace"]}";',
        f'pub const CREDENTIAL_OWNER_COUNT_NAMESPACE: &str = "{credentials["ownerCountNamespace"]}";',
        "pub const MAX_CREDENTIAL_DOCUMENT_BYTES: usize = "
        f"{credentials['maximumDocumentBytes']};",
        f"pub const MAX_CREDENTIAL_SCOPES: usize = {credentials['maximumScopes']};",
        "pub const MAX_CREDENTIAL_RESPONSE_BYTES: usize = "
        f"{credentials['maximumResponseBytes']};",
        "pub const MAX_CREDENTIAL_TIMESTAMP_SECONDS: f64 = "
        f"{credentials['maximumTimestampSeconds']}.0;",
    ]
