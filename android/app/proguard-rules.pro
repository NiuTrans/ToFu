# ProGuard/R8 rules for the release build (isMinifyEnabled = true).

# JS→native bridges exposed as window.TofuDiag / window.TofuNative. The SPA
# calls deliver / requestReauth by source name; shrinking would rename them.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# Room profile store (entities + generated DAO impls).
-keep class com.tofu.client.data.** { *; }

# androidx.security:security-crypto pulls in Tink, whose classes reference
# errorprone annotations. Those are compile-time only and absent at runtime, so
# R8 refuses the release build on the missing classes. They are never touched
# reflectively and add no behavior, so suppressing the warning is correct.
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**
