import SwiftUI
import TofuClientCore
import WebKit

/// Add / edit a server. Port of AddEditScreen.kt — the same invariants:
///
///  * validation is the pure ``ProfileForm``; the view only renders errors;
///  * a freshly typed URL picks its sensible auth default
///    (``ServerUrl/defaultAuthType``) until the user touches the picker;
///  * code-server auth is per-HOST, so a blank password on a host that
///    already has one saved is not an error — the form says so proactively;
///  * "Test connection" is the ``HealthProbe`` — behind the vscode proxy it
///    must report GATEWAY vs NOT-TOFU vs UNREACHABLE honestly, because a bare
///    401 there means "the edge refused", never "wrong URL".
struct AddEditProfileView: View {
    @ObservedObject var model: AppViewModel

    @State private var alias: String
    @State private var url: String
    @State private var auth: AuthType
    @State private var secret: String
    @State private var managed: Bool
    @State private var projectPath: String
    /// The user picked an auth mode explicitly — stop auto-deriving from URL.
    @State private var authTouched: Bool
    @State private var errors: [String: String] = [:]
    @State private var testing = false
    @State private var verdict: TofuProbe.Verdict?
    @State private var probeTarget: String?
    /// Host whose saved password will be reused for a blank field, if any.
    @State private var reusableHost: String?

    init(model: AppViewModel) {
        self.model = model
        let editing = model.editing
        let draft = model.onboardingDraft
        _alias = State(initialValue: editing?.alias ?? draft?.alias ?? "")
        _url = State(initialValue: editing?.baseUrl ?? draft?.baseUrl ?? "")
        _auth = State(initialValue: editing?.authType ?? draft?.authType ?? .none)
        _secret = State(initialValue: "")
        _managed = State(initialValue: editing?.projectPath?.isEmpty == false)
        _projectPath = State(initialValue: editing?.projectPath ?? "")
        _authTouched = State(initialValue: editing != nil)
    }

