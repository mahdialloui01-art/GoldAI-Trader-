package com.goldaitrader.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

/**
 * Phone-side market status monitor. It does not execute trades.
 * MT5/EA remains responsible for demo execution and position management.
 */
class MarketMonitorService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val channelId = "goldai_market_monitor"
    private val notificationId = 7401

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(notificationId, buildNotification("جارٍ الاتصال بخادم GoldAI", false))
        serviceScope.launch {
            while (isActive) {
                val state = fetchMarketState()
                getSharedPreferences("goldai_monitor", MODE_PRIVATE).edit()
                    .putString("connection", if (state.connected) "متصل" else "منقطع")
                    .putString("price", state.price)
                    .putLong("updated_at", System.currentTimeMillis())
                    .apply()
                val message = if (state.connected) "السعر: ${state.price} · تحديث الخلفية نشط"
                    else "الخادم أو تغذية MT5 غير متاحة · إعادة المحاولة"
                val manager = getSystemService(NotificationManager::class.java)
                manager.notify(notificationId, buildNotification(message, state.connected))
                delay(if (state.connected) 15_000L else 30_000L)
            }
        }
    }

    private data class MarketState(val connected: Boolean, val price: String)

    private suspend fun fetchMarketState(): MarketState = withContext(Dispatchers.IO) {
        var connection: HttpURLConnection? = null
        try {
            connection = URL("${BuildConfig.API_BASE_URL}/api/v1/market/snapshot")
                .openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.connectTimeout = 7000
            connection.readTimeout = 7000
            if (connection.responseCode != 200) return@withContext MarketState(false, "غير متاح")
            val json = JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
            val mid = json.optDouble("mid", Double.NaN)
            if (!mid.isFinite()) MarketState(false, "غير متاح")
            else MarketState(true, "$" + String.format(Locale.US, "%,.2f", mid))
        } catch (_: Exception) {
            MarketState(false, "غير متاح")
        } finally {
            connection?.disconnect()
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId, "GoldAI Market Monitor", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "حالة اتصال GoldAI وسعر الذهب في الخلفية" }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(message: String, connected: Boolean): Notification =
        NotificationCompat.Builder(this, channelId)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(if (connected) "GoldAI · متصل" else "GoldAI · مراقبة الخلفية")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int =
        START_STICKY

    override fun onDestroy() {
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
