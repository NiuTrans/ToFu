import SwiftUI
import TofuClientCore

/// App entry point — the MainActivity analogue. Builds the dependency graph
/// (SQLite store + Keychain + WebKit cookie sink + URLSession transport +
/// SessionManager + SessionController) and hands it to one ``AppViewModel``.
/// The SPA is the renderer; the shell stays thin.
@main
struct TofuClientApp: App {

    @StateObject private var model: AppViewModel

    init() {
        let store = SQLiteProfileStore.openDefault()
        let secrets = KeychainSecretStore()
        // The sink and every WKWebView MUST share the same data store —
        // .default() — or the headless login injects into a jar the page
        // never reads.
        let cookies = WebKitCookieSink(dataStore: .default())
        let session = SessionManager(
            store: store,
            secrets: secrets,
            cookies: cookies,
            http: URLSessionHTTPClient()
        )
        let controller = SessionController(store: store, secrets: secrets, session: session)
        _model = StateObject(wrappedValue: AppViewModel(
            store: store,
            secrets: secrets,
            session: session,
            controller: controller
        ))
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .task { await model.migrateOnLaunch() }
        }
    }
}