    private var editing: Profile? { model.editing }
    private var isBusy: Bool {
        if case .loggingIn = model.status { return true }
        return false
    }

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(status: model.status, onCancel: model.cancelOperation)
            Form {
                serverSection
                authSection
                if auth == .codeServerPassword { secretSection }
                manageSection
                probeSection
            }
        }
        .navigationTitle(editing == nil ? "Add server" : "Edit server")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel", action: model.backToList)
            }
            ToolbarItem(placement: .confirmationAction) {
                if isBusy {
                    ProgressView()
                } else {
                    Button("Save", action: submit)
                        .fontWeight(.semibold)
                        .disabled(model.pendingConnection != nil)
                }
            }
        }
        .onChange(of: url) { newUrl in
            if !authTouched { auth = ServerUrl.defaultAuthType(newUrl) }
            verdict = nil // a probe result belongs to the URL it measured
            probeTarget = nil
            testing = false
            reusableHost = nil
        }
        .task(id: url + "|" + (editing?.alias ?? "")) {
            let target = url
            let excludeAlias = editing?.alias
            reusableHost = nil
            let host = await model.reusableSecretHost(target, excludeAlias: excludeAlias)
            guard !Task.isCancelled, url == target, editing?.alias == excludeAlias else { return }
            reusableHost = host
        }
        .task(id: probeTarget) {
            guard let target = probeTarget, !Task.isCancelled, url == target else { return }
            let probe = HealthProbe(
                http: URLSessionHTTPClient(timeoutSeconds: 8),
                cookies: WebKitCookieSink()
            )
            let outcome = await probe.probe(target)
            guard !Task.isCancelled, probeTarget == target, url == target else { return }
            verdict = outcome.verdict
            testing = false
            probeTarget = nil
        }
        .onDisappear {
            secret = ""
            probeTarget = nil
        }
    }

    private var serverSection: some View {
        Section {
            TextField("Name (e.g. codelab training)", text: editable($alias))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            FieldError(errors["alias"])
            TextField("Server URL", text: editable($url))
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            FieldError(errors["baseUrl"])
            Button("Paste connection URL or tofu://connect link") {
                guard let text = UIPasteboard.general.string else {
                    errors["baseUrl"] = "Paste a full URL without embedded credentials"
                    return
                }
                // The same policy as OS/QR links: never overwrite an open form.
                model.receiveConnection(text)
            }
            Text("Paste the full address, e.g. https://…-vscode-….mlp.internal.example.com/proxy/15000/")
                .font(.caption)
                .foregroundStyle(.secondary)
        } header: {
            Text("Server")
        }
    }

    private var authSection: some View {
        Section {
            ForEach(AuthType.allCases, id: \.self) { type in
                Button {
                    model.formDidChange()
                    auth = type
                    authTouched = true
                    if type != .codeServerPassword { secret = "" }
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: auth == type ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(Color.accentColor)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title(for: type)).foregroundStyle(.primary)
                            Text(subtitle(for: type))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        } header: {
            Text("Sign-in")
        } footer: {
            if !authTouched, editing == nil {
                Text("Picked from the URL — /proxy/ addresses sit behind the code-server password gate.")
            }
        }
    }

    private var secretSection: some View {
        Section {
            SecureField(secretPlaceholder, text: editable($secret))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            FieldError(errors["secret"])
            if secret.isEmpty, let reusableHost {
                Label("The password saved for \(reusableHost) will be reused — code-server auth is per host.",
                      systemImage: "key.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Password")
        }
    }

    private var manageSection: some View {
        Section {
            Toggle("Start/stop this server here", isOn: editable($managed))
            if managed {
                TextField("Project path on the host", text: editable($projectPath))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Text("Absolute path the supervisor runs start.sh/stop.sh from, e.g. /home/user/tofu")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Manage (optional)")
        }
    }

    private var probeSection: some View {
        Section {
            Button {
                testConnection()
            } label: {
                HStack(spacing: 8) {
                    if testing {
                        ProgressView().controlSize(.small)
                        Text("Testing…")
                    } else {
                        Image(systemName: "antenna.radiowaves.left.and.right")
                        Text("Test connection")
                    }
                }
            }
            .disabled(testing || ServerUrl.parse(url) == nil)

            if let verdict {
                let hasSecret = !secret.isEmpty || model.secretStoredFor(editing?.alias ?? alias)
                Label(TofuProbe.guidance(verdict, authType: auth, hasSecret: hasSecret),
                      systemImage: TofuProbe.isProblem(verdict, authType: auth, hasSecret: hasSecret)
                        ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(TofuProbe.isProblem(verdict, authType: auth, hasSecret: hasSecret)
                        ? Color.orange : Color.green)
            }
        }
    }

    private var secretPlaceholder: String {
        if let editing, model.secretStoredFor(editing.alias) {
            return "Saved — leave blank to keep"
        }
        return "code-server password"
    }

    /// Only actual control edits invalidate sign-in. Programmatic password
    /// disposal must not cancel the operation that just consumed that field.
    private func editable<Value>(_ binding: Binding<Value>) -> Binding<Value> {
        Binding(get: { binding.wrappedValue }, set: { value in
            model.formDidChange()
            binding.wrappedValue = value
        })
    }

    private func submit() {
        guard model.pendingConnection == nil else { return }
        let result = ProfileForm.validate(
            alias: alias,
            baseUrl: url,
            authType: auth,
            secret: secret,
            existingAliases: Set(model.profiles.map(\.alias)),
            editingAlias: editing?.alias,
            secretAlreadyStored: (editing.map { model.secretStoredFor($0.alias) } ?? false) || reusableHost != nil
        )
        errors = result.errors
        guard result.ok else { return }
        let path = managed ? projectPath : ""
        let submittedSecret = secret
        secret = "" // the form draft never retains a submitted password
        if let editing {
            model.submitEdit(current: editing, alias: alias, url: url, auth: auth,
                             secret: submittedSecret, projectPath: path)
        } else {
            model.submitAdd(alias: alias, url: url, auth: auth, secret: submittedSecret, projectPath: path)
        }
    }

    private func testConnection() {
        testing = true
        verdict = nil
        // SwiftUI cancels this probe on URL change/replacement/disappearance.
        probeTarget = url
    }

    private func title(for type: AuthType) -> String {
        switch type {
        case .codeServerPassword: return "code-server password"
        case .interactiveSso: return "Sign in in the app (SSO)"
        case .none: return "No sign-in"
        }
    }

    private func subtitle(for type: AuthType) -> String {
        switch type {
        case .codeServerPassword:
            return "The /proxy/ address is gated by a password; the app replays it for you."
        case .interactiveSso:
            return "Complete your organization's sign-in once; the session is remembered."
        case .none:
            return "The server is directly reachable."
        }
    }
}

/// Inline validation message under a field; renders nothing when nil.
private struct FieldError: View {
    let text: String?
    init(_ text: String?) { self.text = text }
    var body: some View {
        if let text {
            Text(text)
                .font(.caption)
                .foregroundStyle(Color.red)
        }
    }
}
