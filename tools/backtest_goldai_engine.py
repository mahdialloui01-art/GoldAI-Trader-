from __future__ import annotations
import asyncio, bisect, csv, json, math, os, inspect
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from app import main as e

ROOT=Path(__file__).resolve().parents[1]
BID=ROOT/"xauusd-m1-bid-2026-05-01-2026-08-01.csv"
ASK=ROOT/"xauusd-m1-ask-2026-05-01-2026-08-01.csv"
OUT=ROOT/"backtest-results"
POINT=.01
SLIP=float(os.getenv("BACKTEST_SLIPPAGE_POINTS","2"))*POINT
NOW=[datetime(2026,1,1,tzinfo=timezone.utc)]
QUOTE=[{}]
REAL_DT=e.datetime

# Research-only ablation profile. Production backend thresholds and code are untouched.
# Separate the raw entry score from directional confluence, then test a cautious
# lower score floor (75/100) with a distinct confluence floor (70/100).
_ORIGINAL_RECONCILE = e._reconcile_entry_engine_quality
e._DEMO_MIN_CONFLUENCE_SCORE = 70.0
e._DEMO_STRUCTURAL_MIN_CONFLUENCE_SCORE = 70.0

def research_reconcile(analysis):
    dossier = analysis.get("technical_dossier") or {}
    entry = analysis.get("entry_engine") or {}
    try:
        confluence = float(dossier.get("confluence_score"))
        raw = float(entry.get("entry_quality_score_0_to_100"))
    except (TypeError, ValueError):
        return _ORIGINAL_RECONCILE(analysis)
    strategy = str(entry.get("selected_strategy") or "")
    structural = strategy in e._DEMO_STRUCTURAL_ENTRY_STRATEGIES
    threshold = 82.0 if structural else 75.0
    entry["raw_entry_quality_score_0_to_100"] = round(raw, 1)
    entry["cross_layer_confluence_score"] = round(confluence, 1)
    entry["effective_entry_quality_score_0_to_100"] = round(raw, 1)
    entry["entry_quality_score_0_to_100"] = round(raw, 1)
    entry["score_threshold"] = threshold
    entry["score_reconciliation"] = "RESEARCH_ABLATION_RAW_ENTRY_SCORE_WITH_SEPARATE_CONFLUENCE_FLOOR_70"
    if raw < threshold:
        entry["direction"] = e.Direction.WAIT
        entry["setup"] = None
        entry.setdefault("blockers", []).append("RESEARCH_ENTRY_SCORE_BELOW_ABLATION_THRESHOLD")
    analysis["entry_engine"] = entry
    return analysis


e._reconcile_entry_engine_quality = research_reconcile

