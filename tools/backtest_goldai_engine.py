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
# Separate raw entry score from directional confluence. This second research
# sweep tests 65/100 standard and 72/100 structural; all non-score safety blockers remain.
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
    threshold = 72.0 if structural else 65.0
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
     "    if h4_bias == h1_bias:\n        bias = h4_bias\n    elif h4_bias == Direction.WAIT:\n        bias = h1_bias\n    elif h1_bias == Direction.WAIT:\n        bias = h4_bias\n    elif h4_adx >= h1_adx + 5.0:\n        bias = h4_bias\n    elif h1_adx >= h4_adx + 5.0:\n        bias = h1_bias\n    else:\n        bias = Direction.WAIT\n"),
    ("    higher_aligned = bias in (Direction.BUY, Direction.SELL) and h1_bias == bias\n",
     "    higher_aligned = bias in (Direction.BUY, Direction.SELL) and (h4_bias == bias or h1_bias == bias)\n    both_higher_aligned = h4_bias == bias and h1_bias == bias\n"),
    ("    if higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 20\n        reasons.append(\"اتجاه H4 وH1 متوافق؛ الإطاران الأعلى يحددان نفس الأفضلية.\")\n    else:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 0\n        blockers.append(\"اتجاه H4 وH1 غير متوافق أو غير واضح.\")\n",
     "    if both_higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 20\n        reasons.append(\"اتجاه H4 وH1 متوافق؛ الإطاران الأعلى يحددان نفس الأفضلية.\")\n    elif higher_aligned and (h4_bias == Direction.WAIT or h1_bias == Direction.WAIT):\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 12\n        reasons.append(\"إطار أعلى واحد يحدد الاتجاه والآخر محايد؛ تم اعتماد تأكيد H1/H4 المتاح.\")\n    elif higher_aligned:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 8\n        reasons.append(\"اتجاه H4/H1 متعارض؛ تم ترجيح الإطار ذي ADX الأقوى بفارق واضح.\")\n    else:\n        components[\"H4_H1_DIRECTIONAL_ALIGNMENT\"] = 0\n        blockers.append(\"اتجاه H4 وH1 متعارض دون أفضلية قوة واضحة.\")\n"),
    ("    if micro_matches:\n        reasons.append(\"شكل الشمعة وموقع الإغلاق يدعمان اتجاه الصفقة.\")\n",
     "    if micro_matches:\n        reasons.append(\"شكل الشمعة وموقع الإغلاق يدعمان اتجاه الصفقة.\")\n    m15_retrace_ok = bool(\n        m15_opposes and selected_strategy in (\"PULLBACK\", \"EMA_RECLAIM\")\n        and trigger_direction == bias and micro_matches\n    )\n    if m15_retrace_ok:\n        blockers = [item for item in blockers if item != \"بنية M15 تعاكس اتجاه H4/H1.\"]\n        components[\"M15_MARKET_STRUCTURE\"] = 5\n        reasons.append(\"تعارض M15 مصنف كتراجع مؤقت؛ تأكيد شمعة M5 المكتملة يدعم استمرار الاتجاه.\")\n"),
    ("    minimum_score = 82 if selected_strategy in (\"BREAKOUT\", \"LIQUIDITY_SWEEP\") else 85\n",
     "    minimum_score = 72 if selected_strategy in (\"BREAKOUT\", \"LIQUIDITY_SWEEP\") else 65\n"),
    ("    elif m15_opposes:\n        final_direction = Direction.WAIT\n",
     "    elif m15_opposes and not m15_retrace_ok:\n        final_direction = Direction.WAIT\n"),
]
for old,new in _replacements:
    if old not in _entry_source:
        raise RuntimeError("Research patch anchor missing: "+old[:90])
    _entry_source=_entry_source.replace(old,new,1)
