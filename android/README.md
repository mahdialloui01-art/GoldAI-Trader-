# GoldAI Trader — Android

Native Android application built with Kotlin and Jetpack Compose.

## Open in Android Studio
1. Open the `android/` directory in Android Studio.
2. Install Android SDK platform 35 and a Java 17 JDK.
3. Sync Gradle and run the `app` configuration on an emulator or device.

## Backend connection
The dashboard can test `/health` and request the latest authenticated XM `GOLD` broker Bid/Ask quote from `/api/v1/market/snapshot`. The public indicative spot comparison is separate at `/api/v1/market/spot-reference`.

The default development URL is `http://10.0.2.2:8000`, which reaches the host computer from the Android emulator.

For a physical phone, set the computer's reachable LAN URL when building, for example:

```bash
gradle -p android assembleDebug -PGOLDAI_API_BASE_URL=http://192.168.1.20:8000
```

Replace the example IP with the development computer's LAN IP. The phone and computer must be on the same trusted network, and the backend must listen on the LAN interface.

## Market quote limitations
The primary market snapshot accepts only fresh XM `GOLD` Bid/Ask quotes delivered by the authenticated MT5 Expert Advisor. The Gold API `/price/XAU` endpoint is available only as an indicative comparison at `/api/v1/market/spot-reference`; it is not an execution quote and is never substituted when MT5 data is missing or stale.

## Release security
The current manifest permits cleartext HTTP for local development only. Before production, use HTTPS, remove `android:usesCleartextTraffic="true"`, and protect the API with authentication and rate limits. Do not embed broker credentials or API secrets in the Android app.

## Current prototype
- Arabic-first dashboard with a dark gold-accented theme.
- Paper/Demo mode is visible.
- Manual refresh for the latest broker quote when the MT5 bridge is connected.
- Backend health-check action.
- The MT5 quote bridge source is prepared for XM `GOLD`; it still needs desktop MT5 or a VPS, an HTTPS backend, and end-to-end testing.
- No order execution is active.

MetaTrader 5 execution will be handled separately by a desktop/VPS Expert Advisor. Live trading remains disabled by default.
