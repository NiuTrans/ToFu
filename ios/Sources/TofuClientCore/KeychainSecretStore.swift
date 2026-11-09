#if canImport(Security)
import Foundation
import Security

/// Production secret store: the Keychain — port of Android's
/// EncryptedSharedPreferences-backed SecretStore.
///
/// Keyed by ALIAS, not URL (the Profile doc rule): a re-provisioned sandbox
/// keeps its credential across a baseUrl edit. `ThisDeviceOnly` keeps the
/// item out of iCloud Keychain — a server credential belongs to the device
/// that typed it, mirroring the Android store's device-local encryption.
public final class KeychainSecretStore: SecretStore, @unchecked Sendable {

    private let service: String
    private let lock = NSLock()

    public init(service: String = "com.tofu.client.secrets") {
        self.service = service
    }

    public func secretFor(_ alias: String) -> String? {
        lock.lock(); defer { lock.unlock() }
        return readSecret(alias).secret
    }

    public func putSecret(_ secret: String, for alias: String) {
        lock.lock(); defer { lock.unlock() }
        _ = writeSecret(secret, for: alias)
    }

    public func removeSecret(_ alias: String) {
        lock.lock(); defer { lock.unlock() }
        _ = deleteSecret(alias)
    }

    public func applyProfileEdit(
        from oldAlias: String, to newAlias: String,
        expectedOldSecret: String?, expectedNewSecret: String?, secret: String?
    ) -> Bool {
        lock.lock(); defer { lock.unlock() }
        let source = readSecret(oldAlias), destination = readSecret(newAlias)
        guard source.succeeded, destination.succeeded,
              source.secret == expectedOldSecret, destination.secret == expectedNewSecret else { return false }
        if let secret, secret != expectedNewSecret, !writeSecret(secret, for: newAlias) { return false }
        if oldAlias != newAlias, expectedOldSecret != nil {
            // A failed destination write must never destroy the only copy.
            let written = readSecret(newAlias)
            guard let secret, written.succeeded, written.secret == secret else { return false }
            return deleteSecret(oldAlias)
        }
        return true
    }

    private func readSecret(_ alias: String) -> (succeeded: Bool, secret: String?) {
        var query = baseQuery(alias)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return (true, nil) }
        guard status == errSecSuccess, let data = item as? Data,
              let secret = String(data: data, encoding: .utf8) else { return (false, nil) }
        return (true, secret)
    }

    private func writeSecret(_ secret: String, for alias: String) -> Bool {
        let data = Data(secret.utf8)
        let status = SecItemUpdate(
            baseQuery(alias) as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if status == errSecItemNotFound {
            var attributes = baseQuery(alias)
            attributes[kSecValueData as String] = data
            attributes[kSecAttrAccessible as String] =
                kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
        }
        return status == errSecSuccess
    }

    private func deleteSecret(_ alias: String) -> Bool {
        let status = SecItemDelete(baseQuery(alias) as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    private func baseQuery(_ alias: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: alias,
        ]
    }
}
#endif
