package com.goldaitrader.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.lifecycleScope
import androidx.compose.foundation.background
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

private val Night = Color(0xFF0B1018)
private val Panel = Color(0xFF151F2D)
private val PanelSoft = Color(0xFF1B2939)
private val Gold = Color(0xFFFFC857)
private val Muted = Color(0xFF91A0B4)
private val Green = Color(0xFF39D6A0)
private val Red = Color(0xFFFF7373)
private val Blue = Color(0xFF69B7FF)

private data class ChartCandle(val open: Double, val high: Double, val low: Double, val close: Double)

private enum class AppTab(val title: String, val symbol: String) {
    HOME("الرئيسية", "⌂"),
    ANALYSIS("التحليل", "⌁"),
    RISK("المخاطر", "◈"),
    SYSTEM("النظام", "⚙")
}

class MainActivity : ComponentActivity() {
    private var backendStatus by mutableStateOf("لم يتم فحص الاتصال")
    private var goldPrice by mutableStateOf("—")
    private var quoteStatus by mutableStateOf("في انتظار بيانات الوسيط")
    private var quoteReceived by mutableStateOf(false)
    private var quoteLoading = false
    private var macroSummary by mutableStateOf("لم يتم جلب البيانات الاقتصادية")
    private var calendarSummary by mutableStateOf("لم يتم فحص الأخبار")
    private var newsEngineSummary by mutableStateOf("في انتظار أول تحديث آلي")
    private var newsHeadlinesSummary by mutableStateOf("في انتظار العناوين الرسمية")
    private var expertSummary by mutableStateOf("لم يتم جلب التحليل بعد")
    private var candleSummary by mutableStateOf("لم يتم فحص استقبال الشموع")
    private var candlesReceived by mutableStateOf(false)
    private var chartRows by mutableStateOf<List<ChartCandle>>(emptyList())
    private var chartReport by mutableStateOf("بانتظار تقرير الرسم والتحليل")
    private var selectedTab by mutableStateOf(AppTab.HOME)
    private val backendConfigured: Boolean get() = BuildConfig.API_BASE_URL.trim().startsWith("https://") || BuildConfig.API_BASE_URL.trim().startsWith("http://")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                background = Night, surface = Panel, primary = Gold,
                onBackground = Color.White, onSurface = Color.White
            )) {
                GoldAiApp(
                    selectedTab = selectedTab,
                    onSelectTab = { selectedTab = it },
                    backendStatus = backendStatus,
                    goldPrice = goldPrice,
                    quoteStatus = quoteStatus,
                    quoteReceived = quoteReceived,
                    macroSummary = macroSummary,
                    calendarSummary = calendarSummary,
                    newsEngineSummary = newsEngineSummary,
                    newsHeadlinesSummary = newsHeadlinesSummary,
                    expertSummary = expertSummary,
                    candleSummary = candleSummary,
                    candlesReceived = candlesReceived,
                    chartRows = chartRows,
                    chartReport = chartReport,
                    onCheckBackend = ::checkBackend,
                    onRefreshGold = ::refreshGold,
                    onRefreshContext = ::refreshEconomicContext
                )
            }
        }
    }

    private fun checkBackend() {
        if (!backendConfigured) {
            backendStatus = "عنوان الخادم غير مضبوط — يلزم نشر Backend وربط عنوان HTTPS"
            return
        }
        backendStatus = "جارٍ فحص الخادم..."
        lifecycleScope.launch {
            backendStatus = withContext(Dispatchers.IO) {
                try {
                    val c = URL("${BuildConfig.API_BASE_URL}/health").openConnection() as HttpURLConnection
                    c.requestMethod = "GET"
                    c.connectTimeout = 5000
                    c.readTimeout = 5000
                    try {
                        if (c.responseCode == 200) "متصل بالخادم بنجاح"
                        else "الخادم ردّ برمز ${c.responseCode}"
                    } finally { c.disconnect() }
                } catch (_: Exception) { "تعذر الاتصال — تحقق من الخادم والشبكة" }
            }
        }
    }

    private fun refreshEconomicContext() {
        if (!backendConfigured) {
            macroSummary = "متوقف: لم يُضبط عنوان خادم HTTPS بعد"
            calendarSummary = "متوقف: لم يُضبط عنوان خادم HTTPS بعد"
            expertSummary = "متوقف: لم يُضبط عنوان خادم HTTPS بعد"
            return
        }
        macroSummary = "جارٍ جلب مؤشرات الدولار والعوائد..."
        calendarSummary = "جارٍ فحص الأخبار الأمريكية عالية التأثير..."
        expertSummary = "جارٍ جلب التحليل الفني متعدد الأطر..."
        candleSummary = "جارٍ فحص شموع MT5..."
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                fun request(path: String): JSONObject {
                    val c = URL("${BuildConfig.API_BASE_URL}$path").openConnection() as HttpURLConnection
                    c.requestMethod = "GET"
                    c.connectTimeout = 8000
                    c.readTimeout = 10000
                    try {
                        val code = c.responseCode
                        val stream = if (code in 200..299) c.inputStream else c.errorStream
                        val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                        if (code !in 200..299) {
                            val detail = try { JSONObject(body).optString("detail", "HTTP $code") } catch (_: Exception) { "HTTP $code" }
                            throw IllegalStateException(detail)
                        }
                        return JSONObject(body)
                    } finally { c.disconnect() }
                }
                val macro = try {
                    val j = request("/api/v1/analysis/gold-macro")
                    val bias = j.optString("bias", "غير محدد")
                    val explanation = j.optString("explanation", "لا يوجد تفسير")
                    "الانحياز: $bias — $explanation"
                } catch (e: Exception) {
                    "غير متاح: ${e.message ?: "تعذر الاتصال بمصدر FRED"}"
                }
                val news = try {
                    val j = request("/api/v1/analysis/news-filter")
                    val decision = j.optString("decision", "غير محدد")
                    val reason = j.optString("reason", "لا يوجد تفسير")
                    val count = j.optJSONArray("active_events")?.length() ?: 0
                    val upcoming = j.optJSONArray("upcoming_events")
                    val nextEvent = if (upcoming != null && upcoming.length() > 0) {
                        val item = upcoming.optJSONObject(0)
                        if (item != null) " — القادم: ${item.optString("event")} (${item.optString("date")})" else ""
                    } else {
                        " — لا توجد أحداث عالية التأثير مجدولة في الأيام القادمة"
                    }
                    val attribution = j.optString("calendar_attribution", "Data: XOOMAR — xoomar.com/markets/api/calendar")
                    "القرار: $decision — أحداث نشطة: $count — $reason$nextEvent — المصدر: $attribution"
                } catch (e: Exception) {
                    "غير متاح: ${e.message ?: "التقويم الاقتصادي غير متصل"}"
                }
                val engine = try {
                    val j = request("/api/v1/news/status")
                    val state = j.optString("overall_status", "UNKNOWN")
                    val interval = (j.optInt("poll_interval_seconds", 300) / 60).coerceAtLeast(1)
                    val age = j.optInt("calendar_age_seconds", -1)
                    val last = j.optString("calendar_last_success_at", "لم يصل بعد")
                    "الحالة: $state — التحديث الآلي كل $interval دقائق — عمر التقويم: ${if (age >= 0) "$age ثانية" else "غير متاح"} — آخر نجاح: $last"
                } catch (e: Exception) {
                    "حالة المحرك غير متاحة حالياً: ${e.message ?: "تعذر الاتصال"}"
                }
                val headlines = try {
                    val j = request("/api/v1/news/feed")
                    val items = j.optJSONArray("headlines")
                    if (items == null || items.length() == 0) {
                        "لا توجد عناوين جديدة متاحة حالياً — الحالة: ${j.optString("status", "غير محدد")}"
                    } else {
                        (0 until minOf(3, items.length())).joinToString("\n") { index ->
                            val item = items.optJSONObject(index)
                            "• ${item?.optString("title", "عنوان غير متاح") ?: "عنوان غير متاح"} — ${item?.optString("source", "") ?: ""}"
                        }
                    }
                } catch (e: Exception) {
                    "العناوين الرسمية غير متاحة: ${e.message ?: "تعذر الاتصال"}"
                }
                var candles = "لم يرجع الخادم تفاصيل استقبال الشموع"
                var hasCandles = false
                val expert = try {
                    val j = request("/api/v1/analysis/expert/live")
                    val market = j.optJSONObject("market_data")
                    if (market != null) {
                        val counts = market.optJSONObject("candle_counts")
                        val m5 = counts?.optInt("M5", 0) ?: 0
                        val m15 = counts?.optInt("M15", 0) ?: 0
                        val h1 = counts?.optInt("H1", 0) ?: 0
                        val h4 = counts?.optInt("H4", 0) ?: 0
                        val age = market.optInt("feed_age_seconds", -1)
                        hasCandles = listOf(m5, m15, h1, h4).all { it >= 60 } && age in 0..180
                        candles = "شموع MT5: M5=$m5، M15=$m15، H1=$h1، H4=$h4 — عمر آخر دفعة: ${if (age >= 0) "$age ثانية" else "غير معروف"}"
                        if (hasCandles) candles = "وصلت الشموع بنجاح — $candles"
                        else candles = "بيانات الشموع غير مكتملة أو قديمة — $candles"
                    }
                    val decision = j.optString("decision", "WAIT")
                    val dossier = j.optJSONObject("technical_dossier")
                    val blockers = j.optJSONArray("blockers")
                    val blockText = if (blockers != null && blockers.length() > 0) {
                        (0 until minOf(4, blockers.length())).joinToString("، ") { blockers.optString(it) }
                    } else "لا توجد موانع مسجلة"
                    if (dossier == null) {
                        "القرار: $decision — لم يرجع الخادم ملف التحليل الفني. $blockText"
                    } else {
                        val score = dossier.optDouble("confluence_score", 0.0)
                        val grade = dossier.optString("confluence_grade", "غير محدد")
                        val dominant = dossier.optString("dominant_technical_direction", "WAIT")
                        "القرار: $decision — توافق: ${score.toInt()}/100 ($grade) — الاتجاه الفني: $dominant — الموانع: $blockText"
                    }
                } catch (e: Exception) {
                    candles = "تعذر جلب حالة شموع MT5: ${e.message ?: "خطأ اتصال"}"
                    "تعذر جلب التحليل: ${e.message ?: "خطأ اتصال بالخادم"}"
                }
                val chartPayload = try { request("/api/v1/analysis/chart?timeframe=M5").toString() } catch (_: Exception) { "{}" }
                listOf(macro, news, engine, headlines, expert, candles, hasCandles.toString(), chartPayload)
            }
            macroSummary = result[0]
            calendarSummary = result[1]
            newsEngineSummary = result[2]
            newsHeadlinesSummary = result[3]
            expertSummary = result[4]
            candleSummary = result[5]
            candlesReceived = result[6] == "true"
            try {
                val chart = JSONObject(result[7])
                val rows = chart.optJSONArray("candles_by_timeframe")?.optJSONArray("M5")
                chartRows = if (rows == null) emptyList() else (0 until rows.length()).mapNotNull { i ->
                    val item = rows.optJSONObject(i) ?: return@mapNotNull null
                    ChartCandle(item.optDouble("open"), item.optDouble("high"), item.optDouble("low"), item.optDouble("close"))
                }
                val report = chart.optJSONObject("reports_by_timeframe")?.optJSONObject("M5")
                chartReport = if (report == null) "لا يوجد تقرير فني متاح لهذا الإطار." else
                    "الاتجاه: \${report.optString("direction", "WAIT")}\n" +
                    "حالة السوق: \${report.optString("market_regime", "غير محدد")}\n" +
                    "هيكل السوق: \${report.optString("market_structure", "غير محدد")}\n" +
                    "RSI: \${report.optDouble("rsi14")}  •  ADX: \${report.optDouble("adx14")}\n" +
                    "ATR: \${report.optDouble("atr14")}  •  MACD: \${report.optDouble("macd_histogram")}\n" +
                    "الأحداث: \${report.optJSONArray("confirmed_events")?.toString() ?: "لا يوجد"}\n" +
                    "الأدلة: \${report.optJSONArray("evidence")?.toString() ?: "غير متاحة"}"
            } catch (_: Exception) {
                chartRows = emptyList()
                chartReport = "تعذر قراءة بيانات الرسم والتحليل."
            }
        }
    }

    private fun refreshGold() {
        if (quoteLoading) return
        if (!backendConfigured) {
            goldPrice = "غير متاح"
            quoteReceived = false
            quoteStatus = "يلزم خادم HTTPS متاح؛ لا يوجد اتصال محلي من الهاتف"
            return
        }
        quoteLoading = true
        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    try {
                        val c = URL("${BuildConfig.API_BASE_URL}/api/v1/market/snapshot")
                            .openConnection() as HttpURLConnection
                        c.requestMethod = "GET"
                        c.connectTimeout = 8000
                        c.readTimeout = 8000
                        try {
                            if (c.responseCode != 200) throw IllegalStateException("HTTP ${c.responseCode}")
                            val j = JSONObject(c.inputStream.bufferedReader().use { it.readText() })
                            val bid = j.getDouble("bid")
                            val ask = j.getDouble("ask")
                            val age = j.optInt("age_seconds", -1)
                            val spread = j.optDouble("spread_points", 0.0) * j.optDouble("point", 0.0)
                            val mid = "$" + String.format(Locale.US, "%,.2f", j.getDouble("mid"))
                            val details = "Bid ${String.format(Locale.US, "%.2f", bid)}  •  Ask ${String.format(Locale.US, "%.2f", ask)}  •  Spread ${String.format(Locale.US, "%.2f", spread)}  •  عمر السعر ${if (age >= 0) "${age}ث" else "؟"}"
                            Pair(mid, details)
                        } finally { c.disconnect() }
                    } catch (_: Exception) { null }
                }
                if (result == null) {
                    goldPrice = "غير متاح"
                    quoteReceived = false
                    quoteStatus = "تعذر جلب سعر حديث من MT5 — تحقق من اتصال المنصة"
                } else {
                    goldPrice = result.first
                    quoteReceived = true
                    quoteStatus = result.second
                }
            } finally {
                quoteLoading = false
            }
        }
    }
}

