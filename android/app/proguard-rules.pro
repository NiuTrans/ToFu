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

# SSH kernels (Apache MINA SSHD + net.i2p eddsa). Both create classes by name:
# MINA its security-provider registrars and I/O factories, the EdDSA JCA
# provider its KeyFactory/Signature SPIs. Keep them whole rather than guess.
-keep class org.apache.sshd.** { *; }
-keep class net.i2p.crypto.eddsa.** { *; }
# What they reference but Android lacks, on paths the SSH client never runs:
# server-side GSS/JAAS auth, java.rmi (ClientSession.executeRemoteCommand),
# javax.management (ExceptionUtils, skipped when OsUtils.isAndroid()), eddsa's
# sun.security X509Key branch, MINA's optional BouncyCastle/Tomcat APR
# integrations, and SLF4J 1.7's optional logger binding.
-dontwarn java.rmi.**
-dontwarn javax.management.**
-dontwarn javax.security.auth.callback.**
-dontwarn javax.security.auth.login.**
-dontwarn org.ietf.jgss.**
-dontwarn sun.security.x509.**
-dontwarn org.bouncycastle.**
-dontwarn org.apache.tomcat.jni.**
-dontwarn org.slf4j.impl.**
