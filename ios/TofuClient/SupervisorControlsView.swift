import SwiftUI
import TofuClientCore

/// Start / Stop / Check for the Tofu server behind [profile], driving the
/// host-side supervisor. Port of SupervisorControls.kt — the same invariants:
///
///  1. Polls itself on first appearance when the profile is signed in, so the
///     card shows Running/Stopped without the user hunting for Check.
///  2. Legal actions come from the pure ``ServerLifecycle`` state machine.
///  3. The card's identity is ``CardKey/of`` — an edit mid-poll flips
///     `isCurrent` so a finished call can't yank the user around.
struct SupervisorControlsView: View {

    let profile: Profile
    /// Non-optional on purpose: controls do login-then-act, so a call site
    /// that omitted the session would silently skip the handshake and 401 on
    /// every supervisor call. Mandatory = a build failure, not a deadlock.
    let session: SessionManager
    var client: SupervisorClient = SupervisorClient()
    let onStateChange: (ServerState) -> Void
    let onServerReady: () -> Void

    @State private var running: Bool?
    @State private var busy = false
    @State private var failed = false
    @State private var message: String?
    /// The identity the RUNNING work belongs to — updated whenever the
    /// profile's key fields change, so a mid-poll edit invalidates the poll.
    @State private var currentKey: CardKey
    /// Click timestamp of the in-flight Start; committed to UserDefaults as
    /// the bounded start-pending marker on hand-off (see SupervisorBridge),
    /// lapsed silently on any other outcome.
    @State private var startArmedAtMs: Int64?

    init(
        profile: Profile,
        session: SessionManager,
        client: SupervisorClient = SupervisorClient(),
        onStateChange: @escaping (ServerState) -> Void,
        onServerReady: @escaping () -> Void
    ) {
        self.profile = profile
        self.session = session
        self.client = client
        self.onStateChange = onStateChange
        self.onServerReady = onServerReady
        _currentKey = State(initialValue: CardKey.of(profile))
    }

    private var state: ServerState {
        ServerLifecycle.resolve(profile: profile, running: running, busy: busy, failed: failed)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if busy {
                    ProgressView().controlSize(.small)
                }
                let caps = ServerLifecycle.capabilities(state)
                if caps.canStart {
                    Button {
                        run(.start, trigger: .user)
                    } label: {
                        Label("Start", systemImage: "play.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.accentColor)
                    .disabled(busy)
                }
                if caps.canStop {
                    Button {
                        run(.stop, trigger: .user)
                    } label: {
                        Label("Stop", systemImage: "stop.fill")
                    }
                    .buttonStyle(.bordered)
                    .disabled(busy)
                }
                if caps.canRefresh {
                    Button {
                        run(.status, trigger: .user)
                    } label: {
                        Label("Check", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .disabled(busy)
                }
                Spacer(minLength: 0)
            }
            .controlSize(.small)

            if let message {
                Text(message)
                    .font(.caption)
                    // Only a genuine failure is red; the start-timeout copy
                    // ("accepted, probably still booting") must not read like
                    // "login failed".
                    .foregroundStyle(failed ? Color.red : Color.secondary)
            }
        }
        // Report upward only when the resolved state actually CHANGES.
        .onChange(of: state) { onStateChange($0) }
        .onAppear { onStateChange(state) }
        // The card's identity changed (URL / projectPath edit): reset every
        // piece of per-card state — a stale Running badge must not survive,
        // and the auto-probe must re-fire to correct it.
        .onChange(of: CardKey.of(profile)) { newKey in
            currentKey = newKey
            running = nil
            busy = false
            failed = false
            message = nil
        }
        // Auto-probe on arrival so state is known without hunting for Check.
        // .task is composition-scoped: leaving the screen cancels the poll,
        // the exact lifetime the Android rememberCoroutineScope version has.
        .task(id: CardKey.of(profile)) {
            if running == nil && !busy {
                run(.status, trigger: .auto)
            }
        }
    }

    private func run(_ action: SupervisorAction, trigger: ProbeTrigger) {
        let plan = ServerLifecycle.probePlan(
            trigger: trigger, signedIn: ServerLifecycle.isSignedIn(profile)
        )
        // An AUTO probe with no session doesn't run at all — and `busy` stays
        // untouched, so the card never renders TRANSITIONING for a call we
        // never make.
        guard plan.proceed else { return }
        if action == .start {
            startArmedAtMs = Int64(Date().timeIntervalSince1970 * 1000)
        }
        let startedFor = CardKey.of(profile)
        busy = true
        failed = false
        message = nil
        Task {
            do {
                let outcome = try await executeSupervisorCall(
                    profile: profile,
                    action: action,
                    plan: plan,
                    signedIn: ServerLifecycle.isSignedIn(profile),
                    login: { await session.login($0) },
                    call: { a, p in
                        switch a {
                        case .start: return await client.start(p)
                        case .stop: return await client.stop(p)
                        case .status: return await client.status(p)
                        }
                    },
                    // Reads the CURRENT key, which a profile edit overwrites —
                    // so a mid-poll edit really does flip this to false.
                    isCurrent: { isStillCurrent(startedFor, currentKey) }
                )
                await MainActor.run {
                    if let r = outcome.running { running = r }
                    failed = outcome.failed
                    message = outcome.message
                    if outcome.handOff {
                        // Commit the bounded forgiveness marker FIRST — the
                        // hand-off opens the web screen, whose WebView stamps
                        // it into the page at document start.
                        if let armedAt = startArmedAtMs {
                            UserDefaults.standard.set(
                                String(armedAt + SupervisorBridge.ttlMs),
                                forKey: SupervisorBridge.pendingDefaultsKey
                            )
                        }
                        onServerReady()
                    }
                    startArmedAtMs = nil
                }
            } catch {
                // Cancellation (leaving the screen) is not an error.
            }
            // MUST be in a defer-equivalent position: any throw mid-poll
            // leaving busy stuck true pins the card to TRANSITIONING for the
            // rest of the process lifetime.
            await MainActor.run { busy = false }
        }
    }
}