@Composable
private fun GoldAiApp(
    selectedTab: AppTab,
    onSelectTab: (AppTab) -> Unit,
    backendStatus: String,
    goldPrice: String,
    quoteStatus: String,
    quoteReceived: Boolean,
    macroSummary: String,
    calendarSummary: String,
    newsEngineSummary: String,
    newsHeadlinesSummary: String,
    expertSummary: String,
    candleSummary: String,
    candlesReceived: Boolean,
    chartRows: List<ChartCandle>,
    chartReport: String,
    onCheckBackend: () -> Unit,
    onRefreshGold: () -> Unit,
    onRefreshContext: () -> Unit
) {
    LaunchedEffect(Unit) {
        onCheckBackend()
        onRefreshGold()
        onRefreshContext()
        launch {
            while (true) {
                delay(1_000)
                onRefreshGold()
            }
        }
        launch {
            while (true) {
                delay(60_000)
                onRefreshContext()
            }
        }
    }
    Surface(Modifier.fillMaxSize(), color = Night) {
        Column(Modifier.fillMaxSize()) {
            Column(
                Modifier.weight(1f).verticalScroll(rememberScrollState())
                    .padding(start = 18.dp, end = 18.dp, top = 18.dp, bottom = 18.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                TopHeader()
                when (selectedTab) {
                    AppTab.HOME -> HomePage(goldPrice, quoteStatus, quoteReceived, onRefreshGold)
                    AppTab.ANALYSIS -> AnalysisPage(candlesReceived, candleSummary, macroSummary, calendarSummary, newsEngineSummary, newsHeadlinesSummary, expertSummary, chartRows, chartReport)
                    AppTab.RISK -> RiskPage()
                    AppTab.SYSTEM -> SystemPage(backendStatus, quoteReceived, onCheckBackend, onRefreshGold)
                }
                Text(
                    "GoldAI Trader  •  وضع تجريبي آمن — تنفيذ الصفقات الحقيقية معطّل",
                    Modifier.fillMaxWidth().padding(top = 4.dp),
                    color = Muted, fontSize = 10.sp
                )
            }
            NavigationBar(
                containerColor = Color(0xFF101925),
                contentColor = Color.White,
                tonalElevation = 0.dp
            ) {
                AppTab.values().forEach { tab ->
                    NavigationBarItem(
                        selected = selectedTab == tab,
                        onClick = { onSelectTab(tab) },
                        icon = { Text(tab.symbol, fontSize = 19.sp, fontWeight = FontWeight.Bold) },
                        label = { Text(tab.title, fontSize = 11.sp) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = Gold,
                            selectedTextColor = Gold,
                            indicatorColor = Color(0xFF3A3020),
                            unselectedIconColor = Muted,
                            unselectedTextColor = Muted
                        )
                    )
                }
            }
        }
    }
}

@Composable
private fun TopHeader() {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Text("GOLDAI", color = Gold, fontSize = 12.sp, fontWeight = FontWeight.ExtraBold, letterSpacing = 3.sp)
            Text("TRADER", color = Color.White, fontSize = 23.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
            Text("منصة تحليل الذهب الذكية", color = Muted, fontSize = 11.sp)
        }
        Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Surface(color = Color(0xFF19392F), shape = RoundedCornerShape(50)) {
                Text("●  DEMO MODE", Modifier.padding(horizontal = 11.dp, vertical = 6.dp), color = Green, fontSize = 10.sp, fontWeight = FontWeight.Bold)
            }
            Text("XM  /  GOLD", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
private fun HomePage(price: String, quoteStatus: String, quoteReceived: Boolean, onRefresh: () -> Unit) {
    SectionHeading("نظرة عامة", "ملخص السوق وحالة التحليل")
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(22.dp)) {
        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("XAU / USD  •  GOLD", color = Muted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                    Text(price, color = Color.White, fontSize = 32.sp, fontWeight = FontWeight.Bold)
                    Text(quoteStatus, color = if (quoteReceived) Green else Gold, fontSize = 10.sp)
                }
                Surface(color = Color(0xFF30291C), shape = RoundedCornerShape(14.dp)) {
                    Text("AU", Modifier.padding(13.dp), color = Gold, fontSize = 20.sp, fontWeight = FontWeight.Black)
                }
            }
            Divider(color = Color(0xFF2A3748))
            Text("مصدر السعر: وسيط XM عبر جسر MetaTrader 5 — لا نعتمد سعراً خارجياً للتنفيذ.", color = Muted, fontSize = 11.sp)
            GoldButton("تحديث سعر الوسيط", onRefresh)
        }
    }

    SectionHeading("قرار النظام", "الإشارة لا تتحول إلى أمر تداول تلقائي")
    Card(colors = CardDefaults.cardColors(containerColor = PanelSoft), shape = RoundedCornerShape(20.dp)) {
        Row(Modifier.fillMaxWidth().padding(18.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                Text("WAIT  /  انتظار", color = Gold, fontSize = 21.sp, fontWeight = FontWeight.ExtraBold)
                Text("لا توجد حالياً إشارة مؤكدة صالحة للتنفيذ.", color = Color.White, fontSize = 12.sp)
                Text("سيتم تفعيل التحليل عند وصول بيانات شموع حديثة ومكتملة.", color = Muted, fontSize = 10.sp)
            }
            Surface(color = Color(0xFF3B3020), shape = RoundedCornerShape(50)) {
                Text("Ⅱ", Modifier.padding(horizontal = 15.dp, vertical = 10.dp), color = Gold, fontSize = 22.sp, fontWeight = FontWeight.Bold)
            }
        }
    }

    SectionHeading("مؤشرات سريعة", "إعدادات الحماية الحالية")
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        MiniMetric("المخاطرة / صفقة", "0.25%", "حد الديمو", Modifier.weight(1f))
        MiniMetric("الخسارة اليومية", "10%", "حد إيقاف", Modifier.weight(1f))
    }
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        MiniMetric("وضع الحساب", "PAPER", "بدون أموال حقيقية", Modifier.weight(1f))
        MiniMetric("التنفيذ الآلي", "OFF", "مغلق للحماية", Modifier.weight(1f))
    }

    SectionHeading("أقسام المنصة", "كل أداة في مساحة مستقلة")
    FeatureTile("01", "التحليل الفني", "هيكل السوق • BOS • السيولة • FVG")
    FeatureTile("02", "التحليل متعدد الأطر", "M5 • M15 • H1 • H4")
    FeatureTile("03", "إدارة رأس المال", "حجم العقد • وقف الخسارة • حدود اليوم")
    FeatureTile("04", "اختبار الاستراتيجيات", "Backtesting • الأداء • السحب")
}

