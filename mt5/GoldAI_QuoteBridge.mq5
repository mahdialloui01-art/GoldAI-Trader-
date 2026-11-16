// GoldAI Trader - MT5 bridge with optional guarded DEMO-only execution
#property strict
#include <Trade/Trade.mqh>
CTrade trade;
#property version   "2.1"
#property description "Streams XM GOLD Bid/Ask and closed M5/M15/H1/H4 candles to GoldAI Trader."

input string BackendUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/quote";
input string CandleDataUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/candles";
input string DemoSignalUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/demo/signal";
input string ManualTradeUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/manual/next";
input string BridgeToken = "CHANGE_ME";
input int MinQuoteIntervalMilliseconds = 250;
input int CandleRefreshSeconds = 60;
input int CandleBarsPerTimeframe = 120;
input string RequiredSymbol = "GOLD";
input bool EnableDemoExecution = true;
input double DemoDailyLossLimitPercent = 10.0;
input double DemoMaxRiskPerTradePercent = 0.25;
input ulong DemoMagicNumber = 26111601;
input int DemoMaxDeviationPoints = 30;
input double DemoMaxSpreadPoints = 1000.0;
input bool EnableDemoPositionManagement = true;
input double BreakEvenTriggerR = 1.0;
input double BreakEvenLockR = 0.05;
input double PartialCloseTriggerR = 1.5;
input double PartialClosePercent = 50.0;
input double AtrTrailStartR = 2.0;
input double AtrTrailMultiple = 1.5;

datetime lastCandleSend = 0;
ulong lastQuoteSendMs = 0;
datetime lastDemoDecisionBar = 0;

int OnInit()
{
   if(StringLen(BridgeToken) < 24 ||
      StringFind(BackendUrl, "https://") != 0 ||
      StringFind(CandleDataUrl, "https://") != 0 ||
      StringFind(DemoSignalUrl, "https://") != 0 ||
      StringFind(ManualTradeUrl, "https://") != 0 ||
      DemoDailyLossLimitPercent <= 0 || DemoDailyLossLimitPercent > 10.0 ||
      DemoMaxRiskPerTradePercent <= 0 || DemoMaxRiskPerTradePercent > 0.5 ||
      BreakEvenTriggerR <= 0 || BreakEvenLockR < 0 || PartialCloseTriggerR <= 0 ||
      PartialClosePercent <= 0 || PartialClosePercent >= 100 || AtrTrailStartR <= 0 || AtrTrailMultiple <= 0 ||
      DemoMaxDeviationPoints < 0 || DemoMaxSpreadPoints <= 0 ||
      CandleBarsPerTimeframe < 60 || CandleBarsPerTimeframe > 500 ||
      MinQuoteIntervalMilliseconds < 50 || MinQuoteIntervalMilliseconds > 5000)
   {
      Print("Configure HTTPS quote/candle URLs, a long BridgeToken and 60-500 candles.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(_Symbol != RequiredSymbol)
      Print("Attach this EA to ", RequiredSymbol, ". Current chart: ", _Symbol);
   if(EnableDemoExecution && !IsDemoAccount())
      Print("SAFETY LOCK: execution enabled locally but account is not DEMO. No orders will be sent.");
   if(EnableDemoExecution && IsDemoAccount())
      Print("GoldAI DEMO execution armed locally; server-side demo gate must also be enabled.");
   EventSetTimer(1);
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
      string responseBody = CharArrayToString(result, 0, -1, CP_UTF8);
      Print("GoldAI bridge HTTP status=", status, " error=", GetLastError(),
            " url=", url, " response=", responseBody);
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
   datetime serverNow = TimeTradeServer();
   if(serverNow <= 0) serverNow = TimeCurrent();
   int serverUtcOffset = (int)(serverNow - TimeGMT());
   string tickTimestamp = UtcStamp(tick.time - serverUtcOffset);
   if(tick.time_msc > 0)
   {
      int milliseconds = (int)(tick.time_msc % 1000);
      tickTimestamp = StringSubstr(tickTimestamp, 0, StringLen(tickTimestamp) - 1) +
                      StringFormat(".%03dZ", milliseconds);
   }
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s,\"last\":%s,\"timestamp\":\"%s\",\"digits\":%d,\"point\":%s,\"spread_points\":%s%s}",
      RequiredSymbol, DoubleToString(tick.bid, digits), DoubleToString(tick.ask, digits),
      DoubleToString(tick.last, digits), tickTimestamp, digits,
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


bool IsDemoAccount()
{
   return (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO;
}

double TodayAccountLossPercent()
{
   datetime now = TimeTradeServer();
   if(now <= 0) now = TimeCurrent();
   MqlDateTime parts;
   TimeToStruct(now, parts);
   parts.hour = 0; parts.min = 0; parts.sec = 0;
   datetime dayStart = StructToTime(parts);
   if(!HistorySelect(dayStart, now)) return 100.0;
   double realized = 0.0;
   for(int i = 0; i < HistoryDealsTotal(); i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      ENUM_DEAL_TYPE type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
      realized += HistoryDealGetDouble(ticket, DEAL_PROFIT);
      realized += HistoryDealGetDouble(ticket, DEAL_SWAP);
      realized += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
   }
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double startBalance = balance - realized;
   if(startBalance <= 0) return 100.0;
   double netToday = realized + AccountInfoDouble(ACCOUNT_EQUITY) - balance;
   return MathMax(0.0, -netToday / startBalance * 100.0);
}

int CountOurOpenPositions()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) == RequiredSymbol) count++;
   }
   return count;
}

