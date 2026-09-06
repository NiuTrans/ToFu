package com.tofu.client.session

import com.tofu.client.data.Profile

/**
 * Pure (Android-free) model of a server's lifecycle state, and the rules for
 * which controls are legal in each state.
 *
 * This exists because "can the app run the server itself?" is the feature the
 * whole Start/Stop surface answers, and until now that logic lived inline in a
 * Composable — untestable, and the reason the list could never show a server's
 * state without the user tapping Refresh. Keeping it pure means the state
 * machine is unit-tested off-device while the Composable stays a thin renderer.
 */
enum class ServerState {
    /** No projectPath configured — this profile is open-only. */
    UNMANAGED,

    /**
     * Managed, but we have no authoritative answer yet — either we haven't
     * polled, or we hold no session cookie. These are deliberately the SAME
     * state: a missing cookie does not restrict what the user may do, because
     * the supervisor rides the code-server session and code-server (the proxy)
     * stays up while Tofu is down, so any control can do login-then-act.
     *
     * An earlier version modelled "no cookie" as a separate LOCKED state with
     * Start disabled. That deadlocked: a stopped server has no cookie (Open
     * cannot succeed against it), so Start stayed greyed forever — exactly the
     * chicken-and-egg the supervisor exists to break.
     */
    UNKNOWN,

    RUNNING,
    STOPPED,

    /** A start/stop is in flight, or we're polling for the port to bind. */
    TRANSITIONING,

    /** The last supervisor call failed (daemon down, path not allow-listed…). */
    UNREACHABLE,
}

/** What the UI may offer in a given [ServerState]. */
data class ServerCapabilities(
    val canStart: Boolean,
    val canStop: Boolean,
    val canRefresh: Boolean,
    /** Opening the WebView is pointless while the server is known-stopped. */
    val canOpen: Boolean,
)

/**
 * What caused a supervisor call. This is NOT cosmetic: the two triggers have
 * fundamentally different licence to cause side effects.
 *
 * A USER tap is an explicit request, so signing in on the user's behalf is
 * expected and a failure deserves a visible error. An AUTO probe happens on
 * every composition with nobody asking, so it must stay READ-ONLY — it may not
 * spend a `POST /login`, and its failure may not paint the card red.
 */
enum class ProbeTrigger { AUTO, USER }

/**
 * Whether a supervisor call may log in first, and whether its failure may be
 * surfaced. Derived by [ServerLifecycle.probePlan].
 */
data class ProbePlan(
    /** Run the call at all. False = skip silently. */
    val proceed: Boolean,
    /** Allowed to `POST /login` when no cookie is held. */
    val mayLogIn: Boolean,
    /** Allowed to set the failed flag / show an error message. */
    val reportFailure: Boolean,
)

/**
 * Which supervisor call to make. An enum rather than a string so the dispatch
 * `when` is exhaustive — a stringly-typed action silently degraded any typo to
 * a harmless-looking `/status` call.
 */
enum class SupervisorAction { START, STOP, STATUS }

/**
 * What a finished call should do to the UI. Kept pure so the "don't yank the
 * user into a WebView they navigated away from" rule is testable off-device.
 */
data class CallCompletion(
    /** Hand the user into the WebView (a start that reached RUNNING). */
    val handOff: Boolean,
    /** Show the "still booting" copy (a start whose poll window expired). */
    val showTimeout: Boolean,
)

object ServerLifecycle {

    /** True when [profile] opted into supervisor control by setting a project path. */
    fun isManaged(profile: Profile): Boolean = !profile.projectPath.isNullOrBlank()

    /**
     * Whether the profile currently holds a session cookie valid for its OWN
     * host. A stale cookieHost from a previous URL must NOT count as signed in.
     *
     * This gates whether an action needs a login handshake FIRST — it does not
     * gate whether the action is allowed at all (see [capabilities]).
     */
    fun isSignedIn(profile: Profile): Boolean {
        val host = ServerUrl.parse(profile.baseUrl)?.host ?: return false
        return profile.cookieHost == host
    }