@Composable
private fun AnalysisPage(
    candlesReceived: Boolean,
    candleSummary: String,
    macroSummary: String,
    calendarSummary: String,
    newsEngineSummary: String,
    newsHeadlinesSummary: String,
    expertSummary: String,
    chartRows: List<ChartCandle>,
    chartReport: String
) {
    SectionHeading("مركز التحليل الاحترافي", "تحليل متعدد الطبقات للذهب")
    StatusBanner(if (candlesReceived) "تم استلام شموع MT5 للأطر الأربعة" else "لم يتم تأكيد حداثة واكتمال شموع MT5", if (candlesReceived) Green else Gold)
    SectionHeading("الرسم البياني للذهب", "شموع M5 حقيقية من MT5 — آخر 100 شمعة مغلقة")
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(18.dp)) {
        Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("GOLD · M5", color = Gold, fontWeight = FontWeight.Bold, fontSize = 12.sp)
            if (chartRows.size > 1) CandlestickChart(chartRows)
            else Text("بانتظار شموع MT5 كافية للرسم.", color = Muted, modifier = Modifier.padding(vertical = 30.dp))
            Text(chartReport, color = Color.White, fontSize = 11.sp)
        }
    }
    AnalysisCard("00", "حالة شموع MT5 الفعلية", "M5 • M15 • H1 • H4 — قراءة مباشرة من رد الخادم", candleSummary)
    AnalysisCard("01", "Price Action & Structure", "اتجاه السوق • Swing High/Low • BOS / CHoCH", if (candlesReceived) "تعمل على أحدث شموع MT5 المستلمة" else candleSummary)
    AnalysisCard("02", "Liquidity & Imbalance", "Equal Highs/Lows • Sweep • Fair Value Gap", if (candlesReceived) "بيانات الشموع متاحة للتحليل" else candleSummary)
    AnalysisCard("03", "Multi-Timeframe", "توافق M5 و M15 مع سياق H1 و H4", if (candlesReceived) "تم تحميل الأطر الأربعة؛ التوافق يحدده المحرك" else candleSummary)
    AnalysisCard("04", "Market Regime", "Trend • Range • Transition • Volatility", if (candlesReceived) "يتم تصنيف السوق من الشموع المستلمة" else candleSummary)
    AnalysisCard("05", "Gold Macro — FRED", "العوائد الأمريكية والدولار الأمريكي", macroSummary)
    AnalysisCard("06", "Economic News Filter", "الأحداث الأمريكية عالية التأثير ونافذة الحظر", calendarSummary)
    AnalysisCard("07", "Auto News Monitor", "تحديث تلقائي للخادم كل 5 دقائق ولعرض التطبيق كل دقيقة", newsEngineSummary)
    AnalysisCard("08", "Official Economic Headlines", "آخر العناوين من BLS والاحتياطي الفيدرالي", newsHeadlinesSummary)
    AnalysisCard("09", "Expert GOLD Technical Engine", "توافق M5/M15/H1/H4 • السيولة • BOS • FVG • خطة المخاطر", expertSummary)
    Text("البيانات الاقتصادية والأخبار تُعرض فقط إذا نجح الاتصال بمصادرها. تحليل الشموع لا يعمل قبل وصول OHLC حديث من MT5 Desktop/VPS.", color = Muted, fontSize = 11.sp)
}

