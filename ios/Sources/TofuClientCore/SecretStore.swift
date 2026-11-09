import Foundation

/// Read seam over the credential store (Keychain in production; a dictionary
/// fake in tests). Port of SecretLookup.kt. Keyed by ALIAS, not URL — a
/// re-provisioned sandbox keeps its credential across the host change.
public protocol SecretLookup: Sendable {
    func secretFor(_ alias: String) -> String?
}

/// Write+read seam the app target's Keychain store conforms to; the core's
/// login path depends only on ``SecretLookup``, the controller on this.
/// Port of SecretVault.kt.
public protocol SecretStore: SecretLookup {
    func putSecret(_ secret: String, for alias: String)
    func removeSecret(_ alias: String)
    /// Post-commit alias/password reconciliation. Compare BOTH keys with the
    /// edit's snapshots under the same lock used by ordinary vault writes.
    /// Never overwrite changed keys; verify the destination before removing
    /// the source. False means a concurrent change or a vault write failure.
    @discardableResult func applyProfileEdit(
        from oldAlias: String, to newAlias: String,
        expectedOldSecret: String?, expectedNewSecret: String?, secret: String?
    ) -> Bool
}
