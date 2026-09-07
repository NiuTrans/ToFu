#!/usr/bin/env bash
# Run the unit tests WITHOUT the full Android/Gradle toolchain.
#
# Two tiers, both on a plain JDK 17 + kotlinc (the real CI target is still
# `./gradlew test`, which needs the Android SDK):
#   1. PURE-JVM  — ServerUrl, LoginForm, CookieHeaders, SessionManager (via the
#      CookieSink / SecretLookup seams). No Android runtime.
#   2. ROBOLECTRIC — CookieBridge against a shadow CookieManager + the
#      ReauthWebViewClient latch. Runs headless, no device. SKIPPED unless the
#      instrumented android-all jar is present in $LIBS.
#
# Prereqs (set via env): JAVA_HOME (JDK 17), KOTLINC (kotlinc bin),
# LIBS (dir holding the runtime jars). See README.md for how to fetch them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/app/src"
: "${JAVA_HOME:?set JAVA_HOME to a JDK 17}"
: "${KOTLINC:?set KOTLINC to the kotlinc launcher}"
: "${LIBS:?set LIBS to the dir with the runtime jars}"
KRT="$(cd "$(dirname "$KOTLINC")/.." && pwd)/lib/kotlin-stdlib.jar"

OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
CP="$(ls "$LIBS"/okhttp-*.jar "$LIBS"/okio-*.jar "$LIBS"/kotlinx-coroutines-core-*.jar \
        "$LIBS"/kotlinx-coroutines-test-*.jar "$LIBS"/room-common-*.jar \
        "$LIBS"/junit-4.13*.jar "$LIBS"/hamcrest-core-*.jar "$LIBS"/annotations-2*.jar | tr '\n' ':')$OUT"

# android.util.Log stub (SessionManager's only Android dependency in the pure tier).
cat > "$OUT/Log.java" <<'EOF'
package android.util;
public final class Log {
  public static int i(String t,String m){return 0;} public static int w(String t,String m){return 0;}
  public static int e(String t,String m){return 0;} public static int d(String t,String m){return 0;}
}
EOF
"$JAVA_HOME/bin/javac" -d "$OUT" "$OUT/Log.java"

# JVM no-op CookieBridge so the SessionManager default param resolves off-device.
mkdir -p "$OUT/stub"
cat > "$OUT/stub/CookieBridgeStub.kt" <<'EOF'
package com.tofu.client.session
import okhttp3.Cookie
private class CookieBridgeStubImpl : CookieSink {
  override fun inject(origin: String, cookies: List<Cookie>) {}
  override fun purgeHost(host: String) {}
  override fun cookieHeader(origin: String): String? = null
}
val CookieBridge: CookieSink = CookieBridgeStubImpl()
EOF

echo "== TIER 1: pure-JVM =="
"$KOTLINC" -cp "$CP" -jvm-target 17 -d "$OUT" \
  "$SRC/main/java/com/tofu/client/session/ServerUrl.kt" \
  "$SRC/main/java/com/tofu/client/session/LoginForm.kt" \
  "$SRC/main/java/com/tofu/client/session/CookieHeaders.kt" \
  "$SRC/main/java/com/tofu/client/session/CookieSink.kt" \
  "$SRC/main/java/com/tofu/client/session/SecretLookup.kt" \
  "$SRC/main/java/com/tofu/client/session/SecretVault.kt" \
  "$SRC/main/java/com/tofu/client/session/ProfileForm.kt" \
  "$SRC/main/java/com/tofu/client/session/SessionManager.kt" \
  "$SRC/main/java/com/tofu/client/session/SessionController.kt" \
  "$SRC/main/java/com/tofu/client/session/SupervisorUrl.kt" \
  "$SRC/main/java/com/tofu/client/session/InteractiveSso.kt" \
  "$SRC/main/java/com/tofu/client/session/ServerLifecycle.kt" \
  "$SRC/main/java/com/tofu/client/session/TofuProbe.kt" \
  "$SRC/main/java/com/tofu/client/session/ApiMetaGate.kt" \
  "$SRC/main/java/com/tofu/client/api/ApiV4Generated.kt" \
  "$SRC/main/java/com/tofu/client/data/Profile.kt" \
  "$OUT/stub/CookieBridgeStub.kt" \
  "$SRC/test/java/com/tofu/client/session/ServerUrlTest.kt" \
  "$SRC/test/java/com/tofu/client/session/LoginFormTest.kt" \
  "$SRC/test/java/com/tofu/client/session/CookieHeadersTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionManagerReauthTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionManagerLoginDegradeTest.kt" \
  "$SRC/test/java/com/tofu/client/session/ProfileFormTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionControllerTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SupervisorUrlTest.kt" \
  "$SRC/test/java/com/tofu/client/session/InteractiveSsoTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionManagerInteractiveSsoTest.kt" \
  "$SRC/test/java/com/tofu/client/session/ServerLifecycleTest.kt" \
  "$SRC/test/java/com/tofu/client/session/TofuProbeTest.kt" \
  "$SRC/test/java/com/tofu/client/session/ApiMetaGateTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionManagerLoginRetryTest.kt" \
  "$SRC/test/java/com/tofu/client/session/SessionManagerPreflightTest.kt"

