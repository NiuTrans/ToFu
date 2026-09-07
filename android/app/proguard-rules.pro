# ProGuard/R8 rules for the release build.
#
# Code shrinking is currently DISABLED (isMinifyEnabled = false in
# build.gradle.kts), so these rules are not applied today. The file exists
# because the release buildType references it via proguardFiles(...); keep it
# here so enabling minification later is a one-line flag flip. Add keep rules
# below if/when shrinking is turned on (e.g. Room, OkHttp, Compose reflection).
