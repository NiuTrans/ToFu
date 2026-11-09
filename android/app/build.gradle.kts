import java.security.MessageDigest

// Local kernel runtime, produced outside this build (android/README.md "Local kernel"):
// -Ptofu.localKernel.jniLibsDir=<dir with arm64-v8a/libproot.so, libproot-loader.so, …>
// -Ptofu.localKernel.assetsDir=<assets root with kernel/index.json and its archives>
// Without them the app builds without the runtime and reports it as not available.
val localKernelJniLibs = providers.gradleProperty("tofu.localKernel.jniLibsDir").orNull?.let { file(it) }?.also { dir ->
    val abis = dir.listFiles().orEmpty().filter { it.isDirectory }.map { it.name }
    require(abis == listOf("arm64-v8a")) { "tofu.localKernel.jniLibsDir=$dir must contain exactly arm64-v8a/, found $abis" }
    for (library in listOf("libproot.so", "libproot-loader.so")) {
        require(File(dir, "arm64-v8a/$library").isFile) { "tofu.localKernel.jniLibsDir=$dir lacks arm64-v8a/$library" }
    }
}
val localKernelAssets = providers.gradleProperty("tofu.localKernel.assetsDir").orNull?.let { file(it) }?.also { dir ->
    require(File(dir, "kernel/index.json").isFile) { "tofu.localKernel.assetsDir=$dir lacks kernel/index.json" }
    // AGP's asset merger gunzips every *.gz asset and drops the extension, which would break
    // the archives' pinned names and SHA-256; the runtime archives are named *.tgz instead.
    val gzipped = dir.walk().filter { it.isFile && it.extension.equals("gz", ignoreCase = true) }.toList()
    require(gzipped.isEmpty()) { "tofu.localKernel.assetsDir: AGP would gunzip $gzipped while merging assets; name runtime archives *.tgz" }
}

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")   // Kotlin 2.0+ Compose Compiler
    id("com.google.devtools.ksp")
    id("app.cash.paparazzi")                     // JVM layoutlib screenshots (dev only)
}