@Composable
private fun CandlestickChart(candles: List<ChartCandle>) {
    val visible = candles.takeLast(70)
    val low = visible.minOf { it.low }
    val high = visible.maxOf { it.high }
    val range = (high - low).takeIf { it > 0.0 } ?: 1.0
    Canvas(Modifier.fillMaxWidth().height(230.dp).background(PanelSoft, RoundedCornerShape(12.dp))) {
        val pad = 8.dp.toPx()
        val plotHeight = size.height - 2 * pad
        val step = size.width / visible.size
        val bodyWidth = (step * 0.58f).coerceAtLeast(2f)
        repeat(5) { n ->
            val y = pad + plotHeight * n / 4f
            drawLine(Color(0xFF344356), Offset(0f, y), Offset(size.width, y), 1f)
        }
        visible.forEachIndexed { i, c ->
            val x = step * i + step / 2f
            fun py(v: Double) = pad + ((high - v) / range * plotHeight).toFloat()
            val tint = if (c.close >= c.open) Green else Red
            drawLine(tint, Offset(x, py(c.high)), Offset(x, py(c.low)), 1.4f)
            val y1 = py(c.open)
            val y2 = py(c.close)
            drawRect(tint, Offset(x - bodyWidth / 2f, minOf(y1, y2)), Size(bodyWidth, maxOf(2f, kotlin.math.abs(y2 - y1))))
        }
    }
}


