#!/usr/bin/env bash
# Populate a $LIBS dir with every jar test-local.sh needs, so the two SDK-free
# test tiers are reproducible from a fresh clone (not just from a warm cache).
#
# Usage:
#   ./fetch-test-deps.sh [DEST_DIR]        # default: ./.testharness/libs
#
# Sources each artifact from the primary Maven mirror, falling back to Maven
# Central. Versions are PINNED to what the suites were validated against.
# The Robolectric tier additionally needs a JDK 17 + kotlinc on PATH (see
# README "Running the unit tests"); this script fetches only the JARs.
set -euo pipefail

DEST="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.testharness/libs}"
mkdir -p "$DEST"

# Maven Central is the default source; set MIRROR to a nearer mirror (e.g. a
# corporate Nexus) to override. CENTRAL is the fallback when MIRROR is set.
MIRROR="${MIRROR:-https://repo1.maven.org/maven2}"
CENTRAL="https://repo1.maven.org/maven2"
# androidx artifacts are NOT on Maven Central — they resolve only from Google's
# Maven repo, so every fetch tries it as the last fallback.
GOOGLE="https://maven.google.com"

# groupPath/artifact/version/file  — one per line (Maven coordinates as a path).
ARTIFACTS=(
  # ── Kotlin compiler + runtime (pure tier compile) ──
  "org/jetbrains/kotlin/kotlin-compiler-embeddable/1.9.24/kotlin-compiler-embeddable-1.9.24.jar"
  "org/jetbrains/kotlin/kotlin-stdlib/1.9.24/kotlin-stdlib-1.9.24.jar"
  "org/jetbrains/kotlin/kotlin-reflect/1.9.24/kotlin-reflect-1.9.24.jar"
  "org/jetbrains/kotlin/kotlin-script-runtime/1.9.24/kotlin-script-runtime-1.9.24.jar"
  "org/jetbrains/kotlin/kotlin-daemon-embeddable/1.9.24/kotlin-daemon-embeddable-1.9.24.jar"
  "org/jetbrains/intellij/deps/trove4j/1.0.20200330/trove4j-1.0.20200330.jar"
  # ── App runtime deps ──
  "org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/1.8.1/kotlinx-coroutines-core-jvm-1.8.1.jar"
  "org/jetbrains/kotlinx/kotlinx-coroutines-test-jvm/1.8.1/kotlinx-coroutines-test-jvm-1.8.1.jar"
  "com/squareup/okhttp3/okhttp/4.12.0/okhttp-4.12.0.jar"
  "com/squareup/okio/okio-jvm/3.6.0/okio-jvm-3.6.0.jar"
  "androidx/room/room-common/2.6.1/room-common-2.6.1.jar"
  "org/jetbrains/annotations/23.0.0/annotations-23.0.0.jar"
  # ── JUnit ──
  "junit/junit/4.13.2/junit-4.13.2.jar"
  "org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
  # ── Robolectric tier ──
  "org/robolectric/robolectric/4.11.1/robolectric-4.11.1.jar"
  "org/robolectric/shadows-framework/4.11.1/shadows-framework-4.11.1.jar"
  "org/robolectric/annotations/4.11.1/annotations-4.11.1.jar"
  "org/robolectric/junit/4.11.1/junit-4.11.1.jar"
  "org/robolectric/resources/4.11.1/resources-4.11.1.jar"
  "org/robolectric/sandbox/4.11.1/sandbox-4.11.1.jar"
  "org/robolectric/utils/4.11.1/utils-4.11.1.jar"
  "org/robolectric/utils-reflector/4.11.1/utils-reflector-4.11.1.jar"
  "org/robolectric/pluginapi/4.11.1/pluginapi-4.11.1.jar"
  "org/robolectric/shadowapi/4.11.1/shadowapi-4.11.1.jar"
  "org/robolectric/plugins-maven-dependency-resolver/4.11.1/plugins-maven-dependency-resolver-4.11.1.jar"
  "org/robolectric/android-all/14-robolectric-10818077/android-all-14-robolectric-10818077.jar"
  # Instrumented android-all: the tests pin @Config(sdk=[33]) → the SDK 33
  # (…-13-…) jar is what Robolectric actually loads. The SDK 34 (…-14-…) jar is
  # fetched too so raising @Config later doesn't break the cold path.
  "org/robolectric/android-all-instrumented/13-robolectric-9030017-i4/android-all-instrumented-13-robolectric-9030017-i4.jar"
  "org/robolectric/android-all-instrumented/14-robolectric-10818077-i4/android-all-instrumented-14-robolectric-10818077-i4.jar"
  # ── Robolectric transitive runtime ──
  "org/ow2/asm/asm/9.5/asm-9.5.jar"
  "org/ow2/asm/asm-commons/9.5/asm-commons-9.5.jar"
  "org/ow2/asm/asm-tree/9.5/asm-tree-9.5.jar"
  "org/ow2/asm/asm-util/9.5/asm-util-9.5.jar"
  "org/ow2/asm/asm-analysis/9.5/asm-analysis-9.5.jar"
  "net/bytebuddy/byte-buddy/1.12.19/byte-buddy-1.12.19.jar"
  "net/bytebuddy/byte-buddy-agent/1.12.19/byte-buddy-agent-1.12.19.jar"
  "com/google/guava/guava/32.1.2-jre/guava-32.1.2-jre.jar"
  "com/google/guava/failureaccess/1.0.1/failureaccess-1.0.1.jar"
  "javax/inject/javax.inject/1/javax.inject-1.jar"
  "javax/annotation/javax.annotation-api/1.3.2/javax.annotation-api-1.3.2.jar"
  "com/google/code/findbugs/jsr305/3.0.2/jsr305-3.0.2.jar"
  "com/google/errorprone/error_prone_annotations/2.21.1/error_prone_annotations-2.21.1.jar"
  "org/bouncycastle/bcprov-jdk18on/1.71/bcprov-jdk18on-1.71.jar"
  "com/almworks/sqlite4java/sqlite4java/1.0.392/sqlite4java-1.0.392.jar"
  "com/ibm/icu/icu4j/72.1/icu4j-72.1.jar"
  "androidx/annotation/annotation-jvm/1.7.0/annotation-jvm-1.7.0.jar"
  "org/conscrypt/conscrypt-openjdk-uber/2.5.2/conscrypt-openjdk-uber-2.5.2.jar"
)

