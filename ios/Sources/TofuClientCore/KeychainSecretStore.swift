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

    public init(service: String = "com.tofu.client.secrets") {
        self.service = service
    }

    public func secretFor(_ alias: String) -> String? {
        var query = baseQuery(alias)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func putSecret(_ secret: String, for alias: String) {
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
            SecItemAdd(attributes as CFDictionary, nil)
        }
    }

    public func removeSecret(_ alias: String) {
        SecItemDelete(baseQuery(alias) as CFDictionary)
    }

    private func baseQuery(_ alias: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: alias,
        ]
    }
}
