import SwiftUI
import TofuClientCore

extension SQLiteProfileStore {
    /// The store at its on-device location (Application Support), creating
    /// the directory on first launch. Kept out of the core so `swift test`
    /// never touches a filesystem layout.
    static func openDefault() -> SQLiteProfileStore {
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        )[0]
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        let path = support.appendingPathComponent("tofu-profiles.db").path
        do {
            return try SQLiteProfileStore(path: path)
        } catch {
            // A store that cannot open must not wedge the shell behind a
            // crash loop: fall back to an in-memory database so the app
            // still runs (profiles just won't persist this launch).
            return try! SQLiteProfileStore(path: ":memory:")
        }
    }
}

/// Root navigation switch — the MainActivity setContent analogue.
struct RootView: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        VStack(spacing: 0) {
            if let pending = model.pendingConnection {
                VStack(alignment: .leading, spacing: 8) {
                    Text("A connection link is ready to review.").font(.headline)
                    Text(ServerUrl.displayLabel(pending.baseUrl))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text("Use link replaces this form, including its unsaved password. Nothing is saved or signed in until you choose Save.")
                        .font(.caption)
                    HStack {
                        Button("Use link", action: model.usePendingConnection)
                        Button("Keep form", action: model.keepCurrentForm)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(Color.accentColor.opacity(0.10))
            }
            // Pending metadata never changes the form identity. Only explicit
            // Use link, Add or Edit replaces its local state.
            switch model.screen {
            case .list:
                ProfileListView(model: model)
            case .addEdit:
                AddEditProfileView(model: model).id(model.formRevision)
            case .web(let profile):
                WebScreenView(
                    profile: profile,
                    session: model.session,
                    onBack: model.backToList
                )
            }
        }
    }
}
