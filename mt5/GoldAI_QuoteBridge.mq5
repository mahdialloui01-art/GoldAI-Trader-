// GoldAI Trader - MT5 quote + closed-candle bridge (no order execution)
#property strict
#property version   "2.0"
#property description "Streams XM GOLD Bid/Ask and closed M5/M15/H1/H4 candles to GoldAI Trader."

input string BackendUrl = "https://YOUR_BACKEND_HOST/api/v1/mt5/quote";
input string CandleDataUrl = "https://YOUR_BACKEND_HOST/api/v1/mt5/candles";
input string BridgeToken = "CHANGE_ME";
input int SendIntervalSeconds = 1;
input int CandleRefreshSeconds = 60;
input int CandleBarsPerTimeframe = 120;
input string RequiredSymbol = "GOLD";

datetime lastCandleSend = 0;

int OnInit()
{
   if(StringLen(BridgeToken) < 24 ||
      StringFind(BackendUrl, "https://") != 0 ||
      StringFind(CandleDataUrl, "https://") != 0 ||
      CandleBarsPerTimeframe < 60 || CandleBarsPerTimeframe > 500)
   {
      Print("Configure HTTPS quote/candle URLs, a long BridgeToken and 60-500 candles.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(_Symbol != RequiredSymbol)
      Print("Attach this EA to ", RequiredSymbol, ". Current chart: ", _Symbol);
   EventSetTimer(MathMax(1, SendIntervalSeconds));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

string UtcStamp(datetime value)
{
   MqlDateTime utc;
   TimeToStruct(value, utc);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
      utc.year, utc.mon, utc.day, utc.hour, utc.min, utc.sec);
}

bool PostJson(string url, string body)
{
   char data[], result[];
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(data, ArraySize(data) - 1);
   string headers = "Content-Type: application/json\r\nAuthorization: Bearer " + BridgeToken + "\r\n";
   string responseHeaders;
   ResetLastError();
   int status = WebRequest("POST", url, headers, 8000, data, result, responseHeaders);
   if(status != 200)
   {
      Print("GoldAI bridge HTTP status=", status, " error=", GetLastError(), " url=", url);
      return false;
   }
   return true;
}

void SendQuote()
{
   MqlTick tick;
   if(!SymbolInfoTick(RequiredSymbol, tick) || tick.bid <= 0 || tick.ask < tick.bid)
      return;

   int digits = (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
   if(point <= 0) return;
   double spreadPoints = (tick.ask - tick.bid) / point;
   double tickSize = SymbolInfoDouble(RequiredSymbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValueLoss = SymbolInfoDouble(RequiredSymbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickValueLoss <= 0)
      tickValueLoss = SymbolInfoDouble(RequiredSymbol, SYMBOL_TRADE_TICK_VALUE);
   double tickValueProfit = SymbolInfoDouble(RequiredSymbol, SYMBOL_TRADE_TICK_VALUE_PROFIT);
   if(tickValueProfit <= 0)
      tickValueProfit = tickValueLoss;
   double volumeMin = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MIN);
   double volumeMax = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MAX);
   double volumeStep = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_STEP);
   double contractSize = SymbolInfoDouble(RequiredSymbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double accountEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   string accountCurrency = AccountInfoString(ACCOUNT_CURRENCY);
   string specJson = "";
   if(tickSize > 0 && tickValueLoss > 0 && volumeMin > 0 &&
      volumeMax >= volumeMin && volumeStep > 0 && contractSize > 0 &&
      accountEquity > 0 && StringLen(accountCurrency) >= 3)
   {
      specJson = StringFormat(
         ",\"tick_size\":%s,\"tick_value_loss\":%s,\"tick_value_profit\":%s,\"volume_min\":%s,\"volume_max\":%s,\"volume_step\":%s,\"contract_size\":%s,\"account_equity\":%s,\"account_currency\":\"%s\"",
         DoubleToString(tickSize, 10), DoubleToString(tickValueLoss, 8),
         DoubleToString(tickValueProfit, 8), DoubleToString(volumeMin, 8),
         DoubleToString(volumeMax, 8), DoubleToString(volumeStep, 8),
         DoubleToString(contractSize, 4), DoubleToString(accountEquity, 2),
         accountCurrency);
   }
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s,\"last\":%s,\"timestamp\":\"%s\",\"digits\":%d,\"point\":%s,\"spread_points\":%s%s}",
      RequiredSymbol, DoubleToString(tick.bid, digits), DoubleToString(tick.ask, digits),
      DoubleToString(tick.last, digits), UtcStamp(TimeGMT()), digits,
      DoubleToString(point, 10), DoubleToString(spreadPoints, 1), specJson);
   PostJson(BackendUrl, body);
}

string BuildCandleArray(ENUM_TIMEFRAMES timeframe, int requestedBars, int serverUtcOffset)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(RequiredSymbol, timeframe, 1, requestedBars, rates);
   if(copied < 60)
   {
      Print("GoldAI candle feed waiting for history. timeframe=", EnumToString(timeframe),
            " copied=", copied);
      return "";
   }

   string json = "[";
   // With series indexing, the highest index is the oldest closed bar.
   // Emit oldest-to-newest because the API rejects unordered timestamps.
   for(int i = copied - 1; i >= 0; i--)
   {
      datetime utcBarTime = rates[i].time - serverUtcOffset;
      if(StringLen(json) > 1) json += ",";
      json += StringFormat(
         "{\"time\":\"%s\",\"open\":%s,\"high\":%s,\"low\":%s,\"close\":%s,\"tick_volume\":%I64d}",
         UtcStamp(utcBarTime),
         DoubleToString(rates[i].open, (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS)),
         DoubleToString(rates[i].high, (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS)),
         DoubleToString(rates[i].low, (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS)),
         DoubleToString(rates[i].close, (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS)),
         rates[i].tick_volume);
   }
   json += "]";
   return json;
}

void SendCandleBatch()
{
   datetime serverNow = TimeTradeServer();
   if(serverNow <= 0) serverNow = TimeCurrent();
   int serverUtcOffset = (int)(serverNow - TimeGMT());

   string m5 = BuildCandleArray(PERIOD_M5, CandleBarsPerTimeframe, serverUtcOffset);
   string m15 = BuildCandleArray(PERIOD_M15, CandleBarsPerTimeframe, serverUtcOffset);
   string h1 = BuildCandleArray(PERIOD_H1, CandleBarsPerTimeframe, serverUtcOffset);
   string h4 = BuildCandleArray(PERIOD_H4, CandleBarsPerTimeframe, serverUtcOffset);
   if(m5 == "" || m15 == "" || h1 == "" || h4 == "")
   {
      Print("GoldAI candle batch not sent: one or more timeframes have insufficient history.");
      return;
   }

   string body = StringFormat(
      "{\"symbol\":\"%s\",\"m5\":%s,\"m15\":%s,\"h1\":%s,\"h4\":%s}",
      RequiredSymbol, m5, m15, h1, h4);
   if(PostJson(CandleDataUrl, body))
      Print("GoldAI closed candle batch sent. Bars per timeframe: ", CandleBarsPerTimeframe);
}

void OnTimer()
{
   if(_Symbol != RequiredSymbol) return;
   SendQuote();

   datetime now = TimeCurrent();
   if(lastCandleSend == 0 || now - lastCandleSend >= MathMax(30, CandleRefreshSeconds))
   {
      SendCandleBatch();
      lastCandleSend = now;
   }
}
