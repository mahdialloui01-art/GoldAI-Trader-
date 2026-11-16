package com.goldaitrader.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

/**
 * Persistent phone-side monitor. It refreshes quote, expert analysis and news status.
 * MT5/EA remains responsible for trade execution and open-position management.
 */
class MarketMonitorService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val channelId = "goldai_market_monitor"
    private val notificationId = 7401
    @Volatile private var analysisLine = "التحليل: في انتظار أول تحديث"
    @Volatile private var newsLine = "الأخبار: في انتظار التحديث"

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(notificationId, buildNotification("جارٍ الاتصال بخادم GoldAI", false))
        serviceScope.launch {
            var lastAnalysisAt = 0L
            var lastNewsAt = 0L
            while (isActive) {
                val state = fetchMarketState()
                val now = SystemClock.elapsedRealtime()
                if (now - lastAnalysisAt >= 60_000L) {
                    analysisLine = fetchExpertAnalysis()
                    lastAnalysisAt = now
                }
                if (now - lastNewsAt >= 300_000L) {
                    newsLine = fetchNewsStatus()
                    lastNewsAt = now
                }
                val message = if (state.connected) {
                    "GOLD ${state.price} | ${analysisLine} | ${newsLine}"
                } else {
                    "الخادم أو تغذية MT5 غير متاحة؛ ستتم إعادة المحاولة تلقائياً"
                }
                getSharedPreferences("goldai_monitor", MODE_PRIVATE).edit()
                    .putString("connection", if (state.connected) "متصل" else "منقطع")
                    .putString("price", state.price)
                    .putString("analysis", analysisLine)
                    .putString("news", newsLine)
                    .putLong("updated_at", System.currentTimeMillis())
                    .apply()
                getSystemService(NotificationManager::class.java)
                    .notify(notificationId, buildNotification(message, state.connected))
                delay(if (state.connected) 15_000L else 30_000L)
            }
        }
    }

    private data class MarketState(val connected: Boolean, val price: String)

    private fun getJson(path: String): JSONObject? {
        var connection: HttpURLConnection? = null
        return try {
            connection = URL("${BuildConfig.API_BASE_URL}$path").openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.connectTimeout = 7000
            connection.readTimeout = 9000
            if (connection.responseCode != 200) null
            else JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
        } catch (_: Exception) {
            null
        } finally {
            connection?.disconnect()
        }
    }

    private fun fetchMarketState(): MarketState {
        val json = getJson("/api/v1/market/snapshot")
            ?: return MarketState(false, "غير متاح")
        val mid = json.optDouble("mid", Double.NaN)
        return if (!mid.isFinite()) MarketState(false, "غير متاح")
        else MarketState(true, "$" + String.format(Locale.US, "%,.2f", mid))
    }

    private fun fetchExpertAnalysis(): String {
        val json = getJson("/api/v1/analysis/expert/live")
            ?: return "التحليل: الخادم غير متاح"
        val decision = json.optString("decision", "WAIT")
        val dossier = json.optJSONObject("technical_dossier")
        val score = dossier?.optDouble("confluence_score", Double.NaN) ?: Double.NaN
        val market = json.optJSONObject("market_data")
        val age = market?.optInt("feed_age_seconds", -1) ?: -1
        val freshness = if (age >= 0) "عمر الشموع ${age}ث" else "حداثة الشموع غير معروفة"
        val scoreText = if (score.isFinite()) "التوافق ${score.toInt()}/100" else "التوافق غير متاح"
        return "التحليل: $decision · $scoreText · $freshness"
    }

    private fun fetchNewsStatus(): String {
        val json = getJson("/api/v1/news/status")
            ?: return "الأخبار: حالة المحرك غير متاحة"
        val status = json.optString("overall_status", "UNKNOWN")
        val age = json.optInt("calendar_age_seconds", -1)
        val ageText = if (age >= 0) "عمر التقويم ${age}ث" else "عمر التقويم غير معروف"
        return "الأخبار: $status · $ageText"
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId, "GoldAI Market Monitor", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "مراقبة سعر الذهب والتحليل والأخبار في الخلفية" }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(message: String, connected: Boolean): Notification =
        NotificationCompat.Builder(this, channelId)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(if (connected) "GoldAI · مراقبة نشطة" else "GoldAI · إعادة الاتصال")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