_entry_source = _entry_source.replace("elif spread_atr_ratio <= 0.15:", "elif spread_atr_ratio <= 0.20:")
_entry_source = _entry_source.replace("if quote_distance_atr > 0.50:", "if quote_distance_atr > 0.75:")
_entry_source = _entry_source.replace("if m5_extension_atr > 1.50:", "if m5_extension_atr > 1.80:")
_entry_source = _entry_source.replace("spread_atr_ratio > 0.15 or volatility_ratio > 1.8 or quote_distance_atr > 0.50 or m5_extension_atr > 1.50", "spread_atr_ratio > 0.20 or volatility_ratio > 1.8 or quote_distance_atr > 0.75 or m5_extension_atr > 1.80")
exec(compile(_entry_source, inspect.getsourcefile(e.analyze_entry_plan) or "<research-entry-ablation>", "exec"), e.__dict__)
# Independent research-only M5 trigger detector. Closed bars only; production untouched.
def research_m5_setup_classifier(direction, frames, reports, spread_atr_ratio):
    if direction not in ("BUY", "SELL"):
        return {"selected": None, "candidates": [], "direction": direction, "blockers": ["NO_HIGHER_TIMEFRAME_DIRECTION"]}
    m5, report = frames["M5"], reports.get("M5", {})
    if len(m5) < 30:
        return {"selected": None, "candidates": [], "direction": direction, "blockers": ["INSUFFICIENT_M5_HISTORY"]}
    atr, ema20 = float(report.get("atr14") or 0.0), float(report.get("ema20") or 0.0)
    if atr <= 0 or ema20 <= 0:
        return {"selected": None, "candidates": [], "direction": direction, "blockers": ["INVALID_ATR_OR_EMA"]}
    last, prev = m5[-1], m5[-2]
    close, opn, high, low = float(last.close), float(last.open), float(last.high), float(last.low)
    prev_high, prev_low = float(prev.high), float(prev.low)
    candle_range = max(high-low, 1e-12)
    body_atr, close_pos = abs(close-opn)/atr, (close-low)/candle_range
    extension, bullish = abs(close-ema20)/atr, direction == "BUY"
    directional = close > opn if bullish else close < opn
    near_ema = extension <= 1.80
    micro_break = close > prev_high if bullish else close < prev_low
    close_strong = close_pos >= 0.68 if bullish else close_pos <= 0.32
    reclaim = (float(prev.close) <= ema20 < close) if bullish else (float(prev.close) >= ema20 > close)
    recent = m5[-4:-1]
    if bullish:
        touched = min(float(c.low) for c in recent) <= ema20 + 0.18*atr
        held = close > ema20 and close > float(recent[-1].close)
    else:
        touched = max(float(c.high) for c in recent) >= ema20 - 0.18*atr
        held = close < ema20 and close < float(recent[-1].close)
    candidates=[]
    if reclaim and directional and close_strong and body_atr >= 0.20 and near_ema:
        candidates.append({"strategy":"EMA_RECLAIM","score":5,"max_score":5,"evidence":["إغلاق M5 استعاد EMA20 بعد تصحيح","شمعة مغلقة في اتجاه H1/H4"]})
    if micro_break and directional and close_strong and body_atr >= 0.25 and near_ema:
        candidates.append({"strategy":"MOMENTUM_CONTINUATION","score":5,"max_score":5,"evidence":["إغلاق M5 تجاوز قمة/قاع الشمعة السابقة","جسم الشمعة وموقع الإغلاق يؤكدان الاتجاه"]})
    if touched and held and directional and close_strong and near_ema:
        candidates.append({"strategy":"PULLBACK","score":5,"max_score":5,"evidence":["إعادة اختبار منطقة EMA20 خلال آخر ثلاث شمعات","رفض سعري وإغلاق مؤيد للاتجاه"]})

    # Isolated replay of the production VWAP_RECLAIM rule using only closed M5 bars.
    # Tick volume is broker-provided activity, not centralized XAUUSD volume.
    vwap_window = m5[-20:]
    vwap_volume = sum(int(getattr(item, "tick_volume", 0) or 0) for item in vwap_window)
    previous_window = m5[-21:-1]
    previous_volume = sum(int(getattr(item, "tick_volume", 0) or 0) for item in previous_window)
    if len(vwap_window) == 20 and len(previous_window) == 20 and vwap_volume > 0 and previous_volume > 0:
        current_vwap = sum(((float(item.high) + float(item.low) + float(item.close)) / 3.0) * int(getattr(item, "tick_volume", 0) or 0) for item in vwap_window) / vwap_volume
        previous_vwap = sum(((float(item.high) + float(item.low) + float(item.close)) / 3.0) * int(getattr(item, "tick_volume", 0) or 0) for item in previous_window) / previous_volume
        current_typical = (high + low + close) / 3.0
        previous_typical = (float(prev.high) + float(prev.low) + float(prev.close)) / 3.0
        confirmed = directional and (close_pos >= 0.65 if bullish else close_pos <= 0.35)
        reclaimed = (previous_typical <= previous_vwap and current_typical > current_vwap) if bullish else (previous_typical >= previous_vwap and current_typical < current_vwap)
        if reclaimed and confirmed and extension <= 1.0:
            candidates.append({"strategy":"VWAP_RECLAIM","score":5,"max_score":5,"evidence":["Closed M5 reclaim/loss of 20-bar broker tick-volume VWAP","Directional candle confirmation","VWAP volume is broker tick-volume proxy"]})

    priority={"VWAP_RECLAIM":4,"EMA_RECLAIM":3,"MOMENTUM_CONTINUATION":2,"PULLBACK":1}
    candidates.sort(key=lambda x:priority.get(x["strategy"],0),reverse=True)
    selected=candidates[0] if candidates else None
    blockers=[]
    if spread_atr_ratio > 0.20: blockers.append("SPREAD_TOO_HIGH")
    if report.get("market_regime") == "HIGH_VOLATILITY": blockers.append("EXTREME_M5_VOLATILITY")
    if extension > 1.80: blockers.append("M5_OVEREXTENDED")
    if blockers: selected=None
    return {"selected":selected,"candidates":candidates,"direction":direction,"spread_to_m5_atr_ratio":round(spread_atr_ratio,4),"m5_extension_from_ema20_atr":round(extension,3),"blockers":blockers,"score_note":"Independent research trigger rules; not calibrated win probability."}

