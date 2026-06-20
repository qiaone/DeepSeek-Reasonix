plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.reasonix.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.reasonix.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.0.1"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    // Extract .so files to disk so the Go binary (bundled as libreasonix.so)
    // is physically present in nativeLibraryDir and can be executed.
    // Default is false for minSdk >= 23, which keeps .so files compressed
    // inside the APK — inaccessible to ProcessBuilder.
    packaging {
        jniLibs {
            useLegacyPackaging = true
        }
    }

    // Don't compress Termux bootstrap zips in the APK — they're already
    // compressed and double-compression wastes CPU on extraction.
    aaptOptions {
        noCompress("zip")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("androidx.cardview:cardview:1.0.0")
}