# androidx.test artifacts ship as .aar — Robolectric 4.11 needs
# androidx.test.platform.app.InstrumentationRegistry (from :monitor) internally.
# We fetch each .aar and extract its classes.jar onto the JVM classpath as
# androidxtest_<name>.jar (an .aar can't go on a classpath directly).
AARS=(
  "androidx/test/monitor/1.6.1/monitor-1.6.1.aar"
  "androidx/test/core/1.5.0/core-1.5.0.aar"
  "androidx/test/ext/junit/1.1.5/junit-1.1.5.aar"
  "androidx/test/runner/1.5.2/runner-1.5.2.aar"
  "androidx/test/annotation/1.0.1/annotation-1.0.1.aar"
)

is_zip() { head -c2 "$1" 2>/dev/null | grep -q 'PK'; }

fetch() {
  local coord="$1"; local out="$DEST/$(basename "$coord")"
  if [[ -s "$out" ]] && is_zip "$out"; then echo "  cached  $(basename "$coord")"; return 0; fi
  for base in "$MIRROR" "$CENTRAL" "$GOOGLE"; do
    if curl -fsSL -o "$out" "$base/$coord" 2>/dev/null && is_zip "$out"; then
      echo "  ok      $(basename "$coord")  ($(stat -c%s "$out" 2>/dev/null || echo '?')B)"
      return 0
    fi
  done
  rm -f "$out"
  echo "  FAIL    $coord" >&2
  return 1
}

# Fetch an .aar and extract its classes.jar as androidxtest_<name>.jar.
fetch_aar() {
  local coord="$1"; local name; name="$(basename "$coord" .aar)"
  local out="$DEST/androidxtest_${name}.jar"
  if [[ -s "$out" ]] && is_zip "$out"; then echo "  cached  androidxtest_${name}.jar"; return 0; fi
  local tmp; tmp="$(mktemp --suffix=.aar)"
  for base in "$MIRROR" "$CENTRAL" "$GOOGLE"; do
    if curl -fsSL -o "$tmp" "$base/$coord" 2>/dev/null && is_zip "$tmp"; then
      if unzip -p "$tmp" classes.jar > "$out" 2>/dev/null && is_zip "$out"; then
        rm -f "$tmp"; echo "  ok      androidxtest_${name}.jar  ($(stat -c%s "$out" 2>/dev/null || echo '?')B)"
        return 0
      fi
    fi
  done
  rm -f "$tmp" "$out"
  echo "  FAIL    $coord" >&2
  return 1
}

echo "Fetching test-harness jars into: $DEST"
fails=0
for a in "${ARTIFACTS[@]}"; do fetch "$a" || fails=$((fails+1)); done
for a in "${AARS[@]}"; do fetch_aar "$a" || fails=$((fails+1)); done

total=$(( ${#ARTIFACTS[@]} + ${#AARS[@]} ))
echo
if [[ $fails -eq 0 ]]; then
  echo "All $total artifacts present. Now run:  LIBS=$DEST ./test-local.sh"
else
  echo "$fails artifact(s) failed to download (see FAIL lines above)." >&2
  exit 1
fi
