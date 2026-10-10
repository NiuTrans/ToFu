package com.tofu.client.session

import com.tofu.client.data.Profile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * The network half of the update check: one `GET {base}/api/health`, whose
 * `mobile_client_version` / `mobile_client_url` fields advertise the newest
 * published Android client. Pure of Android APIs (only OkHttp), so it is
 * exercised by the fast JDK tier via an injected interceptor.
 *
 * `/api/health` is a public liveness path, so this needs no credential and
 * works even for a server the user has not signed in to yet.
 */
class UpdateProbe(http: OkHttpClient = defaultClient(), private val diagnostics: DiagLog? = null) {
    private val http = DiagnosticHttp.attach(http, diagnostics, "UpdateHttp")

    /** Probe one server; never throws, returns null when unavailable/absent. */
    fun advertised(baseUrl: String): AdvertisedClient? {
        val server = ServerUrl.parse(baseUrl) ?: return null
        val request = Request.Builder().url(server.endpoint("api/health")).get().build()
        return try {
            http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return null
                UpdateCheck.parseAdvertised(response.body?.string())
            }
        } catch (e: Exception) {
            diagnostics?.exception("Update", e)
            null
        }
    }

    companion object {
        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            // Short: this runs in the background on list open and must never
            // stall the UI on a dead server.
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(4, TimeUnit.SECONDS)
            .build()
    }
}

/**
 * Compares the installed app against every server the user has, keeping the
 * newest advertised version. Probing all of them (rather than guessing "the"
 * server) matters because a user can point at a stale sandbox while their
 * primary server carries the released client.
 *
 * The [probe] seam is a plain function so the aggregation is unit-testable with
 * no network; production passes [UpdateProbe].
 */
class UpdateChecker(
    private val installedName: String,
    private val probe: (String) -> AdvertisedClient? = UpdateProbe()::advertised,
) {
    /** Returns null when there are no profiles to ask. Never throws. */
    suspend fun check(profiles: List<Profile>): UpdateStatus? = withContext(Dispatchers.IO) {
        if (profiles.isEmpty()) return@withContext null
        // Probe every server concurrently: N servers each with a 4s timeout
        // would otherwise cost N×4s on a cold or dead fleet. The profile count
        // is small (a user's own servers), so unbounded fan-out here is bounded
        // in practice.
        val advertised = coroutineScope {
            profiles.map { profile ->
                async {
                    try {
                        probe(profile.baseUrl)
                    } catch (e: Exception) {
                        null
                    }
                }
            }.awaitAll()
        }
        var best: AdvertisedClient? = null
        for (candidate in advertised) {
            if (candidate == null) continue
            val current = best
            if (current == null ||
                UpdateCheck.compareVersions(candidate.version, current.version) > 0
            ) {
                best = candidate
            }
        }
        UpdateCheck.evaluate(installedName, best)
    }
}