# Use the current production strategy classifier for this replay.
# The earlier narrow experimental override omitted structural strategies and
# made the backtest unsuitable for comparing the complete strategy portfolio.
# Keep research_m5_setup_classifier above as a separately inspectable prototype,
# but do not let it replace production setup detection.

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
                # Source CSV volume is fractional; preserve relative weights instead of truncating every row to zero.
                vol=max(0,int(round(float(x.get("volume") or 0)*1_000_000)))
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

def research_entry_candidate_gate(analysis):
    """Replay only candidates that satisfy the production DEMO quality gate.
    Keep this implementation aligned with _demo_candidate_quality_gate in app.main.
    This function is never imported by the production API.
    """
    dossier = analysis.get("technical_dossier") or {}
    entry = analysis.get("entry_engine") or {}
    if not isinstance(dossier, dict) or not isinstance(entry, dict):
        return False, "MISSING_DOSSIER_OR_ENTRY_ENGINE", {}
    # Critical alignment fix: the dedicated entry engine owns its own scan.
    # The dossier strategy_scan belongs to the older expert-direction engine
    # and may be empty whenever that separate engine says WAIT.
    scan = entry.get("entry_setup_scan") or {}
    if not isinstance(scan, dict):
        return False, "MISSING_ENTRY_ENGINE_SCAN", {}
    if scan.get("blockers"):
        return False, "ENTRY_SETUP_SCAN_BLOCKED", {"strategy_blockers": scan.get("blockers")}
    blockers = entry.get("blockers") or []
    if blockers:
        return False, "ENTRY_ENGINE_BLOCKED", {"entry_engine_blockers": blockers}
    direction_value = entry.get("direction", "WAIT")
    direction = str(getattr(direction_value, "value", direction_value)).upper()
    setup = entry.get("setup")
    if direction not in ("BUY", "SELL") or not isinstance(setup, dict):
        return False, "NO_EXECUTABLE_ENTRY_SETUP", {}
    expert_direction = str(analysis.get("decision", "WAIT")).upper()
    # This is a strategy-lab replay of the dedicated entry engine, not a
    # production authorization replay. Keep expert-direction disagreement as
    # a diagnostic; it must not suppress measurement of the independent entry engine.
    strategy = str(entry.get("selected_strategy", setup.get("strategy", ""))).upper()
    research_allowed = {
        "BREAKOUT", "COMPRESSION_BREAKOUT", "EMA_RECLAIM",
        "MOMENTUM_CONTINUATION", "LIQUIDITY_SWEEP", "PULLBACK",
    }
    if strategy not in research_allowed:
        return False, "STRATEGY_NOT_IN_RESEARCH_PORTFOLIO", {"selected_strategy": strategy}
    selected = scan.get("selected") or {}
    try:
        setup_score = float(selected.get("score", 0))
        setup_max = float(selected.get("max_score", 0))
        entry_score = float(entry.get("effective_entry_quality_score_0_to_100",
                                      entry.get("entry_quality_score_0_to_100")))
        threshold = float(entry.get("score_threshold") or 85)
    except (TypeError, ValueError):
        return False, "INVALID_SCORE", {}
    if not all(math.isfinite(x) for x in (setup_score, setup_max, entry_score, threshold)) or setup_max <= 0:
        return False, "INVALID_SCORE", {}
    if setup_score < setup_max * 0.8:
        return False, "SETUP_SCORE_BELOW_80_PERCENT", {"setup_score": setup_score, "setup_max": setup_max}
    if entry_score < threshold:
        return False, "ENTRY_SCORE_BELOW_THRESHOLD", {"entry_score": entry_score, "threshold": threshold}
    try:
        confluence_score = float(dossier.get("confluence_score"))
    except (TypeError, ValueError):
        return False, "MISSING_CONFLUENCE", {}
    structural_full_score = (
        strategy in e._DEMO_STRUCTURAL_ENTRY_STRATEGIES
        and setup_score >= setup_max and setup_max > 0
    )
    # Research-only ablation floor. Production remains 85/82 in app/main.py.
    # The purpose is to measure candidates between 70 and the production floor,
    # not to authorize any order at these lower thresholds.
    minimum_confluence = 70.0
    if confluence_score < minimum_confluence:
        return False, "RESEARCH_CONFLUENCE_BELOW_70", {
            "confluence": confluence_score, "minimum_confluence": minimum_confluence,
        }
    return True, "RESEARCH_ENTRY_QUALITY_GATE_PASSED", {
        "entry_direction": direction, "expert_direction": expert_direction,
        "entry_score": entry_score, "threshold": threshold, "strategy": strategy,
        "setup_score": setup_score, "setup_max": setup_max,
        "confluence": dossier.get("confluence_score"),
        "minimum_confluence": minimum_confluence,
        "production_gate_bypassed": [
            "expert-direction agreement (tracked diagnostically, not required for this isolated entry-engine replay)",
            "production confluence thresholds 85/82 (research floor is 70)",
            "production strategy allow-list (research includes paused PULLBACK and LIQUIDITY_SWEEP)",
        ],
    }


