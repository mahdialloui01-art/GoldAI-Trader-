# GoldAI Trader — Android

Native Android application built with Kotlin and Jetpack Compose.

## Open in Android Studio
1. Open the `android/` directory in Android Studio.
2. Install Android SDK platform 35 and a Java 17 JDK.
3. Sync Gradle and run the `app` configuration on an emulator or device.

## Backend connection
The dashboard can test `/health` and request the latest authenticated XM `GOLD` broker Bid/Ask quote from `/api/v1/market/snapshot`. The public indicative spot comparison is separate at `/api/v1/market/spot-reference`.

There is deliberately no default backend URL in phone builds. The old `10.0.2.2` address is a special alias for the development computer from the Android Emulator only; it is not a public server address and does not work as a general backend address on a physical phone. Android documents this emulator-only mapping: https://developer.android.com/studio/run/emulator-networking-address

For local development, explicitly set a reachable backend URL when building. For a physical phone on the same trusted Wi-Fi, use the computer's LAN address and make sure the backend listens on the LAN interface:

```bash
gradle -p android assembleDebug -PGOLDAI_API_BASE_URL=http://192.168.1.20:8000
```

For use without a running personal computer, deploy the FastAPI backend to an always-reachable HTTPS hosting service and build with its HTTPS URL:

```bash
gradle -p android assembleDebug -PGOLDAI_API_BASE_URL=https://YOUR-BACKEND-HOST
```

The app now shows a clear configuration message instead of attempting to connect to the emulator-only address when no backend URL is configured. A hosted backend still needs to be deployed and configured; this repository change does not itself create a live server.

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