string JsonValue(string payload, string key)
{
   string marker = "\"" + key + "\":";
   int start = StringFind(payload, marker);
   if(start < 0) return "";
   start += StringLen(marker);
   while(start < StringLen(payload) && StringGetCharacter(payload, start) == ' ') start++;
   if(start >= StringLen(payload)) return "";
   if(StringGetCharacter(payload, start) == '"')
   {
      start++;
      int finish = StringFind(payload, "\"", start);
      if(finish < 0) return "";
      return StringSubstr(payload, start, finish - start);
   }
   int finish = start;
   while(finish < StringLen(payload) && StringGetCharacter(payload, finish) != ',' &&
         StringGetCharacter(payload, finish) != '}') finish++;
   string value = StringSubstr(payload, start, finish - start);
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

double JsonNumber(string payload, string key)
{
   return StringToDouble(JsonValue(payload, key));
}

bool FetchDemoSignal(double dailyLoss, string &payload)
{
   string url = DemoSignalUrl + "?daily_loss_percent=" + DoubleToString(dailyLoss, 4);
   char data[], result[];
   ArrayResize(data, 0);
   string headers = "Authorization: Bearer " + BridgeToken + "\r\n";
   string responseHeaders;
   ResetLastError();
   int status = WebRequest("GET", url, headers, 10000, data, result, responseHeaders);
   if(status != 200)
   {
      Print("GoldAI demo signal HTTP status=", status, " error=", GetLastError());
      return false;
   }
   payload = CharArrayToString(result, 0, -1, CP_UTF8);
   return StringLen(payload) > 0;
}

bool FetchManualTrade(string &payload)
{
   char data[], result[];
   ArrayResize(data, 0);
   string headers = "Authorization: Bearer " + BridgeToken + "\r\n";
   string responseHeaders;
   ResetLastError();
   int status = WebRequest("GET", ManualTradeUrl, headers, 8000, data, result, responseHeaders);
   if(status != 200)
   {
      if(status != -1)
         Print("GoldAI manual queue HTTP status=", status, " error=", GetLastError());
      return false;
   }
   payload = CharArrayToString(result, 0, -1, CP_UTF8);
   return StringLen(payload) > 0 && JsonValue(payload, "available") == "true";
}

void TryManualDemoTrade()
{
   if(!EnableDemoExecution || !IsDemoAccount()) return;
   if(_Symbol != RequiredSymbol || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !MQLInfoInteger(MQL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) return;

   string payload;
   if(!FetchManualTrade(payload)) return;
   string direction = JsonValue(payload, "direction");
   string symbol = JsonValue(payload, "symbol");
   double lots = JsonNumber(payload, "lots");
   double slPoints = JsonNumber(payload, "stop_loss_points");
   double tpPoints = JsonNumber(payload, "take_profit_points");
   if(symbol != RequiredSymbol || (direction != "BUY" && direction != "SELL") ||
      lots <= 0 || slPoints <= 0 || tpPoints <= 0)
   {
      Print("GoldAI manual DEMO request rejected locally: invalid command fields.");
      return;
   }
   double dailyLoss = TodayAccountLossPercent();
   if(dailyLoss >= DemoDailyLossLimitPercent)
   {
      Print("GoldAI manual DEMO blocked by daily loss lock: ", DoubleToString(dailyLoss, 2), "%");
      return;
   }
   if(CountOurOpenPositions() > 0)
   {
      Print("GoldAI manual DEMO blocked: a GOLD position is already open.");
      return;
   }

   MqlTick tick;
   if(!SymbolInfoTick(RequiredSymbol, tick) || tick.bid <= 0 || tick.ask < tick.bid) return;
   double point = SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
   if(point <= 0 || (tick.ask - tick.bid) / point > DemoMaxSpreadPoints)
   {
      Print("GoldAI manual DEMO blocked: invalid quote or spread above limit.");
      return;
   }
   double vmin = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_STEP);
   if(step <= 0 || lots < vmin || lots > vmax ||
      MathAbs(lots / step - MathRound(lots / step)) > 1e-7)
   {
      Print("GoldAI manual DEMO blocked: lot size violates broker volume rules.");
      return;
   }

   int digits = (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS);
   long stopsLevel = SymbolInfoInteger(RequiredSymbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minDistance = (double)stopsLevel * point;
   double entry = direction == "BUY" ? tick.ask : tick.bid;
   double stop = direction == "BUY" ? entry - slPoints * point : entry + slPoints * point;
   double target = direction == "BUY" ? entry + tpPoints * point : entry - tpPoints * point;
   stop = NormalizeDouble(stop, digits);
   target = NormalizeDouble(target, digits);
   if(MathAbs(entry - stop) < minDistance || MathAbs(target - entry) < minDistance)
   {
      Print("GoldAI manual DEMO blocked: SL/TP is inside broker minimum stop distance.");
      return;
   }

   ENUM_ORDER_TYPE orderType = direction == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double estimatedStopPnl = 0.0;
   if(!OrderCalcProfit(orderType, RequiredSymbol, lots, entry, stop, estimatedStopPnl))
   {
      Print("GoldAI manual DEMO blocked: broker risk calculation failed. error=", GetLastError());
      return;
   }
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double maxRiskMoney = equity * DemoMaxRiskPerTradePercent / 100.0;
   if(equity <= 0 || estimatedStopPnl >= 0 || (-estimatedStopPnl * 1.05) > maxRiskMoney)
   {
      Print("GoldAI manual DEMO blocked: stop risk exceeds configured maximum ",
            DoubleToString(DemoMaxRiskPerTradePercent, 2), "%.");
      return;
   }

   trade.SetExpertMagicNumber(DemoMagicNumber);
   trade.SetDeviationInPoints(DemoMaxDeviationPoints);
   trade.SetTypeFillingBySymbol(RequiredSymbol);
   bool sent = direction == "BUY" ?
      trade.Buy(lots, RequiredSymbol, 0.0, stop, target, "GoldAI MANUAL DEMO BUY") :
      trade.Sell(lots, RequiredSymbol, 0.0, stop, target, "GoldAI MANUAL DEMO SELL");
   if(sent && (trade.ResultRetcode() == TRADE_RETCODE_DONE ||
               trade.ResultRetcode() == TRADE_RETCODE_PLACED))
      Print("GoldAI MANUAL DEMO order accepted: ", direction, " lots=", DoubleToString(lots, 2),
            " SL=", DoubleToString(stop, digits), " TP=", DoubleToString(target, digits));
   else
      Print("GoldAI MANUAL DEMO order rejected. retcode=", trade.ResultRetcode(),
            " ", trade.ResultRetcodeDescription());
}

void TryDemoTrade()
{
   if(!EnableDemoExecution) return;
   if((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("GoldAI DEMO EXECUTION BLOCKED: this is not an MT5 DEMO account.");
      return;
   }
   if(_Symbol != RequiredSymbol || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !MQLInfoInteger(MQL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) return;
   datetime bar = iTime(RequiredSymbol, PERIOD_M5, 0);
   if(bar <= 0 || bar == lastDemoDecisionBar) return;
   lastDemoDecisionBar = bar;
   if(CountOurOpenPositions() > 0) return;
   double dailyLoss = TodayAccountLossPercent();
   if(dailyLoss >= DemoDailyLossLimitPercent)
   {
      Print("GoldAI daily loss lock: ", DoubleToString(dailyLoss, 2), "%");
      return;
   }
   MqlTick tick;
   if(!SymbolInfoTick(RequiredSymbol, tick) || tick.bid <= 0 || tick.ask < tick.bid) return;
   double point = SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
   if(point <= 0 || (tick.ask - tick.bid) / point > DemoMaxSpreadPoints) return;
   string payload;
   if(!FetchDemoSignal(dailyLoss, payload)) return;
   if(JsonValue(payload, "candidate_ready") != "true") return;
   if(JsonValue(payload, "symbol") != RequiredSymbol) return;
   string direction = JsonValue(payload, "decision");
   double lots = JsonNumber(payload, "recommended_lots");
   double stop = JsonNumber(payload, "stop_loss");
   double target = JsonNumber(payload, "take_profit");
   double signalRisk = JsonNumber(payload, "risk_percent");
   if(signalRisk <= 0 || signalRisk > DemoMaxRiskPerTradePercent) return;
   double vmin = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_STEP);
   if(lots <= 0 || stop <= 0 || target <= 0 || step <= 0) return;
   lots = NormalizeDouble(MathFloor((lots + 1e-10) / step) * step, 8);
   if(lots < vmin || lots > vmax) return;
   int digits = (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS);
   long stopsLevel = SymbolInfoInteger(RequiredSymbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minimumStopDistance = (double)stopsLevel * SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
   stop = NormalizeDouble(stop, digits);
   target = NormalizeDouble(target, digits);

   // Independent terminal-side risk verification using broker-calculated P/L.
   ENUM_ORDER_TYPE orderType;
   double estimatedEntry = 0.0;
   if(direction == "BUY") { orderType = ORDER_TYPE_BUY; estimatedEntry = tick.ask; }
   else if(direction == "SELL") { orderType = ORDER_TYPE_SELL; estimatedEntry = tick.bid; }
   else return;
   double estimatedStopPnl = 0.0;
   if(!OrderCalcProfit(orderType, RequiredSymbol, lots, estimatedEntry, stop, estimatedStopPnl))
   {
      Print("GoldAI DEMO blocked: broker could not calculate stop-loss risk. error=", GetLastError());
      return;
   }
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double maxRiskMoney = equity * DemoMaxRiskPerTradePercent / 100.0;
   if(equity <= 0 || estimatedStopPnl >= 0 ||
      (-estimatedStopPnl * 1.05) > maxRiskMoney)
   {
      Print("GoldAI DEMO blocked: independent risk check failed. estimated loss=",
            DoubleToString(-estimatedStopPnl, 2), " max with 5% buffer=",
            DoubleToString(maxRiskMoney, 2));
      return;
   }

   trade.SetExpertMagicNumber(DemoMagicNumber);
   trade.SetDeviationInPoints(DemoMaxDeviationPoints);
   trade.SetTypeFillingBySymbol(RequiredSymbol);
   bool sent = false;
   if(direction == "BUY" && stop < tick.bid && target > tick.ask &&
      tick.bid - stop >= minimumStopDistance && target - tick.ask >= minimumStopDistance)
      sent = trade.Buy(lots, RequiredSymbol, 0.0, stop, target, "GoldAI DEMO BUY");
   else if(direction == "SELL" && stop > tick.ask && target < tick.bid &&
      stop - tick.ask >= minimumStopDistance && tick.bid - target >= minimumStopDistance)
      sent = trade.Sell(lots, RequiredSymbol, 0.0, stop, target, "GoldAI DEMO SELL");
   else return;
   if(sent && (trade.ResultRetcode() == TRADE_RETCODE_DONE ||
               trade.ResultRetcode() == TRADE_RETCODE_PLACED))
      Print("GoldAI DEMO order accepted: ", direction, " lots=", DoubleToString(lots, 2),
            " SL=", DoubleToString(stop, digits), " TP=", DoubleToString(target, digits));
   else
      Print("GoldAI DEMO order rejected. retcode=", trade.ResultRetcode(),
            " ", trade.ResultRetcodeDescription());
}

double PositionRMultiple(ulong ticket, double entry, double current, double initialRisk, long positionType)
{
   if(initialRisk <= 0) return 0.0;
   double favorable = positionType == POSITION_TYPE_BUY ? current - entry : entry - current;
   return favorable / initialRisk;
}

string TicketGlobalKey(ulong ticket, string suffix)
{
   return "GoldAI_" + IntegerToString((long)ticket) + "_" + suffix;
}

void ManageDemoPositions()
{
   if(!EnableDemoPositionManagement || !IsDemoAccount() || _Symbol != RequiredSymbol) return;
   int atrHandle = iATR(RequiredSymbol, PERIOD_M5, 14);
   if(atrHandle == INVALID_HANDLE) return;
   double atrBuffer[];
   ArraySetAsSeries(atrBuffer, true);
   bool atrReady = CopyBuffer(atrHandle, 0, 1, 1, atrBuffer) == 1 && atrBuffer[0] > 0;
   IndicatorRelease(atrHandle);
   double atr = atrReady ? atrBuffer[0] : 0.0;
   double point = SymbolInfoDouble(RequiredSymbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != RequiredSymbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != DemoMagicNumber) continue;

      long type = PositionGetInteger(POSITION_TYPE);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double stop = PositionGetDouble(POSITION_SL);
      double target = PositionGetDouble(POSITION_TP);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double current = type == POSITION_TYPE_BUY ?
         SymbolInfoDouble(RequiredSymbol, SYMBOL_BID) : SymbolInfoDouble(RequiredSymbol, SYMBOL_ASK);
      if(current <= 0 || entry <= 0 || stop <= 0 || volume <= 0) continue;

      string riskKey = TicketGlobalKey(ticket, "RISK");
      string partialKey = TicketGlobalKey(ticket, "PARTIAL");
      double initialRisk = GlobalVariableCheck(riskKey) ? GlobalVariableGet(riskKey) : MathAbs(entry - stop);
      if(initialRisk <= point) continue;
      if(!GlobalVariableCheck(riskKey)) GlobalVariableSet(riskKey, initialRisk);
      double r = PositionRMultiple(ticket, entry, current, initialRisk, type);
      double newStop = stop;

      // Break-even: only tighten risk, never widen the original stop.
      if(r >= BreakEvenTriggerR)
      {
         double be = type == POSITION_TYPE_BUY ?
            entry + initialRisk * BreakEvenLockR : entry - initialRisk * BreakEvenLockR;
         if(type == POSITION_TYPE_BUY && be > newStop && be < current)
            newStop = be;
         if(type == POSITION_TYPE_SELL && be < newStop && be > current)
            newStop = be;
      }

      // Partial close once per ticket. Respect the broker's volume step and minimum.
      if(r >= PartialCloseTriggerR &&
         (!GlobalVariableCheck(partialKey) || GlobalVariableGet(partialKey) < 1.0))
      {
         double step = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_STEP);
         double minVolume = SymbolInfoDouble(RequiredSymbol, SYMBOL_VOLUME_MIN);
         double closeVolume = step > 0 ? MathFloor((volume * PartialClosePercent / 100.0) / step) * step : 0.0;
         closeVolume = NormalizeDouble(closeVolume, 8);
         if(closeVolume >= minVolume && volume - closeVolume >= minVolume)
         {
            if(trade.PositionClosePartial(ticket, closeVolume))
            {
               GlobalVariableSet(partialKey, 1.0);
               Print("GoldAI DEMO partial close: ticket=", ticket, " volume=", DoubleToString(closeVolume, 2));
            }
         }
         else
            GlobalVariableSet(partialKey, 1.0); // Too small to split safely; do not retry every timer tick.
      }

      // ATR trailing starts only after the trade has moved at least 2R in favor.
      if(atr > 0 && r >= AtrTrailStartR)
      {
         double trail = type == POSITION_TYPE_BUY ? current - atr * AtrTrailMultiple : current + atr * AtrTrailMultiple;
         if(type == POSITION_TYPE_BUY && trail > newStop && trail < current)
            newStop = trail;
         if(type == POSITION_TYPE_SELL && trail < newStop && trail > current)
            newStop = trail;
      }

      newStop = NormalizeDouble(newStop, digits);
      if(MathAbs(newStop - stop) >= point &&
         ((type == POSITION_TYPE_BUY && newStop > stop && newStop < current) ||
          (type == POSITION_TYPE_SELL && newStop < stop && newStop > current)))
      {
         if(trade.PositionModify(ticket, newStop, target))
            Print("GoldAI DEMO managed stop: ticket=", ticket, " SL=", DoubleToString(newStop, digits),
                  " R=", DoubleToString(r, 2));
      }
   }
}

void ExportClosedDealsToJournal()
{
   datetime now = TimeTradeServer();
   if(now <= 0) now = TimeCurrent();
   MqlDateTime day;
   TimeToStruct(now, day);
   day.hour = 0; day.min = 0; day.sec = 0;
   datetime from = StructToTime(day);
   if(!HistorySelect(from, now)) return;

   int file = FileOpen("GoldAI_TradeJournal.csv", FILE_READ|FILE_WRITE|FILE_CSV|FILE_COMMON, ',');
   if(file == INVALID_HANDLE)
   {
      Print("GoldAI journal: cannot open CSV. error=", GetLastError());
      return;
   }
   if(FileSize(file) == 0)
      FileWrite(file, "deal_ticket", "position_id", "time", "symbol", "direction", "volume",
                "price", "profit", "commission", "swap", "reason");
   FileSeek(file, 0, SEEK_END);

   for(int i = 0; i < HistoryDealsTotal(); i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetString(deal, DEAL_SYMBOL) != RequiredSymbol) continue;
      if((ulong)HistoryDealGetInteger(deal, DEAL_MAGIC) != DemoMagicNumber) continue;
      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT) continue;
      string key = "GoldAI_JOURNAL_" + IntegerToString((long)deal);
      if(GlobalVariableCheck(key)) continue;

      ENUM_DEAL_TYPE type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);
      string direction = type == DEAL_TYPE_BUY ? "BUY" : (type == DEAL_TYPE_SELL ? "SELL" : "OTHER");
      datetime dealTime = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
      FileWrite(file,
         (long)deal,
         (long)HistoryDealGetInteger(deal, DEAL_POSITION_ID),
         TimeToString(dealTime, TIME_DATE|TIME_SECONDS),
         RequiredSymbol,
         direction,
         DoubleToString(HistoryDealGetDouble(deal, DEAL_VOLUME), 2),
         DoubleToString(HistoryDealGetDouble(deal, DEAL_PRICE), (int)SymbolInfoInteger(RequiredSymbol, SYMBOL_DIGITS)),
         DoubleToString(HistoryDealGetDouble(deal, DEAL_PROFIT), 2),
         DoubleToString(HistoryDealGetDouble(deal, DEAL_COMMISSION), 2),
         DoubleToString(HistoryDealGetDouble(deal, DEAL_SWAP), 2),
         IntegerToString((int)HistoryDealGetInteger(deal, DEAL_REASON)));
      GlobalVariableSet(key, 1.0);
   }
   FileFlush(file);
   FileClose(file);
}

void OnTick()
{
   if(_Symbol != RequiredSymbol) return;
   ulong nowMs = GetTickCount64();
   if(lastQuoteSendMs != 0 &&
      nowMs - lastQuoteSendMs < (ulong)MinQuoteIntervalMilliseconds)
      return;
   lastQuoteSendMs = nowMs;
   SendQuote();
}

void OnTimer()
{
   if(_Symbol != RequiredSymbol) return;
   datetime now = TimeCurrent();
   if(lastCandleSend == 0 || now - lastCandleSend >= MathMax(30, CandleRefreshSeconds))
   {
      SendCandleBatch();
      lastCandleSend = now;
   }
   ManageDemoPositions();
   ExportClosedDealsToJournal();
   TryManualDemoTrade();
   TryDemoTrade();
}