@Composable
private fun RiskPage() {
    SectionHeading("مركز إدارة المخاطر", "الحماية تسبق البحث عن الربح")
    StatusBanner("التداول الحقيقي معطّل — Paper mode فقط", Green)
    RiskRow("المخاطرة القصوى لكل صفقة", "0.25% — ديمو")
    RiskRow("حد الخسارة اليومية", "10% — ديمو")
    RiskRow("حساب حجم العقد", "حسب مواصفات XM من MT5")
    RiskRow("تقريب حجم العقد", "للأسفل حسب Volume Step")
    RiskRow("الحد الأدنى للعقد", "رفض الصفقة إذا تجاوز المخاطرة")
    RiskRow("بيانات العقد", "Tick Size / Tick Value / Contract Size")
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(18.dp)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("قاعدة الحماية", color = Gold, fontWeight = FontWeight.Bold, fontSize = 14.sp)
            Text("إذا كانت بيانات الوسيط ناقصة أو قديمة، أو كان الحد الأدنى للعقد يرفع المخاطرة فوق المسموح، يرفض النظام الحساب بدلاً من التخمين.", color = Muted, fontSize = 12.sp)
        }
    }
}

@Composable
private fun SystemPage(backendStatus: String, quoteReceived: Boolean, onCheckBackend: () -> Unit, onRefresh: () -> Unit) {
    SectionHeading("مركز النظام والاتصال", "حالة الخدمات ومصدر البيانات")
    StatusRow("خادم التحليل", backendStatus, backendStatus.contains("بنجاح"))
    StatusRow("سعر GOLD من MT5", if (quoteReceived) "تم استلام السعر" else "في انتظار السعر", quoteReceived)
    StatusRow("وسيط XM", "يحتاج تأكيد الربط من MT5 Desktop", false)
    StatusRow("تنفيذ الصفقات الحقيقية", "معطّل للحماية", false)
    GoldButton("اختبار اتصال الخادم", onCheckBackend)
    OutlinedButton(
        onClick = onRefresh, modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(13.dp),
        colors = ButtonDefaults.outlinedButtonColors(contentColor = Gold)
    ) { Text("تحديث بيانات السوق") }
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(18.dp)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("متطلبات الربط الكامل", color = Color.White, fontWeight = FontWeight.Bold)
            Text("• تشغيل MetaTrader 5 Desktop أو VPS مع Quote Bridge", color = Muted, fontSize = 11.sp)
            Text("• تأكيد الرمز GOLD ومواصفات العقد لدى XM", color = Muted, fontSize = 11.sp)
            Text("• اختبار تدفق الأسعار والشموع قبل أي اختبار تنفيذ", color = Muted, fontSize = 11.sp)
        }
    }
    Text("API: ${BuildConfig.API_BASE_URL}", color = Muted, fontSize = 10.sp)
}

