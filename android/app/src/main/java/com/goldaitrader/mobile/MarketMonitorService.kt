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
 * User-visible phone monitor. The backend remains the source of analysis;
 * the MT5 EA on the desktop remains responsible for execution and position management.
 */
class MarketMonitorService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val channelId = "goldai_market_monitor"
    private val notificationId = 7401

    @Volatile private var analysisLine = "WAIT"
    @Volatile private var newsLine = "غير متاحة"
    @Volatile private var macroLine = "غير متاح"
    @Volatile private var headlinesLine = "لا توجد عناوين محملة"
    @Volatile private var lastError = ""

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(notificationId, buildNotification("جارٍ تشغيل مراقبة السوق", false))
        serviceScope.launch {
            var lastAnalysisAt = 0L
            var lastNewsAt = 0L
            var lastMacroAt = 0L
            var lastHeadlinesAt = 0L

            while (isActive) {
                val now = SystemClock.elapsedRealtime()
                val market = fetchMarketState()

                if (now - lastAnalysisAt >= 60_000L) {
                    analysisLine = fetchExpertAnalysis()
                    lastAnalysisAt = now
                }
                if (now - lastNewsAt >= 300_000L) {
                    newsLine = fetchNewsStatus()
                    lastNewsAt = now
                }
                if (now - lastMacroAt >= 300_000L) {
                    macroLine = fetchMacroContext()
                    lastMacroAt = now
                }
                if (now - lastHeadlinesAt >= 300_000L) {
                    headlinesLine = fetchHeadlines()
                    lastHeadlinesAt = now
                }

                val connected = market.connected
                val notificationText = if (connected) {
                    "GOLD ${market.price} · ${analysisLine.take(70)} · أخبار: ${newsLine.take(55)}"
                } else {
                    "لا توجد تغذية سعر حديثة من MT5؛ إعادة المحاولة تلقائياً"
                }

                val prefs = getSharedPreferences("goldai_monitor", MODE_PRIVATE)
                prefs.edit()
                    .putString("connection", if (connected) "متصل" else "منقطع")
                    .putString("price", market.price)
                    .putString("analysis", analysisLine)
                    .putString("news", newsLine)
                    .putString("macro", macroLine)
                    .putString("headlines", headlinesLine)
                    .putString("last_error", lastError)
                    .putLong("updated_at", System.currentTimeMillis())
                    .apply()

                getSystemService(NotificationManager::class.java)
                    .notify(notificationId, buildNotification(notificationText, connected))

                // The phone monitors and displays state only; it never places orders itself.
                delay(if (connected) 15_000L else 30_000L)
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
            val code = connection.responseCode
            if (code !in 200..299) {
                lastError = "$path HTTP $code"
                null
            } else {
                lastError = ""
                JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
            }
        } catch (e: Exception) {
            lastError = "$path: ${e.message ?: "connection error"}".take(180)
            null
        } finally {
            connection?.disconnect()
        }
    }

    private fun fetchMarketState(): MarketState {
        val json = getJson("/api/v1/market/snapshot")
            ?: return MarketState(false, "غير متاح")
        val mid = json.optDouble("mid", Double.NaN)
        val age = json.optInt("age_seconds", -1)
        return if (!mid.isFinite() || age !in 0..90) {
            MarketState(false, "سعر قديم")
        } else {
            MarketState(true, "$" + String.format(Locale.US, "%,.2f", mid))
        }
    }

    private fun fetchExpertAnalysis(): String {
        val json = getJson("/api/v1/analysis/expert/live")
            ?: return "WAIT · التحليل غير متاح"
        val entryEngine = json.optJSONObject("entry_engine")
        val decision = entryEngine?.optString("direction", json.optString("decision", "WAIT"))
            ?: json.optString("decision", "WAIT")
        val score = entryEngine?.optDouble("entry_quality_score_0_to_100", Double.NaN)
            ?: Double.NaN
        val strategy = entryEngine?.optString("selected_strategy", "—") ?: "—"
        val market = json.optJSONObject("market_data")
        val age = market?.optInt("feed_age_seconds", -1) ?: -1
        val scoreText = if (score.isFinite()) "\${score.toInt()}/100" else "—"
        val freshness = if (age >= 0) "\${age}ث" else "؟"
        val blockers = entryEngine?.optJSONArray("blockers")
        val blockerText = if (blockers != null && blockers.length() > 0) {
            (0 until minOf(2, blockers.length())).joinToString("، ") { blockers.optString(it) }
        } else "لا توجد موانع"
        return "$decision · جودة $scoreText · $strategy · شموع $freshness · $blockerText".take(180)
    }

    private fun fetchNewsStatus(): String {
        val json = getJson("/api/v1/analysis/news-filter")
            ?: return "غير متاحة حالياً"
        val decision = json.optString("decision", "غير محدد")
        val count = json.optJSONArray("active_events")?.length() ?: 0
        val reason = json.optString("reason", "")
        return "$decision · أحداث نشطة $count · $reason".take(180)
    }

    private fun fetchMacroContext(): String {
        val json = getJson("/api/v1/analysis/gold-macro") ?: return "غير متاح حالياً"
        return "${json.optString("bias", "غير محدد")} · ${json.optString("explanation", "")}".take(180)
    }

    private fun fetchHeadlines(): String {
        val json = getJson("/api/v1/news/feed") ?: return "تعذر جلب العناوين"
        val items = json.optJSONArray("headlines") ?: return "لا توجد عناوين حالياً"
        if (items.length() == 0) return "لا توجد عناوين جديدة"
        return (0 until minOf(3, items.length())).joinToString(" | ") { index ->
            items.optJSONObject(index)?.optString("title", "عنوان غير متاح") ?: "عنوان غير متاح"
        }.take(350)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId, "GoldAI Market Monitor", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "مراقبة سعر الذهب والتحليل الفني والأخبار الاقتصادية" }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(message: String, connected: Boolean): Notification =
        NotificationCompat.Builder(this, channelId)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(if (connected) "GoldAI · مراقبة نشطة" else "GoldAI · فحص الاتصال")
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
