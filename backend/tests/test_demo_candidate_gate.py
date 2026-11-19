from app.main import _demo_candidate_quality_gate


def _analysis(score=85.0, strategy="PULLBACK", setup_score=4, blockers=None):
    return {
        "decision": "BUY",
        "technical_dossier": {
            "confluence_score": score,
            "strategy_scan": {
                "selected": {
                    "strategy": strategy,
                    "score": setup_score,
                    "max_score": 5,
                },
                "blockers": blockers or [],
            },
        },
        "entry_engine": {
            "direction": "BUY",
            "selected_strategy": strategy,
            "entry_quality_score_0_to_100": score,
            "effective_entry_quality_score_0_to_100": score,
            "score_threshold": 82 if strategy in ("BREAKOUT", "LIQUIDITY_SWEEP") and setup_score >= 5 else 85,
            "setup": {
                "strategy": strategy,
                "entry_reference": 2300.0,
                "stop_loss": 2295.0,
                "take_profit": {"TP2_2R": 2310.0},
            },
            "blockers": [],
        },
    }


def test_demo_gate_accepts_high_confluence_confirmed_setup():
    passed, reason, details = _demo_candidate_quality_gate(_analysis())
    assert passed is True
    assert details["confluence_score"] == 85.0
    assert details["selected_strategy"] == "PULLBACK"


def test_demo_gate_rejects_pullback_below_85():
    passed, reason, details = _demo_candidate_quality_gate(_analysis(score=84.99))
    assert passed is False
    assert "85/100" in reason


def test_demo_gate_accepts_fully_confirmed_breakout_at_82():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=82.0, strategy="BREAKOUT", setup_score=5)
    )
    assert passed is True
    assert details["minimum_confluence_score"] == 82.0
    assert details["threshold_profile"] == "STRUCTURAL_FULL_CONFIRMATION"


def test_demo_gate_rejects_breakout_below_82():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=81.99, strategy="BREAKOUT", setup_score=5)
    )
    assert passed is False
    assert "82/100" in reason


def test_demo_gate_keeps_partial_breakout_at_85():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=84.99, strategy="BREAKOUT", setup_score=4)
    )
    assert passed is False
    assert "85/100" in reason


def test_demo_gate_rejects_reversal_watch():
    passed, reason, details = _demo_candidate_quality_gate(_analysis(strategy="REVERSAL_WATCH"))
    assert passed is False
    assert details["selected_strategy"] == "REVERSAL_WATCH"


def test_demo_gate_rejects_strategy_blockers():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(blockers=["SPREAD_TOO_HIGH"])
    )
    assert passed is False
    assert details["strategy_blockers"] == ["SPREAD_TOO_HIGH"]


def test_demo_gate_rejects_weak_setup_score():
    passed, reason, details = _demo_candidate_quality_gate(_analysis(setup_score=3))
    assert passed is False
    assert details["setup_score"] == 3.0


def test_demo_gate_rejects_vwap_reclaim_after_negative_initial_backtest():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=95.0, strategy="VWAP_RECLAIM", setup_score=5)
    )
    assert passed is False
    assert "informational only or unsupported" in reason
    assert details["selected_strategy"] == "VWAP_RECLAIM"


def test_demo_gate_rejects_research_only_range_mean_reversion():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=90.0, strategy="RANGE_MEAN_REVERSION", setup_score=5)
    )
    assert passed is False
    assert details["selected_strategy"] == "RANGE_MEAN_REVERSION"

def test_demo_gate_rejects_liquidity_sweep_after_negative_walk_forward_replay():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=95.0, strategy="LIQUIDITY_SWEEP", setup_score=5)
    )
    assert passed is False
    assert "informational only or unsupported" in reason
    assert details["selected_strategy"] == "LIQUIDITY_SWEEP"

def test_demo_gate_rejects_pullback_after_negative_training_and_weak_validation():
    passed, reason, details = _demo_candidate_quality_gate(
        _analysis(score=95.0, strategy="PULLBACK", setup_score=5)
    )
    assert passed is False
    assert "informational only or unsupported" in reason
    assert details["selected_strategy"] == "PULLBACK"
