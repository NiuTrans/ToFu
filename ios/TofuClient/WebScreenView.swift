import SwiftUI
import WebKit
import TofuClientCore

/// The hosted Tofu SPA. Port of WebScreen.kt + ReauthWebViewClient.kt — every
/// WebKit hook delegates its DECISION to the tested ``ReauthCoordinator`` /
/// ``SessionManager`` core; this file is only wiring.
///
/// Ported invariants:
///  * three re-auth triggers (main-frame 302→login, main-frame bare 401, the
///    page's own `TofuNative.requestReauth`) funnel into one latched,
///    failure-capped headless re-login, then a reload;
///  * INTERACTIVE_SSO is never intercepted — its sign-in IS main-frame
///    login-page navigation;
///  * a Start hand-off arms `window.tofuStartPending` at document start
///    (one-shot, from UserDefaults) so the page forgives the login bounce
///    that a headless START necessarily causes;
///  * certificates are never auto-proceeded — the user decides, once;
///  * the page learns app foreground/background via `tofu:native-visibility`.
struct WebScreenView: View {
    let profile: Profile
    let onBack: () -> Void

    @StateObject private var store: WebViewStore
    @Environment(\.scenePhase) private var scenePhase

    init(profile: Profile, session: SessionManager, onBack: @escaping () -> Void) {
        self.profile = profile
        self.onBack = onBack
        _store = StateObject(wrappedValue: WebViewStore(
            profile: profile, session: session, onBack: onBack
        ))
    }

    var body: some View {
        ZStack {
            WebViewRepresentable(store: store)
                .ignoresSafeArea()

            if !store.firstLoadDone || store.progress < 1 {
                ProgressView(value: store.progress)
                    .progressViewStyle(.linear)
                    .frame(maxHeight: .infinity, alignment: .top)
                    .ignoresSafeArea()
            }

            chrome

            if let loadError = store.loadError {
                errorCover(loadError)
            } else if !store.firstLoadDone {
                loadingCover
            }

            if store.diagCopied {
                Text("Diagnostics copied — paste to the maintainer")
                    .font(.callout)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(.ultraThinMaterial)
                    .clipShape(Capsule())
                    .frame(maxHeight: .infinity, alignment: .bottom)
                    .padding(.bottom, 32)
                    .transition(.opacity)
            }
        }
        .alert("Unverified certificate", isPresented: $store.tlsPrompt) {
            Button("Proceed once", role: .destructive) { store.answerTls(proceed: true) }
            Button("Cancel", role: .cancel) { store.answerTls(proceed: false) }
        } message: {
            Text("The server's certificate could not be verified. Proceeding trusts it for this load only.")
        }
        .alert("Session expired", isPresented: $store.exhaustedAlert) {
            Button("Back to servers", action: onBack)
        } message: {
            Text("The saved sign-in no longer works (password changed or gateway down). Fix it from the server list.")
        }
        .onChange(of: scenePhase) { store.postVisibility(hidden: $0 != .active) }
    }

    private var chrome: some View {
        VStack {
            HStack {
                Button(action: store.goBackOrExit) {
                    Image(systemName: "chevron.left")
                        .font(.body.weight(.semibold))
                        .frame(width: 36, height: 36)
                        .background(.ultraThinMaterial)
                        .clipShape(Circle())
                }
                .padding(.leading, 12)
                .padding(.top, 54)
                Spacer()
            }
            Spacer()
            HStack {
                Spacer()
                VStack(spacing: 12) {
                    Button(action: store.reload) {
                        Image(systemName: "arrow.clockwise")
                            .frame(width: 44, height: 44)
                            .background(.ultraThinMaterial)
                            .clipShape(Circle())
                    }
                    Button(action: store.runDiagnostics) {
                        Image(systemName: "stethoscope")
                            .frame(width: 44, height: 44)
                            .background(.ultraThinMaterial)
                            .clipShape(Circle())
                    }
                }
                .padding(.trailing, 14)
                .padding(.bottom, 40)
            }
        }
    }

    private var loadingCover: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Opening \(profile.alias)…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
    }

    private func errorCover(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.exclamationmark")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text("Couldn't reach \(profile.alias)")
                .font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            HStack(spacing: 16) {
                Button("Back", action: onBack).buttonStyle(.bordered)
                Button("Retry", action: store.reload).buttonStyle(.borderedProminent)
            }
            .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
    }
}

/// Owns the WKWebView and implements its delegates; every policy decision is
/// delegated to the core. @MainActor: all WK delegate callbacks are main-thread.
@MainActor
final class WebViewStore: NSObject, ObservableObject {

    @Published var progress: Double = 0
    @Published var firstLoadDone = false
    @Published var loadError: String?
    @Published var tlsPrompt = false
    @Published var exhaustedAlert = false
    @Published var diagCopied = false

