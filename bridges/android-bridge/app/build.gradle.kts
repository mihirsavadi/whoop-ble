plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.whoopble.bridge"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.whoopble.bridge"
        minSdk = 31          // Android 12+: BLUETOOTH_SCAN/CONNECT permission model only
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// Zero external dependencies by design: BLE via android.bluetooth,
// HTTP server hand-rolled on java.net.ServerSocket, JSON via org.json.
dependencies {
}
