# GoldAI Trader — Android

Native Android application built with Kotlin and Jetpack Compose.

## Open in Android Studio
1. Open the `android/` directory in Android Studio.
2. Install the Android SDK platform 35 and a Java 17 JDK.
3. Sync Gradle and run the `app` configuration on an emulator or device.

## Current prototype
- Arabic-first dashboard with RTL support.
- Clearly labels the app as DEMO / Paper mode.
- Does not fabricate a gold price or claim to receive live market data.
- Does not connect to MetaTrader or place orders yet.

The next integration step is an authenticated HTTPS connection to the FastAPI backend. MetaTrader 5 execution will be handled separately by a desktop/VPS Expert Advisor, with live trading disabled by default.