    /**
     * Resolve the displayable state from the profile plus the last known poll
     * result. [running] is null when never polled; [busy] marks an in-flight
     * call; [failed] marks the last call having errored.
     */
    fun resolve(
        profile: Profile,
        running: Boolean?,
        busy: Boolean = false,
        failed: Boolean = false,
    ): ServerState = when {
        !isManaged(profile) -> ServerState.UNMANAGED
        busy -> ServerState.TRANSITIONING
        failed -> ServerState.UNREACHABLE
        // A poll result OUTRANKS the cookie check: if the supervisor answered,
        // we demonstrably reached it, so report the truth. Without this, a
        // login-then-act that succeeds still renders LOCKED until Room re-emits
        // the profile carrying the freshly-stamped cookieHost.
        running == true -> ServerState.RUNNING
        running == false -> ServerState.STOPPED
        else -> ServerState.UNKNOWN
    }

    fun capabilities(state: ServerState): ServerCapabilities = when (state) {
        // An unmanaged profile has no supervisor at all — Open is all there is.
        ServerState.UNMANAGED ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = true)
        // No authoritative state yet (never polled, or no session cookie).
        // Every control stays live: the supervisor rides the code-server
        // session, and code-server — the PROXY — is up even while Tofu is down,
        // so `POST /login` succeeds regardless and a tap can log in then act.
        ServerState.UNKNOWN ->
            ServerCapabilities(canStart = true, canStop = true, canRefresh = true, canOpen = true)
        ServerState.RUNNING ->
            ServerCapabilities(canStart = false, canStop = true, canRefresh = true, canOpen = true)
        // Deliberately still openable when stopped: opening is how a user
        // discovers the server is down, and the WebView shows the failure.
        ServerState.STOPPED ->
            ServerCapabilities(canStart = true, canStop = false, canRefresh = true, canOpen = true)
        // A start/stop is in flight. Open stays ENABLED on purpose: a start
        // poll can outlast the window, and taking Open away would leave the
        // user with no actionable control at all — the "looks like a deadlock"
        // shape we just removed elsewhere. Opening mid-start is harmless; the
        // WebView shows the server's own state.
        ServerState.TRANSITIONING ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = true)
        ServerState.UNREACHABLE ->
            ServerCapabilities(canStart = true, canStop = true, canRefresh = true, canOpen = true)
    }

    /**
     * Decide what a supervisor call is permitted to do, given who asked.
     *
     * The rule that matters: an AUTO probe against a profile with NO session is
     * SKIPPED entirely. Letting it run would mean that merely opening the home
     * screen fires one `POST /login` per unsigned server — a burst of logins
     * nobody requested, which on a bad password is an auto-retry loop toward
     * account lockout, and on an SSO profile can never succeed at all, so the
     * card would paint itself red on every cold start while the server is
     * perfectly healthy. "Not signed in yet" is not "unreachable".
     *
     * A USER tap always proceeds and always may log in: that is the
     * login-then-act path which makes a stopped server startable.
     */
    fun probePlan(trigger: ProbeTrigger, signedIn: Boolean): ProbePlan = when (trigger) {
        ProbeTrigger.USER -> ProbePlan(
            proceed = true,
            mayLogIn = true,
            reportFailure = true,
        )
        // Read-only by construction: with a cookie we can ask /status for free;
        // without one there is nothing to ask with, so we don't ask at all and
        // leave the card in UNKNOWN ("Tap to check").
        ProbeTrigger.AUTO -> ProbePlan(
            proceed = signedIn,
            mayLogIn = false,
            reportFailure = false,
        )
    }

    /**
     * Decide what a COMPLETED call does to the UI.
     *
     * [stillCurrent] is the guard that matters: a start poll runs for up to
     * [startPollWindowSeconds], and the user can navigate away or edit the
     * profile while it runs. Handing them into a WebView at that point would
     * yank them somewhere they didn't ask to go — possibly for a server they
     * just navigated away from. So a stale completion does nothing at all.
     *
     * Only a START can hand off or time out; STOP and STATUS never do either.
     */
    fun completionFor(
        action: SupervisorAction,
        running: Boolean,
        stillCurrent: Boolean,
    ): CallCompletion {
        if (!stillCurrent || action != SupervisorAction.START) {
            return CallCompletion(handOff = false, showTimeout = false)
        }
        return CallCompletion(handOff = running, showTimeout = !running)
    }

    /**
     * How long to wait for `server.py` to bind its port after `/start`. The
     * supervisor returns immediately by design, so the caller polls. Cold
     * starts (imports, model warmup) routinely exceed a few seconds, so the
     * window is deliberately generous — and, critically, EXPIRING IT IS NOT AN
     * ERROR: see [startTimeoutMessage].
     */
    const val START_POLL_ATTEMPTS = 15
    const val START_POLL_INTERVAL_MS = 2_000L

    /** Total start-poll window in seconds, for user-facing copy. */
    val startPollWindowSeconds: Int
        get() = (START_POLL_ATTEMPTS * START_POLL_INTERVAL_MS / 1000).toInt()

    /**
     * Shown when the start poll window expires without the port coming up.
     *
     * This is explicitly NOT phrased as a failure: `/start` was accepted, the
     * server is probably still booting. What matters is that the user is left
     * with something to DO — stranding them on a spinner with every control
     * disabled is the same dead end as the old greyed-Start bug.
     */
    fun startTimeoutMessage(): String =
        "Started, but the server hasn't answered in ${startPollWindowSeconds}s — " +
            "it may still be booting. Tap Check again, or Open to watch it come up."

    /**
     * True when a login outcome BLOCKS the supervisor call that follows it.
     *
     * [LoginResult.NeedsInteractiveSso] counts as blocking: it yields no cookie,
     * so proceeding would 401 and report "the daemon isn't responding" — blaming
     * the host for what is actually an un-completed sign-in.
     */
    fun isLoginBlocking(result: LoginResult): Boolean = when (result) {
        is LoginResult.Success -> false
        is LoginResult.BadCredentials -> true
        is LoginResult.NoCredential -> true
        is LoginResult.NeedsInteractiveSso -> true
        is LoginResult.Error -> true
        is LoginResult.Incompatible -> true
    }

    /**
     * Why a login-then-act attempt could not reach the supervisor. Exhaustive
     * over [LoginResult] — no `else` branch, so a new outcome added later is a
     * COMPILE error here rather than a silently generic "couldn't sign in".
     */
    fun explainLoginBlock(result: LoginResult): String = when (result) {
        is LoginResult.BadCredentials ->
            "Wrong password for this server — edit it and try again."
        is LoginResult.NoCredential ->
            "No saved password for this server, so it can't be controlled from here."
        // The ONE case where Open genuinely is the answer: interactive SSO
        // cannot be replayed headlessly, so the user must complete it once in
        // the WebView before start/stop can work.
        is LoginResult.NeedsInteractiveSso ->
            "This server needs an interactive sign-in first — tap Open, sign in " +
                "once, then Start and Stop will work from here."
        is LoginResult.Error ->
            "Can't reach this server: ${result.message}"
        is LoginResult.Incompatible ->
            result.message
        is LoginResult.Success ->
            "Signed in."   // not a block; never surfaced
    }

    /** Short status word for the state chip. */
    fun label(state: ServerState): String = when (state) {
        ServerState.UNMANAGED -> "Open only"
        ServerState.UNKNOWN -> "Tap to check"
        ServerState.RUNNING -> "Running"
        ServerState.STOPPED -> "Stopped"
        ServerState.TRANSITIONING -> "Working…"
        ServerState.UNREACHABLE -> "Unreachable"
    }
}
