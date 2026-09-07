import Foundation

/// Persistence seam for ``Profile`` rows — the ProfileDao surface the session
/// layer writes through. The production impl (app target) adds observation for
/// the UI; the core depends on this surface only.
///
/// Several writes are TARGETED single-column operations on purpose: a card
/// tap can land between a snapshot read and a write, and a full-row update
/// would then reinstate stale `cookieHost` / `lastUsedAt` values.
public protocol ProfileStore: Sendable {
    func getAllOnce() async -> [Profile]
    func getById(_ id: Int64) async -> Profile?
    func getByAlias(_ alias: String) async -> Profile?
    /// Returns the new row id.
    @discardableResult func insert(_ profile: Profile) async -> Int64
    func update(_ profile: Profile) async
    func deleteById(_ id: Int64) async
    func touchLastUsed(_ id: Int64, _ at: Int64) async
    func setAuthType(_ id: Int64, _ authType: AuthType) async
    func setCookieHost(_ id: Int64, _ host: String?) async
}
