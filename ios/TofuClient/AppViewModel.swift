import Foundation
import Combine
import TofuClientCore

/// What the host UI is currently showing — port of Screen.kt.
enum AppScreen: Equatable {
    case list
    case addEdit
    case web(Profile)
}

/// A transient status the UI surfaces (login progress / errors).
enum UiStatus: Equatable {
    case idle
    case loggingIn(alias: String)
    case error(message: String)
    case badCredentials(Profile)
}

/// UI wiring only: session decisions stay in SessionController. Owns one
/// cancellable operation and fences results against UI intent and the actual
/// store configuration. Connection review retains no local password fields.
@MainActor
final class AppViewModel: ObservableObject {
    @Published private(set) var profiles: [Profile] = []
    @Published private(set) var screen: AppScreen = .list
    @Published private(set) var status: UiStatus = .idle
    @Published private(set) var editing: Profile?
    @Published private var connectionReview = ConnectionReviewState()
    @Published private(set) var formRevision = 0

    var onboardingDraft: ConnectionDraft? { connectionReview.accepted }
    var pendingConnection: ConnectionDraft? { connectionReview.pending }

    private var operationTask: Task<Void, Never>?
    private var operationGeneration = ProfileOperationGeneration()

    let store: SQLiteProfileStore
    let secrets: KeychainSecretStore
    let session: SessionManager
    let controller: SessionController

    init(store: SQLiteProfileStore, secrets: KeychainSecretStore,
         session: SessionManager, controller: SessionController) {
        self.store = store
        self.secrets = secrets
        self.session = session
        self.controller = controller
        store.onDidChange = { [weak self] in
            Task { @MainActor in await self?.reload() }
        }
    }

    deinit { operationTask?.cancel() }

    func reload() async {
        guard !Task.isCancelled else { return }
        let rows = await store.getAllOnce()
        guard !Task.isCancelled else { return }
        profiles = rows
    }

    func startAdd() {
        invalidateOperation()
        editing = nil
        connectionReview.reset()
        formRevision &+= 1
        screen = .addEdit
    }

    func receiveConnection(_ input: String) {
        // Even a rejected new intent supersedes an old sign-in. Never echo a
        // rejected URL, which may contain the very credentials being refused.
        invalidateOperation()
        guard let draft = ConnectionOnboarding.parse(input) else {
            status = .error(message: "Use a credential-free server URL or tofu://connect link. No password or token can be imported.")
            return
        }
        guard connectionReview.receive(draft, formIsOpen: screen == .addEdit) else { return }
        editing = nil
        formRevision &+= 1
        screen = .addEdit
    }

    func usePendingConnection() {
        guard pendingConnection != nil else { return }
        invalidateOperation()
        guard connectionReview.usePending() else { return }
        editing = nil
        formRevision &+= 1 // explicit acceptance discards the old form fields
        screen = .addEdit
    }

    func keepCurrentForm() {
        invalidateOperation()
        connectionReview.keepForm() // accepted metadata and form identity survive
    }

    func startEdit(_ profile: Profile) {
        invalidateOperation()
        connectionReview.reset()
        var reviewed = profile
        reviewed.baseUrl = ServerUrl.sanitizeForStorage(profile.baseUrl) ?? ""
        editing = reviewed
        if ServerUrl.parse(profile.baseUrl) == nil {
            status = .error(message: "This saved address was unsafe. Review a credential-free server URL before saving.")
        }
        formRevision &+= 1
        screen = .addEdit
    }

    func backToList() {
        invalidateOperation()
        editing = nil
        connectionReview.reset()
        screen = .list
    }

    /// Local field changes supersede the submitted snapshot, without copying
    /// any field or password into retained intent/review state.
    func formDidChange() { invalidateOperation() }
    func cancelOperation() { invalidateOperation() }

    func secretStoredFor(_ alias: String) -> Bool { secrets.secretFor(alias) != nil }

    func reusableSecretHost(_ url: String, excludeAlias: String?) async -> String? {
        guard !Task.isCancelled,
              await controller.findSharedSecret(url, excludeAlias: excludeAlias) != nil,
              !Task.isCancelled else { return nil }
        return ServerUrl.parse(url)?.host
    }

    func activate(_ profile: Profile) {
        guard screen == .list else { return }
        let generation = beginOperation(alias: profile.alias)
        operationTask = Task { [weak self] in
            guard let self, self.isCurrent(generation) else { return }
            defer { self.finishOperation(generation) }
            let stored = await self.store.getById(profile.id)
            guard self.isCurrent(generation) else { return }
            guard self.operationGeneration.canComplete(generation, isCancelled: Task.isCancelled,
                                                       expected: profile, stored: stored), let stored else {
                self.profileChanged()
                return
            }
            let result = await self.controller.activate(stored)
            guard self.isCurrent(generation) else { return }
            await self.completeLogin(result.login, expected: stored, generation: generation)
        }
    }