# In the offline lab only, make paused-but-implemented strategies selectable
# so they receive actual entry/exit simulations. Production classifier and
# production allow-list remain untouched.
_PRODUCTION_SETUP_CLASSIFIER = e._classify_entry_setups_for_direction
_RESEARCH_STRATEGY_PRIORITY = {
    "BREAKOUT": 7, "COMPRESSION_BREAKOUT": 6, "LIQUIDITY_SWEEP": 5,
    "EMA_RECLAIM": 4, "MOMENTUM_CONTINUATION": 3, "PULLBACK": 2,
}
_RESEARCH_STRATEGY_ALLOWLIST = set(_RESEARCH_STRATEGY_PRIORITY)

def research_setup_classifier(direction, frames, reports, spread_atr_ratio):
    scan = _PRODUCTION_SETUP_CLASSIFIER(direction, frames, reports, spread_atr_ratio)
    # Preserve every original safety blocker (spread, volatility, extension).
    if scan.get("blockers"):
        return scan
    candidates = [
        item for item in (scan.get("candidates") or [])
        if str(item.get("strategy") or "") in _RESEARCH_STRATEGY_ALLOWLIST
    ]
    if not candidates:
        scan["selected"] = None
        return scan
    # Candidate list is already regime-fit ranked by the production classifier.
    # Selecting a paused candidate here is strictly for offline measurement.
    selected = candidates[0]
    scan["selected"] = selected
    scan["research_only_selection"] = bool(selected.get("research_only"))
    scan["research_selection_note"] = (
        "Offline-only selection of a strategy paused in production; this does not change production policy."
        if selected.get("research_only")
        else "Offline strategy-lab selection using the production candidate ranking."
    )
    return scan

