#!/usr/bin/env bash
# Offline tests on actual production sources; Gradle remains the APK/Compose/Room gate.
# JAVA_HOME: JDK17; KOTLINC: Kotlin1.9.24 launcher; LIBS: existing pinned runtime jars.
# Full ladder requires LIFECYCLE_LIBS (official AndroidX2.8.6/annotation1.8/coroutines-android1.8.1)
# and SSH_CLASSPATH (cached MINA sshd-core/common2.20.1, eddsa0.3.0, slf4j-api jars).
# No real tier is replaced with a stub or silently skipped. No downloads are performed.
# --updates / --pure / --robolectric / --lifecycle / --ssh select a focused tier.
# Bare class arguments select pure-JVM regressions only. After a tier selector they
# select that tier's classes. TEST_OUTPUT retains classes/logs/input manifests.
# Optional PYTHON selects the interpreter for the full ladder's actual-DAO SQL gate.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/app/src"
MAIN="$SRC/main/java/com/tofu/client"
TEST="$SRC/test/java/com/tofu/client"
SESSION="$MAIN/session"
: "${JAVA_HOME:?set JAVA_HOME to a JDK 17}"
: "${KOTLINC:?set KOTLINC to the kotlinc launcher}"
: "${LIBS:?set LIBS to the directory with cached runtime jars}"
LIBS="$(cd "$LIBS" && pwd)"
MODE=all
if (( $# > 0 )); then
  case "$1" in
    --updates|--pure|--robolectric|--lifecycle|--ssh) MODE="${1#--}"; shift ;;
    --help)
      echo 'Usage: test-local.sh [--updates|--pure|--robolectric|--lifecycle|--ssh] [JUnit classes...]'
      echo 'No arguments runs all genuine tiers plus the actual-DAO SQL scope gate.'
      exit 0 ;;
    --*) echo "Unknown test tier: $1" >&2; exit 2 ;;
    *) MODE=pure ;;
  esac
fi
SELECTED_CLASSES=("$@")
if [[ -n "${TEST_OUTPUT:-}" ]]; then
  OUT="$TEST_OUTPUT"; mkdir -p "$OUT"; RETAIN_OUTPUT=1
else
  OUT="$(mktemp -d)"; RETAIN_OUTPUT=0
fi
MANIFESTS=()
finish() {
  local result=$?
  trap - EXIT
  for manifest in "${MANIFESTS[@]}"; do
    sha256sum --check "$manifest" > "$manifest.check.log" || result=1
  done
  printf '%s\n' "$result" > "$OUT/exit-code.txt"
  printf 'Local evidence: %s (exit %s)\n' "$OUT" "$result"
  if (( RETAIN_OUTPUT == 0 )); then rm -rf "$OUT"; fi
  exit "$result"
}
trap finish EXIT
require_file() { [[ -f "$1" ]] || { echo "Missing genuine test input: $1" >&2; exit 1; }; }
BASE_JARS=(
  "$LIBS/kotlin-stdlib-1.9.24.jar" "$LIBS/kotlinx-coroutines-core-jvm-1.8.1.jar"
  "$LIBS/kotlinx-coroutines-test-jvm-1.8.1.jar" "$LIBS/okhttp-4.12.0.jar"
  "$LIBS/okio-jvm-3.6.0.jar" "$LIBS/room-common-2.6.1.jar"
  "$LIBS/junit-4.13.2.jar" "$LIBS/hamcrest-core-1.3.jar" "$LIBS/annotations-23.0.0.jar"
)
for jar in "${BASE_JARS[@]}"; do require_file "$jar"; done
BASE_CP="$(IFS=:; echo "${BASE_JARS[*]}")"
ANDROID_ALL="$LIBS/android-all-14-robolectric-10818077.jar"
require_file "$ANDROID_ALL"
"$JAVA_HOME/bin/java" -version 2>&1 | tee "$OUT/java-version.log"
"$KOTLINC" -version 2>&1 | tee "$OUT/kotlin-version.log"
sha256sum "$KOTLINC" "$JAVA_HOME/bin/java" "$ROOT/test-local.sh" > "$OUT/harness.sha256"
MANIFESTS+=("$OUT/harness.sha256")

# One production dependency closure, with no platform/lifecycle replacements.
SESSION_CORE=("$MAIN/data/Profile.kt" "$MAIN/api/ApiV4Generated.kt")
for name in ServerUrl BoundedResponse LoginForm CookieHeaders CookieSink CookieBridge SecretLookup SecretVault \
  ProfileForm SessionManager SessionController HealthProbe SupervisorUrl SupervisorClient SupervisorRunner UrlKernelController \
  InteractiveSso ServerLifecycle TofuProbe ApiMetaGate DiagLog DiagnosticJournal DiagnosticHttp ClientReport \
  UpdateCheck UpdateChecker DeepLink KernelController LocalKernelController LocalKernelCookie LocalKernelLifecycle; do
  SESSION_CORE+=("$SESSION/$name.kt")