    func submitAdd(alias: String, url: String, auth: AuthType, secret: String, projectPath: String) {
        guard screen == .addEdit, editing == nil, pendingConnection == nil else { return }
        let generation = beginOperation(alias: alias)
        operationTask = Task { [weak self] in
            guard let self, self.isCurrent(generation) else { return }
            defer { self.finishOperation(generation) }
            let result = await self.controller.addProfile(alias: alias, baseUrl: url, authType: auth,
                                                         secret: secret, projectPath: projectPath)
            guard self.isCurrent(generation) else { return }
            switch result {
            case .duplicateAlias:
                self.status = .error(message: "A server named \"\(alias)\" already exists")
            case .invalidInput(let message):
                self.status = .error(message: message)
            case .added(let persisted, let login):
                await self.completeLogin(login, expected: persisted, generation: generation)
            }
        }
    }

    func submitEdit(current: Profile, alias: String, url: String, auth: AuthType, secret: String, projectPath: String) {
        guard screen == .addEdit, editing?.id == current.id, pendingConnection == nil else { return }
        let generation = beginOperation(alias: alias)
        operationTask = Task { [weak self] in
            guard let self, self.isCurrent(generation) else { return }
            defer { self.finishOperation(generation) }
            let stored = await self.store.getById(current.id)
            guard self.isCurrent(generation) else { return }
            guard let stored else { self.profileChanged(); return }
            // Compare the same credential-free projection the edit form saw,
            // then pass the actual row to the controller for rename/purge.
            var reviewed = stored
            reviewed.baseUrl = ServerUrl.sanitizeForStorage(stored.baseUrl) ?? ""
            guard self.operationGeneration.canComplete(generation, isCancelled: Task.isCancelled,
                                                       expected: current, stored: reviewed) else {
                self.profileChanged()
                return
            }
            let expected = ProfileForm.toProfile(id: stored.id, alias: alias, baseUrl: url,
                                                 authType: auth, lastUsedAt: stored.lastUsedAt,
                                                 projectPath: projectPath)
            let result = await self.controller.editProfile(current: stored, newAlias: alias, newUrl: url,
                                                          newAuthType: auth, newSecret: secret,
                                                          newProjectPath: projectPath)
            guard self.isCurrent(generation) else { return }
            await self.completeLogin(result.login, expected: expected, generation: generation)
        }
    }

    func deleteProfile(_ profile: Profile) {
        guard screen == .list else { return }
        let generation = beginOperation(alias: nil)
        operationTask = Task { [weak self] in
            guard let self, self.isCurrent(generation) else { return }
            defer { self.finishOperation(generation) }
            let stored = await self.store.getById(profile.id)
            guard self.isCurrent(generation) else { return }
            guard self.operationGeneration.canComplete(generation, isCancelled: Task.isCancelled,
                                                       expected: profile, stored: stored), let stored else {
                self.profileChanged()
                return
            }
            await self.controller.deleteProfile(stored)
            guard self.isCurrent(generation) else { return }
            _ = await self.reload(for: generation)
        }
    }

    private func invalidateOperation() {
        operationTask?.cancel()
        operationTask = nil
        operationGeneration.advance()
        status = .idle
    }

    private func beginOperation(alias: String?) -> UInt64 {
        invalidateOperation()
        if let alias { status = .loggingIn(alias: alias) }
        return operationGeneration.current
    }

    private func isCurrent(_ generation: UInt64) -> Bool {
        operationGeneration.isCurrent(generation, isCancelled: Task.isCancelled)
    }

    private func finishOperation(_ generation: UInt64) {
        guard operationGeneration.current == generation else { return }
        operationTask = nil
        if case .loggingIn = status { status = .idle }
    }

    private func reload(for generation: UInt64) async -> Bool {
        guard isCurrent(generation) else { return false }
        let rows = await store.getAllOnce()
        guard isCurrent(generation) else { return false }
        profiles = rows
        return true
    }

    private func completeLogin(_ result: LoginResult, expected: Profile, generation: UInt64) async {
        guard isCurrent(generation), await reload(for: generation), isCurrent(generation) else { return }
        let stored = await store.getById(expected.id)
        guard isCurrent(generation) else { return }
        guard operationGeneration.canComplete(generation, isCancelled: Task.isCancelled,
                                              expected: expected, stored: stored), let stored else {
            profileChanged()
            return
        }
        handleLogin(result, stored) // navigation uses the row actually persisted
    }

    private func profileChanged() {
        status = .error(message: "This server changed or was deleted. Return to the list and select it again.")
    }

    private func handleLogin(_ result: LoginResult, _ profile: Profile) {
        // Keep the current transient browser handoff policy; saved auth is not
        // rewritten, including an explicit NONE choice.
        if InteractiveSso.shouldOpenWebView(result) {
            screen = .web(InteractiveSso.profileForWebView(profile, result: result))
            status = .idle
            return
        }
        switch result {
        case .badCredentials:
            status = .badCredentials(profile)
        case .error(let message), .incompatible(let message):
            status = .error(message: message)
        case .success:
            screen = .web(InteractiveSso.profileForWebView(profile, result: result))
            status = .idle
        case .needsInteractiveSso:
            status = .idle // navigated above
        }
    }
}