    let profile: Profile
    private let session: SessionManager
    private let onBack: () -> Void

    /// Set once by the representable; implicitly unwrapped like every
    /// Android `lateinit` WebView ref.
    private(set) var webView: WKWebView!
    private var reauth: ReauthCoordinator!
    private var progressObservation: NSKeyValueObservation?
    private var parkedTls: (URLAuthenticationChallenge, (URLSession.AuthChallengeDisposition, URLCredential?) -> Void)?
    private var reauthTask: Task<Void, Never>?

    init(profile: Profile, session: SessionManager, onBack: @escaping () -> Void) {
        self.profile = profile
        self.session = session
        self.onBack = onBack
        super.init()
        self.reauth = ReauthCoordinator(authType: profile.authType) { [weak self] action in
            Task { @MainActor in self?.handleReauth(action) }
        }
    }

    // MARK: - WebView construction

    func makeWebView() -> WKWebView {
        let config = WKWebViewConfiguration()
        // The SAME default data store WebKitCookieSink writes to — headless
        // login's injected session cookie must be visible here.
        config.websiteDataStore = .default()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        let userContent = WKUserContentController()
        // Bridge shims at document start: the SPA's native-shell contract
        // (window.TofuNative.requestReauth) and the diag return channel
        // (TofuDiag.deliver), both backed by script message handlers.
        var startJs = """
        window.TofuNative={requestReauth:function(r){try{window.webkit.messageHandlers.tofuNative.postMessage({reason:String(r)})}catch(e){}}};
        window.TofuDiag={deliver:function(s){try{window.webkit.messageHandlers.tofuDiag.postMessage(String(s))}catch(e){}}};
        """
        // A Start hand-off parks its armed expectation in UserDefaults; stamp
        // it into the page BEFORE the SPA's scripts run, one-shot.
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        if let raw = UserDefaults.standard.string(forKey: SupervisorBridge.pendingDefaultsKey),
           SupervisorBridge.isArmed(rawPending: raw, nowMs: nowMs),
           let until = SupervisorBridge.parsePending(raw) {
            startJs += SupervisorBridge.armingScript(untilMs: until)
            UserDefaults.standard.removeObject(forKey: SupervisorBridge.pendingDefaultsKey)
        }
        userContent.addUserScript(WKUserScript(
            source: startJs, injectionTime: .atDocumentStart, forMainFrameOnly: true
        ))
        let weakHandler = WeakScriptMessageHandler(delegate: self)
        userContent.add(weakHandler, name: "tofuNative")
        userContent.add(weakHandler, name: "tofuDiag")
        config.userContentController = userContent

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        // Remote-debuggable via Safari on a connected Mac — same posture as
        // Android's setWebContentsDebuggingEnabled(true) for a self-hosted tool.
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        webView.allowsBackForwardNavigationGestures = true

        progressObservation = webView.observe(\.estimatedProgress) { [weak self] wv, _ in
            Task { @MainActor in self?.progress = wv.estimatedProgress }
        }
        self.webView = webView
        loadBaseUrl(into: webView)
        return webView
    }

    private func loadBaseUrl(into webView: WKWebView) {
        guard let url = URL(string: profile.baseUrl) else {
            loadError = "Invalid URL: \(profile.baseUrl)"
            return
        }
        webView.load(URLRequest(url: url))
    }

    // MARK: - Chrome actions

    func goBackOrExit() {
        if webView?.canGoBack == true {
            webView.goBack()
        } else {
            onBack()
        }
    }

    func reload() {
        loadError = nil
        if webView.url != nil {
            webView.reload()
        } else {
            loadBaseUrl(into: webView)
        }
    }