done
RUNTIME_CORE=()
for name in KernelLayout KernelSecret KernelProcess LocalKernelState LocalKernelSupervisor; do
  RUNTIME_CORE+=("$MAIN/runtime/$name.kt")
done
SELFCHECK_CORE=()
for name in SelfCheckJson SelfCheckRedactor SelfCheckReport ReportJson ExecChecks KernelCheck ChildStress SelfCheckRun SelfCheckSummary; do
  SELFCHECK_CORE+=("$MAIN/selfcheck/$name.kt")
done
UPDATE_CORE=("$MAIN/data/Profile.kt" "$MAIN/api/ApiV4Generated.kt" "$MAIN/runtime/KernelLayout.kt" "$MAIN/runtime/KernelProcess.kt")
for name in ServerUrl BoundedResponse CookieHeaders CookieSink CookieBridge DiagLog DiagnosticJournal \
  DiagnosticHttp ClientReport UpdateCheck UpdateChecker LocalKernelCookie KernelController; do
  UPDATE_CORE+=("$SESSION/$name.kt")
done
UPDATE_SOURCES=("${UPDATE_CORE[@]}")
UPDATE_CLASSES=()
for name in UpdateCheckTest UpdateCheckerTest UpdateProbeTest UpdateKernelIsolationTest; do
  UPDATE_SOURCES+=("$TEST/session/$name.kt"); UPDATE_CLASSES+=("com.tofu.client.session.$name")
done
PURE_SOURCES=("${SESSION_CORE[@]}" "${RUNTIME_CORE[@]}" "${SELFCHECK_CORE[@]}" "$TEST/session/ProfileDaoFixture.kt")
PURE_CLASSES=()
for name in SessionManagerGatewayTest ServerUrlTest LoginFormTest CookieHeadersTest SessionManagerReauthTest \
  SessionManagerLoginDegradeTest ProfileFormTest SessionControllerTest SupervisorUrlTest InteractiveSsoTest \
  SessionManagerInteractiveSsoTest ServerLifecycleTest TofuProbeTest ApiMetaGateTest \
  SessionManagerLoginRetryTest SessionManagerPreflightTest DiagLogTest DiagnosticJournalTest \
  DiagnosticHttpTest ClientReportTest SessionManagerSubmitReportTest UpdateCheckTest UpdateCheckerTest \
  DeepLinkTest UrlCredentialsTest UpdateProbeTest UpdateKernelIsolationTest HealthProbeTest NativeHttpCancellationTest SecretVaultTest \
  KernelControllerTest SessionControllerKernelTest LocalKernelControllerTest LocalKernelCookieTest LocalKernelLifecycleTest; do
  PURE_SOURCES+=("$TEST/session/$name.kt"); PURE_CLASSES+=("com.tofu.client.session.$name")
done
PURE_SOURCES+=("$TEST/runtime/RuntimeFakes.kt" "$TEST/selfcheck/SelfCheckFakes.kt" "$TEST/selfcheck/SelfCheckExampleReport.kt")
for name in LocalKernelSupervisorTest KernelProcessTest KernelSecretTest; do
  PURE_SOURCES+=("$TEST/runtime/$name.kt"); PURE_CLASSES+=("com.tofu.client.runtime.$name")
done
for name in SelfCheckReportSchemaTest SelfCheckRedactionTest ChildStressTest SelfCheckRunTest; do
  PURE_SOURCES+=("$TEST/selfcheck/$name.kt"); PURE_CLASSES+=("com.tofu.client.selfcheck.$name")
done
ROBO_SOURCES=("${SESSION_CORE[@]}" "${RUNTIME_CORE[@]}" "$SESSION/ReauthWebViewClient.kt")
for name in RuntimeIndex TarReader RuntimeInstaller; do ROBO_SOURCES+=("$MAIN/runtime/$name.kt"); done
ROBO_SOURCES+=("$TEST/session/CookieBridgeRobolectricTest.kt" "$TEST/session/UrlKernelControllerTest.kt" \
  "$TEST/runtime/TarFixture.kt" "$TEST/runtime/RuntimeInstallerTest.kt")
ROBO_CLASSES=(com.tofu.client.session.CookieBridgeRobolectricTest com.tofu.client.session.UrlKernelControllerTest \
  com.tofu.client.runtime.RuntimeInstallerTest)
