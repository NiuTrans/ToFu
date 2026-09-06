import Foundation

/// What a server card renders — the projection the real SwiftUI list consumes.
public struct ServerCardUiState: Equatable, Sendable {
    public let profile: Profile
    public let serverState: ServerState
    public let failureText: String?
    public let showTransitioning: Bool
    public let showUnreachable: Bool
    public let capabilities: CardCapabilities
}

/// The test-double seam for the real server list: identical invariants to
/// Android's list ViewModel (pending-start TTL, hand-off arming, guarded
/// start path) without any SwiftUI dependency. Async so the real VM can call
/// it 1:1; WebView/UserDefaults touches are injected closures.
///
/// The pending-start contract: when the user taps Start and the start
/// hand-off fires (server came up), the card arms ``SupervisorBridge`` so the
/// freshly reloaded page forgives the one login bounce that a headless
/// start's 401→login→reload sequence produces (see SupervisorBridge docs).
/// The marker is written at HAND-OFF time (not click time) — arming on click
/// would forgive a bounce for a server that never started.
public final class ServerListViewModel {

    /// The armed pending-start expectation, if a Start click is awaiting its
    /// hand-off. Armed at click with the click timestamp; committed to the
    /// page bridge only on hand-off.
    public private(set) var pendingStart: (key: CardKey, armedAtMs: Int64)?

    private let isManaged: (Profile) -> Bool
    private let nowMs: () -> Int64
    private let commitStartPending: (Int64, Int64) -> Void

    public init(
        isManaged: @escaping (Profile) -> Bool = ServerLifecycle.isManaged,
        nowMs: @escaping () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) },
        commitStartPending: @escaping (Int64, Int64) -> Void = { _, _ in }
    ) {
        self.isManaged = isManaged
        self.nowMs = nowMs
        self.commitStartPending = commitStartPending
    }

    /// A card resolved for display. Failure text appears only where the user
    /// must act (stopped/unreachable), per the Android card rules.
    public func uiState(
        for profile: Profile,
        serverState: ServerState,
        failureText: String? = nil
    ) -> ServerCardUiState {
        let caps = ServerLifecycle.capabilities(serverState)
        let showTransitioning = serverState == .transitioning
        let showUnreachable = serverState == .unreachable
        let text: String?
        switch serverState {
        case .stopped, .unreachable:
            text = failureText ?? ServerLifecycle.label(serverState)
        default:
            text = nil
        }
        return ServerCardUiState(
            profile: profile,
            serverState: serverState,
            failureText: text,
            showTransitioning: showTransitioning,
            showUnreachable: showUnreachable,
            capabilities: caps
        )
    }

    /// The user tapped Start. Guarded: only a card whose capabilities allow
    /// starting (and which is actually managed) may arm a pending hand-off.
    public func onStartClick(_ card: ServerCardUiState) {
        guard isManaged(card.profile), card.capabilities.canStart else { return }
        pendingStart = (CardKey.of(card.profile), nowMs())
    }

    /// The start poll finished: server is running and the card is current —
    /// the hand-off fires (the real VM navigates into the WebView). Commit
    /// the pending marker so the page forgives the login bounce.
    public func onHandOff() {
        guard let pending = pendingStart else { return }
        pendingStart = nil
        commitStartPending(pending.key.id, pending.armedAtMs + SupervisorBridge.ttlMs)
    }

    /// The start poll finished without the server coming up (timeout message,
    /// not an error). The pending expectation lapses silently.
    public func onStartTimeout() {
        pendingStart = nil
    }

    /// A status poll landed. If a pending start is now satisfied… the hand-off
    /// path is what commits; a bare status flip never arms the bridge.
    public func onStatusUpdate(running: Bool) {
        _ = running
    }
}
