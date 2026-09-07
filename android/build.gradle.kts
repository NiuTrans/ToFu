// Top-level build file. Plugin versions declared once, applied per-module.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "2.0.20" apply false
    // Kotlin 2.0+ requires the Compose Compiler Gradle plugin when compose is
    // enabled (replaces the old composeOptions.kotlinCompilerExtensionVersion).
    // Its version is pinned to the Kotlin version.
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.20" apply false
    id("com.google.devtools.ksp") version "2.0.20-1.0.25" apply false
}