@Composable
private fun SectionHeading(title: String, subtitle: String) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text(title, color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text(subtitle, color = Muted, fontSize = 11.sp)
    }
}

@Composable
private fun MiniMetric(label: String, value: String, note: String, modifier: Modifier = Modifier) {
    Card(modifier, colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(16.dp)) {
        Column(Modifier.fillMaxWidth().padding(13.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(label, color = Muted, fontSize = 10.sp)
            Text(value, color = Gold, fontSize = 18.sp, fontWeight = FontWeight.ExtraBold)
            Text(note, color = Muted, fontSize = 9.sp)
        }
    }
}

@Composable
private fun FeatureTile(number: String, title: String, details: String) {
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(14.dp)) {
        Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(number, color = Gold, fontWeight = FontWeight.Bold, fontSize = 12.sp)
            Spacer(Modifier.width(13.dp))
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                Text(details, color = Muted, fontSize = 10.sp)
            }
        }
    }
}

@Composable
private fun AnalysisCard(number: String, title: String, details: String, status: String) {
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(16.dp)) {
        Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(title, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                Text(number, color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold)
            }
            Text(details, color = Muted, fontSize = 10.sp)
            Text("• $status", color = Gold, fontSize = 10.sp)
        }
    }
}

@Composable
private fun RiskRow(label: String, value: String) {
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(13.dp)) {
        Row(Modifier.fillMaxWidth().padding(15.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Text(label, color = Muted, fontSize = 11.sp, modifier = Modifier.weight(1f))
            Spacer(Modifier.width(10.dp))
            Text(value, color = Gold, fontSize = 12.sp, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
private fun StatusBanner(message: String, tint: Color) {
    Card(colors = CardDefaults.cardColors(containerColor = PanelSoft), shape = RoundedCornerShape(14.dp)) {
        Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("●", color = tint, fontSize = 13.sp)
            Spacer(Modifier.width(9.dp))
            Text(message, color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun StatusRow(label: String, value: String, good: Boolean) {
    Card(colors = CardDefaults.cardColors(containerColor = Panel), shape = RoundedCornerShape(13.dp)) {
        Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(label, color = Muted, fontSize = 10.sp)
            Text(value, color = if (good) Green else Gold, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun GoldButton(label: String, onClick: () -> Unit) {
    Button(
        onClick = onClick, modifier = Modifier.fillMaxWidth(),
        colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Night),
        shape = RoundedCornerShape(13.dp), contentPadding = PaddingValues(vertical = 13.dp)
    ) { Text(label, fontWeight = FontWeight.Bold, fontSize = 12.sp) }
}
