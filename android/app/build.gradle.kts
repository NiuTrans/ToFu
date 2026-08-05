plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")   // Kotlin 2.0+ Compose Compiler
    id("com.google.devtools.ksp")
}

android {
    namespace = "com.tofu.client"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.tofu.client"
        minSdk = 26          // API 26: EncryptedSharedPreferences + modern WebView
        targetSdk = 34
        versionCode = 17
        versionName = "0.1.16"
    }

    signingConfigs {
        // A FIXED, committed debug keystore (Android's standard
        // android/androiddebugkey convention) so every build — local AND CI —
        // is signed with the SAME key. Without this, GitHub runners generate a
        // fresh throwaway debug key per build, changing the signature, and
        // Android then refuses to install the update over an existing install
        // (INSTALL_FAILED_UPDATE_INCOMPATIBLE / "App not installed") — forcing
        // an uninstall each time. A debug keystore is NOT a secret; committing
        // it is standard practice for a team-shared test build.
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Sideloaded GitHub-Release distribution (no Play Store), so we
            // sign the release with the SAME fixed, committed debug keystore
            // used for debug builds. This makes `assembleRelease` produce a
            // SIGNED, installable APK with no repo-secret setup, and — because
            // the key never changes — release updates install over each other
            // (and over existing debug installs) without
            // INSTALL_FAILED_UPDATE_INCOMPATIBLE. Signing with the debug KEY
            // does NOT make the build debuggable; that stays false on release.
            // Switch to a secret-backed signingConfigs.release before any
            // Play Store submission (see README "Release & signing").
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    // buildConfig=true generates com.tofu.client.BuildConfig with VERSION_NAME /
    // VERSION_CODE (off by default in AGP 8+), consumed by the home-screen
    // version footer so the shipped version is visible in-app.
    buildFeatures {
        compose = true
        buildConfig = true
    }

    // Robolectric needs the merged Android resources; isReturnDefaultValues lets
    // the NON-Robolectric pure-JVM tests call android.util.Log (used by
    // SessionManager/SessionController) without a "not mocked" RuntimeException
    // — un-mocked android.* calls return 0/null instead of throwing.
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            isReturnDefaultValues = true
            // Robolectric downloads its `android-all-instrumented` runtime jar
            // from Maven at test time via its OWN resolver, which ignores the
            // http_proxy env var and fails with UnknownHostException on a
            // network-restricted machine. When the pre-fetched jars exist
            // locally (.testharness/libs, populated by fetch-test-deps.sh),
            // point Robolectric at them and force offline so tests run with
            // zero network. That dir is NOT committed, so on CI (fresh runner,
            // has network) it is absent — we then skip these properties and let
            // Robolectric resolve its runtime jar online via the default
            // resolver. Forcing offline unconditionally would break CI's
            // `./gradlew test` (offline + no local jar = resolution failure).
            val harnessLibs = rootProject.file(".testharness/libs")
            if (harnessLibs.isDirectory) {
                all {
                    it.systemProperty("robolectric.offline", "true")
                    it.systemProperty(
                        "robolectric.dependency.dir",
                        harnessLibs.absolutePath
                    )
                }
            }
        }
    }
    // The Compose Compiler version is now governed by the
    // org.jetbrains.kotlin.plugin.compose plugin (pinned to the Kotlin
    // version in the root build), so no manual composeOptions is needed.

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

dependencies {
    // ── Compose UI ──
    val composeBom = platform("androidx.compose:compose-bom:2024.09.02")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.6")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")

    // ── Room (profile store) ──
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // ── Encrypted secrets (Keystore-backed) ──
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // ── Headless login / session ──
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // ── WebView (androidx variant, cookie APIs) ──
    implementation("androidx.webkit:webkit:1.11.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    // Robolectric tier (CookieBridge shadow-CookieManager + reauth latch).
    testImplementation("org.robolectric:robolectric:4.11.1")
    testImplementation("androidx.test:core:1.5.0")
    testImplementation("androidx.test.ext:junit:1.1.5")
}
