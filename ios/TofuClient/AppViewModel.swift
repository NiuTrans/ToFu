import Foundation
import TofuClientCore

/// What the host UI is currently showing — port of Screen.kt.
enum AppScreen: Equatable {
    case list
    case addEdit
    case web(Profile)
}

/// A transient status the UI surfaces (login progress / errors).
/// Port of UiStatus.kt.
enum UiStatus: Equatable {
    case idle
    case loggingIn(alias: String)
    case error(message: String)
    case badCredentials(Profile)
}

/// Holds the profile list + navigation/status state and delegates every
/// mutation to ``SessionController``. Kept dumb: no session logic lives here
/// (it's all in the tested controller), so this type is just wiring + state.
/// Port of ProfilesViewModel.kt.
@MainActor
final class AppViewModel: ObservableObject {

    @Published private(set) var profiles: [Profile] = []
    @Published var screen: AppScreen = .list
    @Published var status: UiStatus = .idle
    /// The profile currently being edited (nil = adding a new one).
    @Published private(set) var editing: Profile?

    let store: SQLiteProfileStore
    let secrets: KeychainSecretStore
    let session: SessionManager
    let controller: SessionController

    init(
        store: SQLiteProfileStore,
        secrets: KeychainSecretStore,
        session: SessionManager,
        controller: SessionController
    ) {
        self.store = store
        self.secrets = secrets
        self.session = session
        self.controller = controller
        // The Room Flow analogue: any store mutation re-reads the list.
        store.onDidChange = { [weak self] in
            Task { @MainActor in await self?.reload() }
        }
    }

    func reload() async {
        profiles = await store.getAllOnce()
    }

    /// One-time upgrade migration, run once at app open: fix persisted proxy
    /// profiles stuck on the stale NONE default (see
    /// ``SessionController/migrateProxyAuthDefaults``). Idempotent.
    func migrateOnLaunch() async {
        _ = await controller.migrateProxyAuthDefaults()
        await reload()
    }

    func startAdd() {
        editing = nil
        screen = .addEdit
    }

    func startEdit(_ profile: Profile) {
        editing = profile
        screen = .addEdit
    }

    func backToList() {
        screen = .list
        status = .idle
    }

    func secretStoredFor(_ alias: String) -> Bool {
        secrets.secretFor(alias) != nil
    }

    /// If [url] shares a host with another profile that already has a stored
    /// password, return that host (for a proactive "password will be reused"
    /// hint). Nil = nothing to reuse.
    func reusableSecretHost(_ url: String, excludeAlias: String?) async -> String? {
        guard await controller.findSharedSecret(url, excludeAlias: excludeAlias) != nil else {
            return nil
        }
        return ServerUrl.parse(url)?.host
    }

    func activate(_ profile: Profile) {
        status = .loggingIn(alias: profile.alias)
        Task {
            // Navigate with the row the controller actually read back, not the
            // list-rendered snapshot: that snapshot lags any write not yet
            // re-emitted, and for SSO it is what the web screen holds for the
            // entire session.
            let r = await controller.activate(profile)
            handleLogin(r.login, r.persisted)
        }
    }

    func submitAdd(alias: String, url: String, auth: AuthType, secret: String, projectPath: String) {
        status = .loggingIn(alias: alias)
        Task {
            switch await controller.addProfile(
                alias: alias, baseUrl: url, authType: auth,
                secret: secret, projectPath: projectPath
            ) {
            case .duplicateAlias:
                status = .error(message: "A server named \"\(alias)\" already exists")
            case .added(_, let login):
                // The row was re-read by the controller's caller path; reload
                // so the list shows it, then handle the login outcome.
                await reload()
                if let persisted = profiles.first(where: { $0.alias == alias.trimmingCharacters(in: .whitespacesAndNewlines) }) {
                    handleLogin(login, persisted)
                } else {
                    status = .idle
                }
            }
        }
    }

    func submitEdit(current: Profile, alias: String, url: String, auth: AuthType, secret: String, projectPath: String) {
        status = .loggingIn(alias: alias)
        Task {
            // Navigate with the row the controller ACTUALLY wrote, never a
            // locally-rebuilt copy: the host-change path nulls cookieHost, and
            // for SSO this object is what the web screen holds.
            let r = await controller.editProfile(
                current: current, newAlias: alias, newUrl: url,
                newAuthType: auth, newSecret: secret, newProjectPath: projectPath
            )
            handleLogin(r.login, r.persisted)
        }
    }

    func deleteProfile(_ profile: Profile) {
        Task { await controller.deleteProfile(profile) }
    }

    private func handleLogin(_ result: LoginResult, _ profile: Profile) {
        // SSO must NAVIGATE, not just set a status — its whole design is "the
        // WebView completes the sign-in once, then we persist the jar".
        if InteractiveSso.shouldOpenWebView(result) {
            screen = .web(profile)
            status = .idle
            return
        }
        switch result {
        case .badCredentials, .noCredential:
            status = .badCredentials(profile)
        case .error(let message), .incompatible(let message):
            status = .error(message: message)
        case .success:
            // Session established (or none needed) — open the server.
            screen = .web(profile)
            status = .idle
        case .needsInteractiveSso:
            status = .idle // navigated above
        }
    }
}
