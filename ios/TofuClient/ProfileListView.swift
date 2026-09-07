import SwiftUI
import TofuClientCore

/// Home — the server list / switcher. Port of ProfileListScreen.kt.
struct ProfileListView: View {
    @ObservedObject var model: AppViewModel
    /// Per-profile lifecycle state, lifted here so the header can summarize
    /// how many servers are up without each card re-polling.
    @State private var states: [Int64: ServerState] = [:]

    var body: some View {
        VStack(spacing: 0) {
            header
            StatusBanner(status: model.status)
            if model.profiles.isEmpty {
                emptyState
            } else {
                List {
                    ForEach(model.profiles) { profile in
                        ServerCardView(
                            profile: profile,
                            session: model.session,
                            onActivate: { model.activate(profile) },
                            onEdit: { model.startEdit(profile) },
                            onDelete: { model.deleteProfile(profile) },
                            onStateChange: { states[profile.id] = $0 }
                        )
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                    }
                }
                .listStyle(.plain)
            }
            versionFooter
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: model.startAdd) {
                    Label("Add server", systemImage: "plus")
                }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 9) {
                Text("Tofu").font(.title2.weight(.semibold))
                Text("v\(Self.appVersion)")
                    .font(.caption)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(Color.accentColor.opacity(0.14))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            }
            Text(summaryLine)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.top, 20).padding(.bottom, 14)
    }

    private var summaryLine: String {
        let count = model.profiles.count
        let running = states.values.filter { $0 == .running }.count
        if count == 0 { return "No servers yet" }
        let noun = count == 1 ? "server" : "servers"
        return running > 0 ? "\(count) \(noun) · \(running) running" : "\(count) \(noun)"
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Image(systemName: "server.rack")
                .font(.system(size: 32))
                .foregroundStyle(Color.accentColor)
                .frame(width: 72, height: 72)
                .background(Color.accentColor.opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: 22))
                .padding(.bottom, 12)
            Text("Your servers live here").font(.title3.weight(.semibold))
            Text(
                "Add a Tofu server once — its address and password are remembered, " +
                "so opening it later is a single tap."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            Button(action: model.startAdd) {
                Label("Add your first server", systemImage: "plus")
            }
            .buttonStyle(.bordered)
            .padding(.top, 16)
            Spacer()
        }
        .padding(.horizontal, 40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var versionFooter: some View {
        Text("Tofu v\(Self.appVersion)")
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .frame(maxWidth: .infinity)
            .padding(.bottom, 10)
    }

    static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }
}

/// The login progress / error banner. Port of StatusBanner.
struct StatusBanner: View {
    let status: UiStatus

    var body: some View {
        if let text, text.isEmpty == false {
            Text(text)
                .font(.callout)
                .foregroundStyle(isError ? Color.red : Color.accentColor)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14).padding(.vertical, 11)
                .background((isError ? Color.red : Color.accentColor).opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 16).padding(.vertical, 4)
        }
    }

    private var text: String? {
        switch status {
        case .idle: return nil
        case .loggingIn(let alias): return "Signing in to \(alias)…"
        case .error(let message): return message
        case .badCredentials(let profile):
            return "Wrong password for \(profile.alias) — edit it to fix."
        }
    }

    private var isError: Bool {
        if case .idle = status { return false }
        if case .loggingIn = status { return false }
        return true
    }
}

/// One server. The whole card is the Open target; secondary actions sit in
/// the overflow menu; supervisor controls appear inline only for managed
/// servers. Port of ServerCard.
struct ServerCardView: View {
    let profile: Profile
    let session: SessionManager
    let onActivate: () -> Void
    let onEdit: () -> Void
    let onDelete: () -> Void
    let onStateChange: (ServerState) -> Void

    @State private var state: ServerState
    @State private var confirmDelete = false

    init(
        profile: Profile,
        session: SessionManager,
        onActivate: @escaping () -> Void,
        onEdit: @escaping () -> Void,
        onDelete: @escaping () -> Void,
        onStateChange: @escaping (ServerState) -> Void
    ) {
        self.profile = profile
        self.session = session
        self.onActivate = onActivate
        self.onEdit = onEdit
        self.onDelete = onDelete
        self.onStateChange = onStateChange
        _state = State(initialValue: ServerLifecycle.resolve(profile: profile, running: nil))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                MonogramTile(alias: profile.alias)
                VStack(alignment: .leading, spacing: 2) {
                    Text(profile.alias).font(.headline).lineLimit(1)
                    Text(ServerUrl.displayLabel(profile.baseUrl))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                if ServerLifecycle.isManaged(profile) {
                    StatusChip(state: state)
                }
                Menu {
                    Button("Edit", action: onEdit)
                    Button("Delete", role: .destructive) { confirmDelete = true }
                } label: {
                    Image(systemName: "ellipsis")
                        .foregroundStyle(.secondary)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
            }

            if ServerLifecycle.isManaged(profile) {
                Divider().padding(.vertical, 10)
                SupervisorControlsView(
                    profile: profile,
                    session: session,
                    onStateChange: { state = $0; onStateChange($0) },
                    onServerReady: onActivate
                )
            }
        }
        .padding(14)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .contentShape(RoundedRectangle(cornerRadius: 16))
        .onTapGesture(perform: onActivate)
        .confirmationDialog(
            "Delete \(profile.alias)?",
            isPresented: $confirmDelete,
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive, action: onDelete)
            Button("Cancel", role: .cancel) {}
        }
    }
}

/// Colored monogram tile — the identity anchor a URL-heavy row lacks.
struct MonogramTile: View {
    let alias: String

    var body: some View {
        Text(String(alias.prefix(1)).uppercased())
            .font(.headline)
            .foregroundStyle(Color.accentColor)
            .frame(width: 40, height: 40)
            .background(Color.accentColor.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}

/// Live status chip. Port of StatusChip (Components.kt).
struct StatusChip: View {
    let state: ServerState

    var body: some View {
        Text(ServerLifecycle.label(state))
            .font(.caption2.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(color.opacity(0.12))
            .clipShape(Capsule())
    }

    private var color: Color {
        switch state {
        case .running: return .green
        case .stopped: return .orange
        case .transitioning: return .accentColor
        case .unreachable: return .red
        case .unmanaged, .unknown: return .secondary
        }
    }
}
