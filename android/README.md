# GoldAI Trader — Android

Native Android application built with Kotlin and Jetpack Compose.

## Open in Android Studio
1. Open the `android/` directory in Android Studio.
2. Install Android SDK platform 35 and a Java 17 JDK.
3. Sync Gradle and run the `app` configuration on an emulator or device.

## Backend connection
The app has a **Test server connection** action that requests the backend `/health` endpoint.

The default development URL is `http://10.0.2.2:8000`, which reaches the host computer from the Android emulator.

For a physical phone, set the computer's reachable LAN URL when building, for example:

```bash
gradle -p android assembleDebug -PGOLDAI_API_BASE_URL=http://192.168.1.20:8000
```

Replace the example IP with the development computer's LAN IP. The phone and computer must be on the same trusted network, and the backend must be configured to listen on the LAN interface.

**Release security:** the current manifest permits cleartext HTTP for local development only. Before production, use HTTPS, remove `android:usesCleartextTraffic="true"`, and protect the API with authentication and rate limits. Do not embed broker credentials or API secrets in the Android app.

## Current prototype
- Arabic-first dashboard with a dark gold-accented theme.
- Paper/Demo mode is visible.
- Backend health check is implemented.
- No fabricated gold price is shown; market data is marked disconnected until a real provider is configured.
- No MetaTrader connection or order execution is active.

MetaTrader 5 execution will be handled separately by a desktop/VPS Expert Advisor. Live trading remains disabled by default.