    func postVisibility(hidden: Bool) {
        guard webView != nil else { return }
        let js = "(function(){try{document.dispatchEvent(new CustomEvent("
            + "'tofu:native-visibility',{detail:{hidden:\(hidden)}}));}catch(e){}})()"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    /// The one-click diagnostics FAB: fire the web collector and route its
    /// async JSON back through the TofuDiag shim to the native clipboard —
    /// same snippet as Android, so both shells behave identically on a
    /// wedged page (the failure we diagnose).
    func runDiagnostics() {
        guard webView != nil else { return }
        let js = """
        (function(){
          try {
            if (typeof window.__tofuCollectDiagnostics !== 'function') {
              TofuDiag.deliver('{"error":"diagnostics collector missing — web build predates diag_collect.js; Refresh once on a newer server build"}');
              return;
            }
            Promise.resolve(window.__tofuCollectDiagnostics()).then(
              function(s){ TofuDiag.deliver(String(s)); },
              function(e){ TofuDiag.deliver('{"error":"collector rejected: '+(e&&e.message||e)+'"}'); }
            );
          } catch (e) {
            TofuDiag.deliver('{"error":"collector threw: '+(e&&e.message||e)+'"}');
          }
        })();
        """
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    func answerTls(proceed: Bool) {
        guard let (challenge, handler) = parkedTls else { return }
        parkedTls = nil
        if proceed, let trust = challenge.protectionSpace.serverTrust {
            handler(.useCredential, URLCredential(trust: trust))
        } else {
            handler(.cancelAuthenticationChallenge, nil)
        }
    }

    // MARK: - Re-auth

    private func handleReauth(_ action: ReauthCoordinator.Action) {
        switch action {
        case .reauthStarted:
            reauthTask?.cancel()
            reauthTask = Task { [weak self] in
                guard let self else { return }
                let result = await self.session.login(self.profile)
                guard !Task.isCancelled else { return }
                switch result {
                case .success, .needsInteractiveSso:
                    // success: fresh cookie is in the jar — reload picks it up.
                    // needsInteractiveSso: the gate turned SSO on; reload lets
                    // the page run that flow (never intercepted).
                    self.reauth.settle(succeeded: true)
                    self.webView?.reload()
                default:
                    self.reauth.settle(succeeded: false)
                }
            }
        case .exhausted:
            exhaustedAlert = true
        }
    }
}

// MARK: - WKNavigationDelegate

extension WebViewStore: WKNavigationDelegate {

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        // window.open / target=_blank: the shell has no tabs — hand to the system.
        if navigationAction.targetFrame == nil {
            if url.scheme == "http" || url.scheme == "https" {
                UIApplication.shared.open(url)
            }
            decisionHandler(.cancel)
            return
        }
        let verdict = reauth.navigationVerdict(
            url: url.absoluteString,
            isMainFrame: navigationAction.targetFrame?.isMainFrame ?? false,
            hasGesture: navigationAction.navigationType == .linkActivated,
            ownHost: ServerUrl.parse(profile.baseUrl)?.host
        )
        switch verdict {
        case .allow:
            decisionHandler(.allow)
        case .interceptForReauth:
            // trigger() already fired inside navigationVerdict.
            decisionHandler(.cancel)
        case .openExternally:
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        // The edge session can die UNDER the page: a bare main-frame 401 is
        // the third re-auth trigger (latched — fires once).
        if let http = navigationResponse.response as? HTTPURLResponse,
           navigationResponse.isForMainFrame, http.statusCode == 401 {
            reauth.trigger()
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        firstLoadDone = true
        loadError = nil
        // Interactive-SSO stamping: the browser engine set the cookie, so the
        // profile's cookieHost can only be learned by observing the finished
        // page (see SessionManager.noteInteractiveSignIn).
        if let finished = webView.url?.absoluteString {
            Task { await session.noteInteractiveSignIn(profile, finishedUrl: finished) }
        }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        let nsError = error as NSError
        guard nsError.domain == NSURLErrorDomain, nsError.code != NSURLErrorCancelled else { return }
        loadError = nsError.localizedDescription
    }

    func webView(
        _ webView: WKWebView,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        // Never auto-proceed: park the challenge and let the user decide once.
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           challenge.protectionSpace.serverTrust != nil {
            parkedTls = (challenge, completionHandler)
            tlsPrompt = true
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        // Same posture as Android's onRenderProcessGone: the page is gone;
        // drop to the list rather than sit on a dead surface.
        onBack()
    }
}

// MARK: - WKUIDelegate

extension WebViewStore: WKUIDelegate {

    /// getUserMedia (voice input): Tofu requests the mic only on explicit user
    /// action, so granting inside the hosted app matches the browser default.
    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        decisionHandler(.grant)
    }
}

// MARK: - WKScriptMessageHandler

extension WebViewStore: WKScriptMessageHandler {

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        switch message.name {
        case "tofuNative":
            // The page saw the outer gateway's bare 401 on its API transport.
            reauth.trigger()
        case "tofuDiag":
            guard let json = message.body as? String else { return }
            UIPasteboard.general.string = json
            withAnimation { diagCopied = true }
            Task {
                try? await Task.sleep(nanoseconds: 2_500_000_000)
                withAnimation { diagCopied = false }
            }
        default:
            break
        }
    }
}

/// WKUserContentController RETAINS its message handlers; a proxy keeps the
/// store out of that cycle.
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?
    init(delegate: WKScriptMessageHandler) { self.delegate = delegate }
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        delegate?.userContentController(userContentController, didReceive: message)
    }
}

private struct WebViewRepresentable: UIViewRepresentable {
    let store: WebViewStore

    func makeUIView(context: Context) -> WKWebView {
        store.makeWebView()
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