"$JAVA_HOME/bin/java" -cp "$CP:$KRT:$OUT" org.junit.runner.JUnitCore \
  com.tofu.client.session.ServerUrlTest \
  com.tofu.client.session.LoginFormTest \
  com.tofu.client.session.CookieHeadersTest \
  com.tofu.client.session.SessionManagerReauthTest \
  com.tofu.client.session.SessionManagerLoginDegradeTest \
  com.tofu.client.session.ProfileFormTest \
  com.tofu.client.session.SessionControllerTest \
  com.tofu.client.session.SupervisorUrlTest \
  com.tofu.client.session.InteractiveSsoTest \
  com.tofu.client.session.SessionManagerInteractiveSsoTest \
  com.tofu.client.session.ServerLifecycleTest \
  com.tofu.client.session.TofuProbeTest \
  com.tofu.client.session.ApiMetaGateTest \
  com.tofu.client.session.SessionManagerLoginRetryTest \
  com.tofu.client.session.SessionManagerPreflightTest

# ── TIER 2: Robolectric (optional — needs the instrumented android-all jar) ──
INSTRUMENTED="$(ls "$LIBS"/android-all-instrumented-*.jar 2>/dev/null | head -1 || true)"
if [[ -z "$INSTRUMENTED" || -z "$(ls "$LIBS"/robolectric-*.jar 2>/dev/null || true)" ]]; then
  echo "== TIER 2: Robolectric SKIPPED (no android-all-instrumented / robolectric jars in \$LIBS) =="
  exit 0
fi

echo "== TIER 2: Robolectric =="
RO="$OUT/robo"; mkdir -p "$RO"; echo 'sdk=33' > "$RO/robolectric.properties"
ANDROID_ALL="$(ls "$LIBS"/android-all-1*-robolectric-*.jar | grep -v instrumented | head -1)"
# All jars EXCEPT the compiler + the huge instrumented jar (loaded via dependency.dir).
ROBO_CP="$(ls "$LIBS"/*.jar | grep -vE 'kotlin-compiler|kotlin-daemon|kotlin-script|trove4j|kotlin-reflect|android-all-instrumented' | tr '\n' ':')"
"$KOTLINC" -cp "$ANDROID_ALL:$ROBO_CP" -jvm-target 17 -d "$RO" \
  "$SRC/main/java/com/tofu/client/session/CookieSink.kt" \
  "$SRC/main/java/com/tofu/client/session/CookieHeaders.kt" \
  "$SRC/main/java/com/tofu/client/session/CookieBridge.kt" \
  "$SRC/main/java/com/tofu/client/session/ReauthWebViewClient.kt" \
  "$SRC/main/java/com/tofu/client/session/TofuProbe.kt" \
  "$SRC/main/java/com/tofu/client/session/ServerUrl.kt" \
  "$SRC/main/java/com/tofu/client/session/HealthProbe.kt" \
  "$SRC/main/java/com/tofu/client/data/Profile.kt" \
  "$SRC/test/java/com/tofu/client/session/CookieBridgeRobolectricTest.kt"

"$JAVA_HOME/bin/java" -Drobolectric.offline=true -Drobolectric.dependency.dir="$LIBS" \
  --add-opens java.base/java.lang=ALL-UNNAMED --add-opens java.base/java.util=ALL-UNNAMED \
  --add-opens java.base/java.lang.reflect=ALL-UNNAMED --add-opens java.base/java.io=ALL-UNNAMED \
  --add-opens java.base/java.net=ALL-UNNAMED --add-opens java.base/java.security=ALL-UNNAMED \
  --add-opens java.base/java.text=ALL-UNNAMED \
  -cp "$RO:$ROBO_CP:$KRT" org.junit.runner.JUnitCore \
  com.tofu.client.session.CookieBridgeRobolectricTest