# Research-only directional-bias ablation. It does NOT alter backend/app/main.py.
# H4 remains dominant when both frames agree or when its ADX is clearly stronger;
# H1 may lead when H4 is neutral or H1 is clearly stronger. A confirmed M5
# pullback/EMA reclaim can qualify during an M15 counter-swing, but only when
# the last closed M5 candle confirms the selected direction.
_original_entry_source = inspect.getsource(e.analyze_entry_plan)
_entry_source = _original_entry_source
_replacements = [
    ("    bias = h4_bias\n",
     "    if h4_bias == h1_bias:\n        bias = h4_bias\n    elif h4_bias == e.Direction.WAIT:\n        bias = h1_bias\n    elif h1_bias == e.Direction.WAIT:\n        bias = h4_bias\n    elif h4_adx >= h1_adx + 5.0:\n        bias = h4_bias\n    elif h1_adx >= h4_adx + 5.0:\n        bias = h1_bias\n    else:\n        bias = e.Direction.WAIT\n"),
    ("    higher_aligned = bias in (Direction.BUY, Direction.SELL) and h1_bias == bias\n",
     "    higher_aligned = bias in (Direction.BUY, Direction.SELL) and (h4_bias == bias or h1_bias == bias)\n    both_higher_aligned = h4_bias == bias and h1_bias == bias\n"),
    ("    if higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 20\n        reasons.append(\"اتجاه H4 وH1 متوافق؛ الإطاران الأعلى يحددان نفس الأفضلية.\")\n    else:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 0\n        blockers.append(\"اتجاه H4 وH1 غير متوافق أو غير واضح.\")\n",
     "    if both_higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 20\n        reasons.append(\"اتجاه H4 وH1 متوافق؛ الإطاران الأعلى يحددان نفس الأفضلية.\")\n    elif higher_aligned and (h4_bias == Direction.WAIT or h1_bias == Direction.WAIT):\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 12\n        reasons.append(\"إطار أعلى واحد يحدد الاتجاه والآخر محايد؛ تم اعتماد تأكيد H1/H4 المتاح.\")\n    elif higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 8\n        reasons.append(\"اتجاه H4/H1 متعارض؛ تم ترجيح الإطار ذي ADX الأقوى بفارق واضح.\")\n    else:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 0\n        blockers.append(\"اتجاه H4 وH1 متعارض دون أفضلية قوة واضحة.\")\n"),
    ("    if micro_matches:\n        reasons.append(\"شكل الشمعة وموقع الإغلاق يدعمان اتجاه الصفقة.\")\n",
     "    if micro_matches:\n        reasons.append(\"شكل الشمعة وموقع الإغلاق يدعمان اتجاه الصفقة.\")\n    m15_retrace_ok = bool(\n        m15_opposes and selected_strategy in (\"PULLBACK\", \"EMA_RECLAIM\")\n        and trigger_direction == bias and micro_matches\n    )\n    if m15_retrace_ok:\n        blockers = [item for item in blockers if item != \"بنية M15 تعاكس اتجاه H4/H1.\"]\n        components[\"M15_MARKET_STRUCTURE\"] = 5\n        reasons.append(\"تعارض M15 مصنف كتراجع مؤقت؛ تأكيد شمعة M5 المكتملة يدعم استمرار الاتجاه.\")\n"),
    ("    minimum_score = 82 if selected_strategy in (\"BREAKOUT\", \"LIQUIDITY_SWEEP\") else 85\n",
     "    minimum_score = (82 if selected_strategy in (\"BREAKOUT\", \"LIQUIDITY_SWEEP\") else 85)\n    if higher_aligned and not both_higher_aligned:\n        minimum_score = min(minimum_score, 78)\n    if m15_retrace_ok:\n        minimum_score = min(minimum_score, 75)\n"),
    ("    elif m15_opposes:\n        final_direction = Direction.WAIT\n",
     "    elif m15_opposes and not m15_retrace_ok:\n        final_direction = Direction.WAIT\n"),
]
for old,new in _replacements:
    if old not in _entry_source:
        raise RuntimeError("Research patch anchor missing: "+old[:90])
    _entry_source=_entry_source.replace(old,new,1)
exec(compile(_entry_source, inspect.getsourcefile(e.analyze_entry_plan) or "<research-entry-ablation>", "exec"), e.__dict__)
class SimDateTime(REAL_DT):
    @classmethod
    def now(cls,tz=None):
        v=NOW[0]
        return v.replace(tzinfo=None) if tz is None else v.astimezone(tz)

def load(path):
    if not path.exists() or path.stat().st_size<1000: raise RuntimeError(f"Missing/unresolved data: {path}")
    out={}
    with path.open(encoding="utf-8-sig",newline="") as f:
        r=csv.DictReader(f)
        if not {"timestamp","open","high","low","close","volume"}.issubset(r.fieldnames or []): raise RuntimeError(f"Bad schema: {path}")
        for x in r:
            t=int(float(x["timestamp"])); t=t//1000 if t>10**12 else t
            try:
                v={k:float(x[k]) for k in ("open","high","low","close")}
                vol=max(0,int(float(x.get("volume") or 0)))
                if all(math.isfinite(z) and z>0 for z in v.values()) and v["high"]>=max(v["open"],v["close"],v["low"]) and v["low"]<=min(v["open"],v["close"],v["high"]):
                    out[t]={**v,"volume":vol}
            except (ValueError,TypeError): pass
    return out