LIFE_SOURCES=("${SESSION_CORE[@]}" "${RUNTIME_CORE[@]}" "$MAIN/ssh/SshSetup.kt" \
  "$MAIN/ui/ProfilesViewModel.kt" "$TEST/session/ProfileDaoFixture.kt" "$TEST/ui/ProfilesViewModelTest.kt")
LIFE_CLASSES=(com.tofu.client.ui.ProfilesViewModelTest)
SSH_SOURCES=("${SESSION_CORE[@]}" "${RUNTIME_CORE[@]}" "$TEST/session/ProfileDaoFixture.kt")
for name in SshSetup SshIdentity SshTunnel SshTunnels SshKernelController AndroidSsh; do SSH_SOURCES+=("$MAIN/ssh/$name.kt"); done
SSH_SOURCES+=("$TEST/ssh/SshIdentityTest.kt" "$TEST/ssh/SshKernelControllerTest.kt")
SSH_CLASSES=(com.tofu.client.ssh.SshIdentityTest com.tofu.client.ssh.SshKernelControllerTest)

JAVA_ARGS=()
run_tier() {
  local name="$1" cp="$2"
  local -n sources_ref="$3" defaults_ref="$4" dependencies_ref="$5"
  local dir="$OUT/$name"
  mkdir -p "$dir"
  # A retained output directory never exposes stale classes from an older run.
  local classes_dir; classes_dir="$(mktemp -d "$dir/classes-XXXXXXXX")"
  for source in "${sources_ref[@]}"; do require_file "$source"; done
  sha256sum "${sources_ref[@]}" > "$dir/sources.sha256"
  sha256sum "${dependencies_ref[@]}" > "$dir/dependencies.sha256"
  MANIFESTS+=("$dir/sources.sha256" "$dir/dependencies.sha256")
  printf '%s\n' "$classes_dir:$cp" > "$dir/classpath.txt"
  local classes=("${defaults_ref[@]}")
  if (( ${#SELECTED_CLASSES[@]} > 0 )); then classes=("${SELECTED_CLASSES[@]}"); fi
  printf '%s\n' "${classes[@]}" > "$dir/test-classes.txt"
  echo "== $name: actual sources =="
  "$KOTLINC" -cp "$cp" -jvm-target 17 -d "$classes_dir" "${sources_ref[@]}" 2>&1 | tee "$dir/compile.log"
  local args=("${JAVA_ARGS[@]}")
  if [[ "$name" == lifecycle ]]; then args+=("-Xlog:class+load=info:file=$dir/class-load.log"); fi
  "$JAVA_HOME/bin/java" "${args[@]}" -cp "$classes_dir:$cp" org.junit.runner.JUnitCore "${classes[@]}" \
    2>&1 | tee "$dir/junit.log"
}

run_updates() {
  local jars=("${BASE_JARS[@]}" "$ANDROID_ALL")
  JAVA_ARGS=()
  run_tier updates "$BASE_CP:$ANDROID_ALL" UPDATE_SOURCES UPDATE_CLASSES jars
}
run_pure() {
  # Seam tests inject their own DAO/vault/cookie fixtures. Production code and
  # Android signatures are unchanged; framework behaviour belongs to Robolectric.
  local jars=("${BASE_JARS[@]}" "$ANDROID_ALL")
  JAVA_ARGS=()
  run_tier pure "$BASE_CP:$ANDROID_ALL" PURE_SOURCES PURE_CLASSES jars
}
run_robolectric() {
  require_file "$LIBS/android-all-instrumented-13-robolectric-9030017-i4.jar"
  require_file "$LIBS/android-all-instrumented-14-robolectric-10818077-i4.jar"
  require_file "$LIBS/robolectric-4.11.1.jar"
  local jars=()
  for jar in "$LIBS"/*.jar; do
    case "$(basename "$jar")" in
      kotlin-compiler-*|kotlin-daemon-*|kotlin-script-*|trove4j-*|kotlin-reflect-*|android-all-instrumented-*) ;;
      *) jars+=("$jar") ;;
    esac
  done
  local cp; cp="$(IFS=:; echo "${jars[*]}")"
  # Robolectric loads this genuine framework separately, not from the compile classpath.
  jars+=("$LIBS/android-all-instrumented-13-robolectric-9030017-i4.jar"
    "$LIBS/android-all-instrumented-14-robolectric-10818077-i4.jar")
  JAVA_ARGS=("-Drobolectric.offline=true" "-Drobolectric.dependency.dir=$LIBS"
    --add-opens java.base/java.lang=ALL-UNNAMED --add-opens java.base/java.util=ALL-UNNAMED
    --add-opens java.base/java.lang.reflect=ALL-UNNAMED --add-opens java.base/java.io=ALL-UNNAMED
    --add-opens java.base/java.net=ALL-UNNAMED --add-opens java.base/java.security=ALL-UNNAMED
    --add-opens java.base/java.text=ALL-UNNAMED)
  run_tier robolectric "$cp" ROBO_SOURCES ROBO_CLASSES jars
}
run_lifecycle() {
  : "${LIFECYCLE_LIBS:?genuine lifecycle tier requires the cached official jar directory}"
  local life_libs; life_libs="$(cd "$LIFECYCLE_LIBS" && pwd)"
  printf '%s  %s\n' \
    b03ab18915f0682c18ff3fc8736044b6263d932a0c900d08c85648f1af0b0d3a "$life_libs/lifecycle-viewmodel-android-2.8.6.jar" \
    9aab326d9492800991854360ac248f493ce7f7c3183519309b78ace9e240f6f6 "$life_libs/annotation-jvm-1.8.0.jar" \
    a134dacf4e6578b29b32e97aa50548d09cb45e3cb3551ce77ac27e55e265d8f5 "$life_libs/kotlinx-coroutines-android-1.8.1.jar" \
    | sha256sum --check
  local jars=("${BASE_JARS[@]}" "$ANDROID_ALL" "$life_libs/lifecycle-viewmodel-android-2.8.6.jar" \
    "$life_libs/annotation-jvm-1.8.0.jar" "$life_libs/kotlinx-coroutines-android-1.8.1.jar")
  local cp; cp="$(IFS=:; echo "${jars[*]}")"
  JAVA_ARGS=()
  run_tier lifecycle "$cp" LIFE_SOURCES LIFE_CLASSES jars
  for class in ViewModel ViewModelProvider ViewModelStore ViewModelKt; do
    grep -F "androidx.lifecycle.$class source: file:$life_libs/lifecycle-viewmodel-android-2.8.6.jar" "$OUT/lifecycle/class-load.log"
  done
}
run_ssh() {
  : "${SSH_CLASSPATH:?genuine SSH tier requires cached MINA2.20.1/common, eddsa0.3.0 and slf4j-api jar paths}"
  local ssh_jars; IFS=: read -r -a ssh_jars <<< "$SSH_CLASSPATH"
  local found_core=0 found_common=0 found_eddsa=0 found_slf4j=0
  for jar in "${ssh_jars[@]}"; do
    require_file "$jar"
    case "$(basename "$jar")" in
      sshd-core-2.20.1.jar) found_core=1 ;;
      sshd-common-2.20.1.jar) found_common=1 ;;
      eddsa-0.3.0.jar) found_eddsa=1 ;;
      slf4j-api-*.jar) found_slf4j=1 ;;
      *) echo "Unexpected SSH dependency (do not substitute versions): $jar" >&2; exit 1 ;;
    esac
  done
  if (( found_core + found_common + found_eddsa + found_slf4j != 4 )); then
    echo 'SSH_CLASSPATH must supply real sshd-core/common 2.20.1, eddsa 0.3.0 and slf4j-api.' >&2; exit 1
  fi
  local jars=("${BASE_JARS[@]}" "$ANDROID_ALL" "${ssh_jars[@]}")
  local cp; cp="$(IFS=:; echo "${jars[*]}")"
  JAVA_ARGS=()
  run_tier ssh "$cp" SSH_SOURCES SSH_CLASSES jars
}
run_sql() {
  local python="${PYTHON:-$ROOT/../.venv/bin/python3}"
  [[ -x "$python" ]] || { echo 'Actual-DAO SQL tier needs PYTHON=/path/to/python3.' >&2; exit 1; }
  sha256sum "$MAIN/data/Profile.kt" "$ROOT/tests/test_profile_save_query.py" \
    "$ROOT/app/schemas/com.tofu.client.data.ProfileDatabase/3.json" > "$OUT/sql-sources.sha256"
  MANIFESTS+=("$OUT/sql-sources.sha256")
  echo '== SQL: production Room query against in-memory SQLite (not generated bindings) =='
  "$python" "$ROOT/tests/test_profile_save_query.py" 2>&1 | tee "$OUT/sql-tests.log"
}
case "$MODE" in
  updates) run_updates ;;
  pure) run_pure ;;
  robolectric) run_robolectric ;;
  lifecycle) run_lifecycle ;;
  ssh) run_ssh ;;
  all) run_pure; run_robolectric; run_lifecycle; run_ssh; run_sql ;;
esac
