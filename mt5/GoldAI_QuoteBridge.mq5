// GoldAI Trader - MT5 bridge with optional guarded DEMO-only execution
#property strict
#include <Trade/Trade.mqh>
CTrade trade;
#property version   "2.1"
#property description "Streams XM GOLD Bid/Ask and closed M5/M15/H1/H4 candles to GoldAI Trader."

input string BackendUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/quote";
input string CandleDataUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/candles";
input string DemoSignalUrl = "https://goldai-trader-api.onrender.com/api/v1/mt5/demo/signal";
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

datetime lastCandleSend = 0;
ulong lastQuoteSendMs = 0;
datetime lastDemoDecisionBar = 0;

int OnInit()
{
   if(StringLen(BridgeToken) < 24 ||
      StringFind(BackendUrl, "https://") != 0 ||
      StringFind(CandleDataUrl, "https://") != 0 ||
      StringFind(DemoSignalUrl, "https://") != 0 ||
      DemoDailyLossLimitPercent <= 0 || DemoDailyLossLimitPercent > 10.0 ||
      DemoMaxRiskPerTradePercent <= 0 || DemoMaxRiskPerTradePercent > 0.5 ||
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
   TryDemoTrade();
}
