package com.goldaitrader.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.lifecycleScope
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

private val Night = Color(0xFF10131B)
private val Panel = Color(0xFF1B2230)
private val Gold = Color(0xFFFFC857)
private val Muted = Color(0xFF9AA6B8)
private val Green = Color(0xFF42D6A4)
private val Red = Color(0xFFFF7373)

class MainActivity : ComponentActivity() {
    private var backendStatus by mutableStateOf("لم يتم فحص الاتصال")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                background = Night,
                surface = Panel,
                primary = Gold,
                onBackground = Color.White,
                onSurface = Color.White
            )) {
                DashboardScreen(
                    backendStatus = backendStatus,
                    onCheckBackend = ::checkBackend
                )
            }
        }
    }

    private fun checkBackend() {
        backendStatus = "جارٍ فحص الخادم..."
        lifecycleScope.launch {
            backendStatus = withContext(Dispatchers.IO) {
                try {
                    val connection = URL("${BuildConfig.API_BASE_URL}/health")
                        .openConnection() as HttpURLConnection
                    connection.requestMethod = "GET"
                    connection.connectTimeout = 5000
                    connection.readTimeout = 5000
                    try {
                        if (connection.responseCode == 200) {
                            "متصل بالخادم بنجاح"
                        } else {
                            "الخادم ردّ برمز ${connection.responseCode}"
                        }
                    } finally {
                        connection.disconnect()
                    }
                } catch (_: Exception) {
                    "تعذر الاتصال — تحقق من عنوان الخادم والشبكة"
                }
            }
        }
    }
}

@Composable
private fun DashboardScreen(
    backendStatus: String,
    onCheckBackend: () -> Unit
) {
    Surface(modifier = Modifier.fillMaxSize(), color = Night) {
        Column(
            modifier = Modifier.fillMaxSize().padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text("GoldAI Trader", color = Gold, fontSize = 25.sp, fontWeight = FontWeight.Bold)
                    Text("تحليل الذهب بذكاء وهدوء", color = Muted, fontSize = 13.sp)
                }
                Text("DEMO", color = Green, fontWeight = FontWeight.Bold)
            }

            Card(
                colors = CardDefaults.cardColors(containerColor = Panel),
                shape = RoundedCornerShape(22.dp)
            ) {
                Column(Modifier.fillMaxWidth().padding(18.dp)) {
                    Text("XAUUSD · الذهب مقابل الدولار", color = Muted, fontSize = 13.sp)
                    Spacer(Modifier.height(10.dp))
                    Text("بانتظار بيانات السوق", color = Color.White, fontSize = 23.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(8.dp))
                    Text("السعر المباشر غير متصل بعد", color = Gold, fontSize = 13.sp)
                    Spacer(Modifier.height(12.dp))
                    Text(
                        "لن نعرض سعراً تقديرياً على أنه سعر حقيقي. سيظهر السعر بعد ربط مزود موثوق.",
                        color = Muted,
                        fontSize = 12.sp
                    )
                }
            }

            Text("قرار الذكاء الاصطناعي", color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
            Card(
                colors = CardDefaults.cardColors(containerColor = Panel),
                shape = RoundedCornerShape(18.dp)
            ) {
                Column(Modifier.fillMaxWidth().padding(18.dp)) {
                    Text("WAIT · انتظار", color = Gold, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(6.dp))
                    Text("لا توجد إشارة تداول مؤكدة. لن يتم فتح أي صفقة تلقائياً.", color = Muted, fontSize = 13.sp)
                }
            }

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                MetricCard("وضع التداول", "Paper", Modifier.weight(1f))
                MetricCard("حد المخاطرة", "0.5%", Modifier.weight(1f))
            }

            Card(
                colors = CardDefaults.cardColors(containerColor = Panel),
                shape = RoundedCornerShape(18.dp)
            ) {
                Column(Modifier.fillMaxWidth().padding(18.dp)) {
                    Text("حالة الربط", color = Color.White, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(8.dp))
                    StatusLine("خادم التحليل", backendStatus)
                    StatusLine("بيانات الذهب", "غير متصلة")
                    StatusLine("MetaTrader 5", "غير مربوط")
                    Spacer(Modifier.height(12.dp))
                    Button(
                        onClick = onCheckBackend,
                        modifier = Modifier.fillMaxWidth(),
                        colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Night),
                        shape = RoundedCornerShape(12.dp)
                    ) {
                        Text("اختبر اتصال الخادم", fontWeight = FontWeight.Bold)
                    }
                    Text(
                        "عنوان التطوير الافتراضي: ${BuildConfig.API_BASE_URL}",
                        color = Muted,
                        fontSize = 10.sp
                    )
                }
            }

            Spacer(Modifier.weight(1f))
            Text(
                "نسخة تطويرية · التداول الحقيقي معطّل",
                modifier = Modifier.align(Alignment.CenterHorizontally),
                color = Muted,
                fontSize = 11.sp
            )
        }
    }
}

@Composable
private fun MetricCard(label: String, value: String, modifier: Modifier = Modifier) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(containerColor = Panel),
        shape = RoundedCornerShape(16.dp)
    ) {
        Column(Modifier.padding(14.dp)) {
            Text(label, color = Muted, fontSize = 12.sp)
            Spacer(Modifier.height(7.dp))
            Text(value, color = Gold, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
private fun StatusLine(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, color = Muted, fontSize = 12.sp)
        Text(
            value,
            color = if (value.contains("بنجاح")) Green else if (value.contains("تعذر")) Red else Gold,
            fontSize = 11.sp
        )
    }
}
