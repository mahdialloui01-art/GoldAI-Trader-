// GoldAI Trader - MT5 broker quote bridge (quotes only; no order execution)
#property strict
#property version   "1.1"
#property description "Streams the broker GOLD Bid/Ask quote to GoldAI Trader."

input string BackendUrl = "https://YOUR_BACKEND_HOST/api/v1/mt5/quote";
input string BridgeToken = "CHANGE_ME";
input int SendIntervalSeconds = 1;
input string RequiredSymbol = "GOLD";

int OnInit()
{
   if(StringLen(BridgeToken) < 24 || StringFind(BackendUrl, "https://") != 0)
   {
      Print("Configure HTTPS BackendUrl and a long BridgeToken before starting.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(_Symbol != RequiredSymbol)
      Print("Attach this EA to ", RequiredSymbol, ". Current chart: ", _Symbol);
   EventSetTimer(MathMax(1, SendIntervalSeconds));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   if(_Symbol != RequiredSymbol) return;
   MqlTick tick;
   if(!SymbolInfoTick(RequiredSymbol, tick) || tick.bid <= 0 || tick.ask < tick.bid)
      return;

   MqlDateTime utc;
   TimeToStruct(TimeGMT(), utc);
   string stamp = StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
      utc.year, utc.mon, utc.day, utc.hour, utc.min, utc.sec);
   int digits = (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
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
      DoubleToString(tick.last, digits), stamp, digits,
      DoubleToString(point, 10), DoubleToString(spreadPoints, 1), specJson);

   char data[], result[];
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(data, ArraySize(data) - 1);
   string headers = "Content-Type: application/json\r\nAuthorization: Bearer " + BridgeToken + "\r\n";
   string responseHeaders;
   ResetLastError();
   int status = WebRequest("POST", BackendUrl, headers, 5000, data, result, responseHeaders);
   if(status != 200)
      Print("GoldAI quote bridge HTTP status=", status, " error=", GetLastError());
}