e._classify_entry_setups_for_direction = research_setup_classifier


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
    trades=[]; blocked=defaultdict(int); blocked_details=defaultdict(int); engine_errors=defaultdict(int); signals=0; stale_skipped=0; h4_session_reopen_allowed=0; lower_tf_stale_skipped=0; h4_stale_over_7d_skipped=0; pos=None
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
        gross_r = pnl / pos["risk"]
        commission_per_lot = max(0.0, float(os.getenv("BACKTEST_COMMISSION_USD_PER_LOT_ROUND_TURN", "0")))
        contract_size = max(0.000001, float(os.getenv("BACKTEST_CONTRACT_SIZE", "100")))
        commission_r = commission_per_lot / (contract_size * pos["risk"])
        trades.append({"direction":d,"strategy":pos["strategy"],"entry_time":datetime.fromtimestamp(pos["entry_ts"],timezone.utc).isoformat(),"exit_time":datetime.fromtimestamp(t,timezone.utc).isoformat(),"entry_price":round(pos["entry"],2),"exit_price":round(exit_price,2),"stop":round(pos["stop"],2),"target_2R":round(pos["target"],2),"gross_R":round(gross_r,4),"commission_R":round(commission_r,5),"net_R":round(gross_r-commission_r,4),"entry_score":pos["score"],"confluence":pos["confluence"],"exit_reason":reason})
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
        quote_time = datetime.fromtimestamp(end - 60, timezone.utc)
        QUOTE[0]={"symbol":"GOLD","bid":b["close"],"ask":a["close"],"last":(a["close"]+b["close"])/2,"mid":(a["close"]+b["close"])/2,"spread_points":spread,"digits":2,"point":POINT,"source":"Historical BID/ASK","source_updated_at":quote_time,"age_seconds":60,"quote_type":"broker_bid_ask","tradable_quote":True}
        series={k:make_candles(frames[k],ends[k],end) for k in secs}
        if min(map(len,series.values()))<60: continue
        # Match production freshness limits, but do not count genuine historical
        # market closures/data gaps as engine failures or trading opportunities.
        # Mirror the production freshness contract: M5/M15/H1 stay strict;
        # a stale H4 is tolerated only as a bounded session-reopen gap when
        # every lower timeframe is independently fresh.
        lower_stale_frames = []
        for frame in ("M5", "M15", "H1"):
            age = (NOW[0] - series[frame][-1].time).total_seconds()
            if age > secs[frame] * 2 + 60:
                lower_stale_frames.append(frame)
        h4_age = (NOW[0] - series["H4"][-1].time).total_seconds()
        h4_normal_limit = secs["H4"] * 2 + 60
        h4_session_reopen_gap = (
            not lower_stale_frames
            and h4_age > h4_normal_limit
            and h4_age <= 7 * 24 * 60 * 60
        )
        stale_frames = list(lower_stale_frames)
        if h4_age > h4_normal_limit and not h4_session_reopen_gap:
            stale_frames.append("H4")
        if stale_frames:
            stale_skipped += 1
            if lower_stale_frames:
                lower_tf_stale_skipped += 1
            if h4_age > 7 * 24 * 60 * 60:
                h4_stale_over_7d_skipped += 1
            blocked["HISTORICAL_GAP_SKIPPED_" + "_".join(stale_frames)] += 1
            continue
        if h4_session_reopen_gap:
            h4_session_reopen_allowed += 1
        e._latest_mt5_candles={"symbol":"GOLD","timeframes":series,"received_at":NOW[0],"source":"Historical paired BID/ASK","data_quality":{"H4":{"h4_session_reopen_gap":h4_session_reopen_gap}}}
        signals+=1
        try:
            result=await e.live_expert_technical_analysis()
            ok,reason,details=research_entry_candidate_gate(result)
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
        direction_value=en.get("direction","WAIT")
        d=str(getattr(direction_value,"value",direction_value)).upper()
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
    def summarize(rows):
        wins_ = [x for x in rows if x["net_R"] > 0]
        losses_ = [x for x in rows if x["net_R"] <= 0]
        gross_profit_ = sum(x["net_R"] for x in wins_)
        gross_loss_ = abs(sum(x["net_R"] for x in losses_))
        eq_ = peak_ = dd_ = 0.0
        for trade_ in rows:
            eq_ += trade_["net_R"]
            peak_ = max(peak_, eq_)
            dd_ = max(dd_, peak_ - eq_)
        return {
            "trades": len(rows), "wins": len(wins_), "losses_or_breakeven": len(losses_),
            "win_rate_percent": round(100 * len(wins_) / len(rows), 2) if rows else None,
            "net_R": round(sum(x["net_R"] for x in rows), 3),
            "expectancy_R": round(sum(x["net_R"] for x in rows) / len(rows), 4) if rows else None,
            "profit_factor": round(gross_profit_ / gross_loss_, 3) if gross_loss_ else ("infinite" if gross_profit_ else None),
            "max_drawdown_R": round(dd_, 3),
        }
    split_ts = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
    train_trades = [x for x in trades if datetime.fromisoformat(x["entry_time"]).timestamp() < split_ts]
    validation_trades = [x for x in trades if datetime.fromisoformat(x["entry_time"]).timestamp() >= split_ts]
    strategy_metrics = {}
    for strategy_name in sorted({x["strategy"] or "UNKNOWN" for x in trades}):
        strategy_rows = [x for x in trades if (x["strategy"] or "UNKNOWN") == strategy_name]
        strategy_metrics[strategy_name] = {
            "all": summarize(strategy_rows),
            "train_may_june": summarize([x for x in strategy_rows if x in train_trades]),
            "validation_july": summarize([x for x in strategy_rows if x in validation_trades]),
        }
    production_gate_bypassed = [
        "expert-direction agreement (tracked diagnostically, not required for isolated entry-engine research)",
        "production confluence thresholds 85/82 (research floor 70)",
        "production strategy allow-list (research includes paused PULLBACK and LIQUIDITY_SWEEP)",
    ]
    report={"test":"RESEARCH-ONLY ENTRY ENGINE STRATEGY LAB: 65/72 entry score, 70 confluence floor, six implemented entry strategies; production execution gates unchanged; chronological May-June train / July untouched validation","mode":"OFFLINE_RESEARCH_ONLY_NOT_EXECUTION_APPROVED",
        "performance":{"all":summarize(trades),"train_may_june":summarize(train_trades),"validation_july":summarize(validation_trades),"by_strategy":strategy_metrics},"period_utc":{"start":datetime.fromtimestamp(paired[0]["ts"],timezone.utc).isoformat(),"end":datetime.fromtimestamp(paired[-1]["ts"]+60,timezone.utc).isoformat()},"data":{"bid_rows":len(bid),"ask_rows":len(ask),"paired_m1_rows":len(paired),"complete_bars":{k:len(v) for k,v in frames.items()}},"signals_evaluated":signals,"historical_gap_windows_skipped":stale_skipped,"h4_session_reopen_windows_allowed":h4_session_reopen_allowed,"lower_timeframe_stale_windows_skipped":lower_tf_stale_skipped,"h4_stale_over_7_days_skipped":h4_stale_over_7d_skipped,"production_gate_bypassed":production_gate_bypassed,"trades":len(trades),"wins":len(wins),"losses_or_breakeven":len(losses),"win_rate_percent":round(100*len(wins)/len(trades),2) if trades else None,"net_R":round(sum(x["net_R"] for x in trades),3),"expectancy_R":round(sum(x["net_R"] for x in trades)/len(trades),4) if trades else None,"profit_factor":round(gp/gl,3) if gl else ("infinite" if gp else None),"max_drawdown_R":round(dd,3),"monthly_net_R":{k:round(v,3) for k,v in sorted(monthly.items())},"blocked_reasons":dict(blocked),"blocked_detail_counts":dict(blocked_details),"engine_error_details":dict(engine_errors),"diagnostics":{k:(dict(v) if isinstance(v,defaultdict) else v) for k,v in diag.items()},"assumptions":{"point":POINT,"slippage_points_each_side":slip_points,"buy_entry":"ASK; exit BID","sell_entry":"BID; exit ASK","same_bar_SL_TP":"SL first","commission_usd_per_lot_round_turn":float(os.getenv("BACKTEST_COMMISSION_USD_PER_LOT_ROUND_TURN","0")),"contract_size_oz_per_lot":float(os.getenv("BACKTEST_CONTRACT_SIZE","100")),"commission_swap":"commission configurable; swaps still excluded because historical broker schedule/holding-account data were not supplied","macro_news":"neutral/advisory stubs; point-in-time historic snapshots unavailable","entry_score_thresholds":"research ablation thresholds 65 standard / 72 structural","production_confluence_thresholds":{"standard":85,"fully_confirmed_breakout":82},"research_confluence_floor":70,"research_allowed_strategies":["BREAKOUT","COMPRESSION_BREAKOUT","EMA_RECLAIM","MOMENTUM_CONTINUATION","LIQUIDITY_SWEEP","PULLBACK"],"research_only_strategies":["VWAP_RECLAIM","LIQUIDITY_SWEEP","PULLBACK"],"production_paused_strategies":["VWAP_RECLAIM","LIQUIDITY_SWEEP","PULLBACK"],"research_filter_selection":"May-June training period; July reserved as chronological validation","research_trigger_engine":"current production strategy classifier with closed-candle structural, pullback, momentum and compression filters; VWAP and range mean reversion remain research-only","research_spread_atr_max":0.20,"research_extension_atr_max":1.80,"research_quote_distance_atr_max":0.75,"setup_score_minimum_percent":80,"production_entry_gate_bypassed":production_gate_bypassed,"only_closed_bars":True},"limitations":["XAUUSD historical series may differ from XM GOLD server prices/contract specifications.","Macro and news use neutral advisory context because historical point-in-time snapshots were not supplied.","Configured commission is deducted when supplied; swaps, latency, requotes, stop-level rules and partial fills are not included.","Quality scores are not probabilities; historical win rate is not a future guarantee.","Historical simulation is still not production-approved; demo-only validation and XM broker parity are required before any execution."]}
    OUT.mkdir(exist_ok=True)
    (OUT/"summary.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    with (OUT/"trades.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(trades[0]) if trades else ["no_trades"]); w.writeheader(); w.writerows(trades)
    (OUT/"summary.md").write_text("# GoldAI Trader historical replay\n\n"+"\n".join(f"- {k}: {v}" for k,v in report.items() if k in ("period_utc","signals_evaluated","historical_gap_windows_skipped","h4_session_reopen_windows_allowed","lower_timeframe_stale_windows_skipped","h4_stale_over_7_days_skipped"))+"\n\n- Research-only production-gate bypasses: "+"; ".join(production_gate_bypassed)+"\n\n## Performance\n\n"+"\n".join(f"- {period}: {metrics}" for period,metrics in report["performance"].items() if period != "by_strategy")+"\n\n## Strategy breakdown\n\n"+"\n".join(f"- {name}: {metrics}" for name,metrics in report["performance"]["by_strategy"].items())+"\n\nSee summary.json and trades.csv. Offline research only; macro/news are neutralized. BID/ASK spread and configured slippage are modeled; commission is configurable and swaps/latency/requotes are not modeled. July is held out chronologically from May-June filter selection.\n",encoding="utf-8")
    print(json.dumps(report,indent=2,ensure_ascii=False))

if __name__=="__main__": asyncio.run(run())