def aggregate(paired,sec):
    groups=defaultdict(list)
    for x in paired: groups[x["ts"]//sec*sec].append(x)
    out=[]
    for t,rows in sorted(groups.items()):
        rows.sort(key=lambda x:x["ts"])
        if len(rows)!=sec//60 or any(rows[i]["ts"]-rows[i-1]["ts"]!=60 for i in range(1,len(rows))): continue
        b=[x["bid"] for x in rows]
        out.append({"ts":t,"open":b[0]["open"],"high":max(x["high"] for x in b),"low":min(x["low"] for x in b),"close":b[-1]["close"],"volume":sum(x["volume"] for x in b)})
    return out

def make_candles(rows,ends,cutoff,limit=120):
    n=bisect.bisect_right(ends,cutoff)
    return [e.Candle(time=datetime.fromtimestamp(x["ts"],timezone.utc),open=x["open"],high=x["high"],low=x["low"],close=x["close"],tick_volume=x["volume"]) for x in rows[max(0,n-limit):n]]

async def macro():
    return {"bias":"MIXED_OR_NEUTRAL","confidence_engine":{"directional_bias":"MIXED_OR_NEUTRAL"},"execution_enabled":False}
async def news():
    return {"status":"AVAILABLE","decision":"CLEAR","trading_allowed":True,"active_events":[],"upcoming_events":[],"execution_enabled":False}

async def run():
    bid,ask=load(BID),load(ASK)
    paired=[]
    for t in sorted(set(bid)&set(ask)):
        b,a=bid[t],ask[t]
        if a["open"]>=b["open"] and a["close"]>=b["close"]:
            paired.append({"ts":t,"bid":b,"ask":a})
    if len(paired)<10000: raise RuntimeError(f"Only {len(paired)} aligned BID/ASK rows")
    m1={x["ts"]:x for x in paired}; ts=[x["ts"] for x in paired]
    secs={"M5":300,"M15":900,"H1":3600,"H4":14400}
    frames={k:aggregate(paired,v) for k,v in secs.items()}
    ends={k:[x["ts"]+secs[k] for x in frames[k]] for k in secs}
    if min(map(len,frames.values()))<60: raise RuntimeError("Not enough complete MTF bars")
    e.datetime=SimDateTime
    e._fresh_mt5_quote=lambda:QUOTE[0]
    e.gold_macro_analysis=macro
    e.economic_news_filter=news
    e.save_opportunity=lambda *a,**k:None
    trades=[]; blocked=defaultdict(int); blocked_details=defaultdict(int); engine_errors=defaultdict(int); signals=0; stale_skipped=0; pos=None
    diag = {
        "expert_decisions": defaultdict(int), "entry_directions": defaultdict(int),
        "selected_strategies": defaultdict(int), "candidate_strategies": defaultdict(int),
        "higher_timeframe_bias_pairs": defaultdict(int), "m15_structures": defaultdict(int),
        "spread_ratio_buckets": defaultdict(int), "confluence_buckets": defaultdict(int),
        "entry_score_buckets": defaultdict(int), "score_components": defaultdict(int),
        "samples": [],
    }
    def bucket(value, step=10):
        try: return f"{int(float(value) // step) * step}-{int(float(value) // step) * step + step - 1}"
        except (TypeError, ValueError): return "missing"
    def sval(value):
        return str(value.value if hasattr(value, "value") else value)
    slip_points=float(os.getenv("BACKTEST_SLIPPAGE_POINTS","2"))

    def close(exit_price,t,reason):
        nonlocal pos
        d=pos["direction"]
        pnl=(exit_price-pos["entry"]) if d=="BUY" else (pos["entry"]-exit_price)
        trades.append({"direction":d,"strategy":pos["strategy"],"entry_time":datetime.fromtimestamp(pos["entry_ts"],timezone.utc).isoformat(),"exit_time":datetime.fromtimestamp(t,timezone.utc).isoformat(),"entry_price":round(pos["entry"],2),"exit_price":round(exit_price,2),"stop":round(pos["stop"],2),"target_2R":round(pos["target"],2),"net_R":round(pnl/pos["risk"],4),"entry_score":pos["score"],"confluence":pos["confluence"],"exit_reason":reason})
        pos=None

    for m5 in frames["M5"]:
        end=m5["ts"]+300
        # Resolve the prior open position using each intervening executable BID/ASK M1 bar.
        lo=bisect.bisect_left(ts,end-300); hi=bisect.bisect_right(ts,end-60)
        for x in paired[lo:hi]:
            t=x["ts"]
            if pos is None or t<=pos["entry_ts"]: continue
            b,a=x["bid"],x["ask"]
            if pos["direction"]=="BUY":
                if b["open"]<=pos["stop"]: close(b["open"]-SLIP,t,"STOP_GAP")
                elif b["low"]<=pos["stop"]: close(pos["stop"]-SLIP,t,"STOP")
                elif b["open"]>=pos["target"]: close(b["open"]-SLIP,t,"TARGET_GAP")
                elif b["high"]>=pos["target"]: close(pos["target"]-SLIP,t,"TARGET")
            else:
                if a["open"]>=pos["stop"]: close(a["open"]+SLIP,t,"STOP_GAP")
                elif a["high"]>=pos["stop"]: close(pos["stop"]+SLIP,t,"STOP")
                elif a["open"]<=pos["target"]: close(a["open"]+SLIP,t,"TARGET_GAP")
                elif a["low"]<=pos["target"]: close(pos["target"]+SLIP,t,"TARGET")
        if pos is not None: continue
        nxt=m1.get(end)
        qrow=m1.get(end-60)
        if nxt is None or qrow is None: continue
        NOW[0]=datetime.fromtimestamp(end,timezone.utc)
        b,a=qrow["bid"],qrow["ask"]
        spread=max(0,(a["close"]-b["close"])/POINT)
        QUOTE[0]={"symbol":"GOLD","bid":b["close"],"ask":a["close"],"last":(a["close"]+b["close"])/2,"mid":(a["close"]+b["close"])/2,"spread_points":spread,"digits":2,"point":POINT,"source":"Historical BID/ASK","source_updated_at":NOW[0],"age_seconds":0,"quote_type":"broker_bid_ask","tradable_quote":True}
        series={k:make_candles(frames[k],ends[k],end) for k in secs}
        if min(map(len,series.values()))<60: continue
        # Match production freshness limits, but do not count genuine historical
        # market closures/data gaps as engine failures or trading opportunities.
        stale_frames = []
        for frame, candles in series.items():
            age = (NOW[0] - candles[-1].time).total_seconds()
            if age > secs[frame] * 2 + 60:
                stale_frames.append(frame)
        if stale_frames:
            stale_skipped += 1
            blocked["HISTORICAL_GAP_SKIPPED_" + "_".join(stale_frames)] += 1
            continue
        e._latest_mt5_candles={"symbol":"GOLD","timeframes":series,"received_at":NOW[0],"source":"Historical paired BID/ASK","data_quality":{"H4":{"h4_session_reopen_gap":True}}}
        signals+=1
        try:
            result=await e.live_expert_technical_analysis()
            ok,reason,details=e._demo_candidate_quality_gate(result)
            dossier = result.get("technical_dossier") or {}
            frames_report = dossier.get("timeframes") or {}
            entry = result.get("entry_engine") or {}
            scan = entry.get("entry_setup_scan") or {}
            diag["expert_decisions"][sval(result.get("decision", "WAIT"))] += 1
            diag["entry_directions"][sval(entry.get("direction", "WAIT"))] += 1
            diag["selected_strategies"][str(entry.get("selected_strategy") or "NONE")] += 1
            for candidate in (scan.get("candidates") or []):
                diag["candidate_strategies"][str(candidate.get("strategy") or "UNKNOWN")] += 1
            htf = entry.get("higher_timeframe") or {}
            pair = f'H4:{sval((htf.get("H4") or {}).get("bias","?"))}|H1:{sval((htf.get("H1") or {}).get("bias","?"))}'
            diag["higher_timeframe_bias_pairs"][pair] += 1
            m15 = (entry.get("market_structure") or {}).get("M15") or {}
            diag["m15_structures"][str(m15.get("market_structure") or "UNKNOWN")] += 1
            diag["spread_ratio_buckets"][bucket(entry.get("spread_atr_ratio_M5"), 0.05)] += 1
            diag["confluence_buckets"][bucket(dossier.get("confluence_score"), 10)] += 1
            diag["entry_score_buckets"][bucket(entry.get("entry_quality_score_0_to_100"), 10)] += 1
            for name, value in (entry.get("score_components") or {}).items():
                diag["score_components"][f"{name}={value}"] += 1
            if len(diag["samples"]) < 15 and (entry.get("selected_strategy") or entry.get("direction") in ("BUY","SELL")):
                diag["samples"].append({
                    "time": NOW[0].isoformat(), "expert_decision": sval(result.get("decision","WAIT")),
                    "expert_confluence": dossier.get("confluence_score"),
                    "entry_direction": sval(entry.get("direction","WAIT")),
                    "entry_score": entry.get("entry_quality_score_0_to_100"),
                    "entry_threshold": entry.get("score_threshold"),
                    "selected_strategy": entry.get("selected_strategy"),
                    "candidate_strategies": [x.get("strategy") for x in (scan.get("candidates") or [])],
                    "higher_timeframe": htf,
                    "m15_structure": m15.get("market_structure"),
                    "spread_atr_ratio_M5": entry.get("spread_atr_ratio_M5"),
                    "quote_distance_from_signal_close_atr": entry.get("quote_distance_from_signal_close_atr"),
                    "entry_blockers": entry.get("blockers") or [],
                })
        except Exception as ex:
            blocked["ENGINE_ERROR_"+type(ex).__name__]+=1
            detail = getattr(ex, "detail", None) or str(ex) or repr(ex)
            age_diag = {}
            try:
                cached = e._latest_mt5_candles.get("timeframes", {})
                sim_now = SimDateTime.now(timezone.utc)
                for frame, candles in cached.items():
                    last_time = candles[-1].time.astimezone(timezone.utc)
                    age_diag[frame] = {
                        "last_candle_time": last_time.isoformat(),
                        "sim_now": sim_now.isoformat(),
                        "age_seconds": round((sim_now - last_time).total_seconds(), 2),
                        "limit_seconds": secs[frame] * 2 + 60,
                    }
            except Exception as diag_ex:
                age_diag = {"diagnostic_error": repr(diag_ex)}
            engine_errors[f"{type(ex).__name__}: {detail} | ages={json.dumps(age_diag, ensure_ascii=False, sort_keys=True)}"] += 1
            continue
        if not ok:
            blocked[reason]+=1
            for item in (details.get("entry_engine_blockers") or []):
                blocked_details["ENTRY: "+str(item)] += 1
            for item in (details.get("strategy_blockers") or []):
                blocked_details["STRATEGY: "+str(item)] += 1
            continue
        en=result.get("entry_engine") or {}; setup=en.get("setup") or {}
        d=str(en.get("direction","WAIT"))
        if d not in ("BUY","SELL") or not setup:
            blocked["NO_CONFIRMED_SETUP"]+=1; continue
        b,a=nxt["bid"],nxt["ask"]
        fill=a["open"] if d=="BUY" else b["open"]
        stop=float(setup["stop_loss"]); target=float(setup["take_profit"]["TP2_2R"])
        risk=abs(fill-stop)
        planned=float(setup.get("risk_distance_price") or risk)
        if risk<=0 or risk>planned*1.25 or (d=="BUY" and (stop>=fill or target<=fill)) or (d=="SELL" and (stop<=fill or target>=fill)):
            blocked["INVALID_OR_GAPPED_FILL"]+=1; continue
        pos={"direction":d,"strategy":en.get("selected_strategy"),"entry_ts":end,"entry":fill+(SLIP if d=="BUY" else -SLIP),"stop":stop,"target":target,"risk":risk,"score":en.get("effective_entry_quality_score_0_to_100"),"confluence":(result.get("technical_dossier") or {}).get("confluence_score")}
    if pos:
        x=paired[-1]; px=x["bid"]["close"] if pos["direction"]=="BUY" else x["ask"]["close"]
        close(px-(SLIP if pos["direction"]=="BUY" else -SLIP),x["ts"]+60,"END_OF_DATA")
    wins=[x for x in trades if x["net_R"]>0]; losses=[x for x in trades if x["net_R"]<=0]
    gp=sum(x["net_R"] for x in wins); gl=abs(sum(x["net_R"] for x in losses))
    equity=peak=dd=0
    for x in trades:
        equity+=x["net_R"]; peak=max(peak,equity); dd=max(dd,peak-equity)
    monthly=defaultdict(float)
    for x in trades: monthly[x["entry_time"][:7]]+=x["net_R"]
    report={"test":"Production GoldAI live expert orchestration + dedicated entry engine + production DEMO gate","mode":"OFFLINE_RESEARCH_ONLY","period_utc":{"start":datetime.fromtimestamp(paired[0]["ts"],timezone.utc).isoformat(),"end":datetime.fromtimestamp(paired[-1]["ts"]+60,timezone.utc).isoformat()},"data":{"bid_rows":len(bid),"ask_rows":len(ask),"paired_m1_rows":len(paired),"complete_bars":{k:len(v) for k,v in frames.items()}},"signals_evaluated":signals,"historical_gap_windows_skipped":stale_skipped,"trades":len(trades),"wins":len(wins),"losses_or_breakeven":len(losses),"win_rate_percent":round(100*len(wins)/len(trades),2) if trades else None,"net_R":round(sum(x["net_R"] for x in trades),3),"expectancy_R":round(sum(x["net_R"] for x in trades)/len(trades),4) if trades else None,"profit_factor":round(gp/gl,3) if gl else ("infinite" if gp else None),"max_drawdown_R":round(dd,3),"monthly_net_R":{k:round(v,3) for k,v in sorted(monthly.items())},"blocked_reasons":dict(blocked),"blocked_detail_counts":dict(blocked_details),"engine_error_details":dict(engine_errors),"diagnostics":{k:(dict(v) if isinstance(v,defaultdict) else v) for k,v in diag.items()},"assumptions":{"point":POINT,"slippage_points_each_side":slip_points,"buy_entry":"ASK; exit BID","sell_entry":"BID; exit ASK","same_bar_SL_TP":"SL first","commission_swap":"not included; not provided","macro_news":"neutral/advisory stubs; point-in-time historic snapshots unavailable","standard_entry_threshold":85,"production_structural_BOS_sweep_threshold":82,"only_closed_bars":True},"limitations":["XAUUSD historical series may differ from XM GOLD server prices/contract specifications.","Macro and news use neutral advisory context because historical point-in-time snapshots were not supplied.","Commission, swaps, latency, requotes, stop-level rules and partial fills are not included.","Quality scores are not probabilities; historical win rate is not a future guarantee."]}
    OUT.mkdir(exist_ok=True)
    (OUT/"summary.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    with (OUT/"trades.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(trades[0]) if trades else ["no_trades"]); w.writeheader(); w.writerows(trades)
    (OUT/"summary.md").write_text("# GoldAI Trader historical replay\n\n"+"\n".join(f"- {k}: {v}" for k,v in report.items() if k in ("period_utc","signals_evaluated","trades","wins","losses_or_breakeven","win_rate_percent","net_R","expectancy_R","profit_factor","max_drawdown_R"))+"\n\nSee summary.json and trades.csv. Offline research only; macro/news are neutralized and commissions/swaps are excluded.\n",encoding="utf-8")
    print(json.dumps(report,indent=2,ensure_ascii=False))

if __name__=="__main__": asyncio.run(run())