android {
    namespace = "com.tofu.client"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.tofu.client"
        minSdk = 26          // API 26: EncryptedSharedPreferences + modern WebView
        targetSdk = 34
        versionCode = 20
        versionName = "0.1.19"

        // Pin a pasted report to retained native sources, including same-version
        // local test APKs. No device-side hashing or Git checkout is required.
        val digest = MessageDigest.getInstance("SHA-256")
        val fingerprintInputs = fileTree("src/main").files + listOf(
            file("build.gradle.kts"), file("proguard-rules.pro"),
            rootProject.file("build.gradle.kts"), rootProject.file("settings.gradle.kts"),
        )
        fingerprintInputs.sortedBy { it.relativeTo(rootProject.projectDir).path }.forEach { input ->
            digest.update(input.relativeTo(rootProject.projectDir).path.toByteArray(Charsets.UTF_8))
            digest.update(0.toByte())
            digest.update(input.readBytes())
        }
        val sourceFingerprint = digest.digest().joinToString("") { "%02x".format(it) }.take(16)
        buildConfigField("String", "DIAGNOSTIC_BUILD_ID", "\"$sourceFingerprint\"")
    }

    signingConfigs {
        // Public team debug key is for the distinct .debug app only.
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
        create("release") {
            val keyPath = providers.environmentVariable("TOFU_ANDROID_KEYSTORE_PATH").orNull
            if (!keyPath.isNullOrBlank()) storeFile = file(keyPath)
            storePassword = providers.environmentVariable("TOFU_ANDROID_KEYSTORE_PASSWORD").orNull
            keyAlias = providers.environmentVariable("TOFU_ANDROID_KEY_ALIAS").orNull
            keyPassword = providers.environmentVariable("TOFU_ANDROID_KEY_PASSWORD").orNull
        }
    }
    // Debug/test configuration stays usable without release credentials. No
    // release task may silently emit an unsigned APK or use the public key.
    val requirePrivateReleaseSigning = tasks.register("requirePrivateReleaseSigning") {
        doLast {
            val names = listOf("TOFU_ANDROID_KEYSTORE_PATH", "TOFU_ANDROID_KEYSTORE_PASSWORD",
                "TOFU_ANDROID_KEY_ALIAS", "TOFU_ANDROID_KEY_PASSWORD", "TOFU_ANDROID_SIGNER_SHA256")
            val missing = names.filter { providers.environmentVariable(it).orNull.isNullOrBlank() }
            check(missing.isEmpty()) { "Private release signing requires: ${missing.joinToString()}" }
            val key = file(providers.environmentVariable("TOFU_ANDROID_KEYSTORE_PATH").get())
            check(key.isFile && key.canonicalFile != file("debug.keystore").canonicalFile) {
                "A release requires a private keystore, never app/debug.keystore"
            }
            check(!providers.environmentVariable("TOFU_ANDROID_KEY_ALIAS").get().equals("androiddebugkey", ignoreCase = true)) {
                "The public debug key cannot sign a release"
            }
            // JDK source launch validates actual certificate/private-key
            // identity even if the public keystore was copied and renamed.
            val java = File(System.getProperty("java.home"), "bin/java").absolutePath
            val guard = rootProject.file("ValidateKeystore.java").absolutePath
            // The same canonical file is validated and used by AGP, even
            // when the configured path was relative to android/app.
            val process = ProcessBuilder(java, guard, key.canonicalPath).inheritIO().start()
            check(process.waitFor() == 0) { "Private release certificate verification failed" }
        }
    }
    tasks.configureEach {
        if (name == "preReleaseBuild" || name == "validateSigningRelease") {
            dependsOn(requirePrivateReleaseSigning)
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Private release key injected per build. Legacy public-key APKs
            // need an explicit migration; never reuse that key to preserve
            // in-place-update compatibility at the expense of authenticity.
            isDebuggable = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    // proot's and talloc's licence texts (android/native/licenses) are the assets
    // ui/LicencesScreen shows, packaged in place rather than copied.
    sourceSets.getByName("main").assets.srcDir(rootProject.file("native/licenses"))

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
    // MigrationTestHelper reads Room's exported schemas as assets. An app
    // module's unit tests only see the tested variant's merged assets (test
    // source-set assets are ignored), so they are DEBUG assets: release APKs
    // never ship them, and the release unit-test variant skips that test.
    sourceSets.getByName("debug").assets.srcDir("$projectDir/schemas")
    // The Compose Compiler version is now governed by the
    // org.jetbrains.kotlin.plugin.compose plugin (pinned to the Kotlin
    // version in the root build), so no manual composeOptions is needed.

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
        // proot and its loader are executables shipped as lib*.so: they must be extracted
        // into nativeLibraryDir (extractNativeLibs=true), the only app location Android
        // still lets an app exec at targetSdk 29+.
        jniLibs.useLegacyPackaging = true
    }
    // A gzip stream does not compress again: store the runtime archives so the first
    // install inflates them once.
    androidResources.noCompress += "tgz"

    sourceSets.getByName("main") {
        localKernelJniLibs?.let { jniLibs.srcDir(it) }
        localKernelAssets?.let { assets.srcDir(it) }
    }
}

ksp {
    // One JSON per schema version, committed: the migration-test history.
    arg("room.schemaLocation", "$projectDir/schemas")
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
    testImplementation("androidx.room:room-testing:2.6.1")

    // ── Encrypted secrets (Keystore-backed) ──
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // ── Headless login / session ──
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // ── SSH kernels: the tunnel the phone dials out (ssh/**) ──
    // Apache MINA SSHD's client; net.i2p eddsa supplies Ed25519, which Android
    // lacks at API 26. No BouncyCastle: Android already registers a stripped
    // provider named "BC". MINA never uses commons-logging, so its bridge goes.
    // sshd-core and sshd-common each ship Maven's META-INF/DEPENDENCIES listing,
    // which the APK cannot hold twice; the exclusion lives beside its cause.
    implementation("org.apache.sshd:sshd-core:2.20.1") {
        exclude(group = "org.slf4j", module = "jcl-over-slf4j")
    }
    implementation("net.i2p.crypto:eddsa:0.3.0")
    android.packaging.resources.excludes += "/META-INF/DEPENDENCIES"

    // ── WebView (androidx variant, cookie APIs) ──
    implementation("androidx.webkit:webkit:1.11.0")

    // ── QR scanning: add a server by scanning a link ──
    // ZXing embedded ships a ready CaptureActivity + the AndroidX Activity
    // Result `ScanContract`, so the shell needs no camera code of its own.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    // Robolectric tier (CookieBridge shadow-CookieManager + reauth latch).
    testImplementation("org.robolectric:robolectric:4.11.1")
    testImplementation("androidx.test:core:1.5.0")
    testImplementation("androidx.test.ext:junit:1.1.5")
}
