from app.main import Direction, _reconcile_entry_engine_quality


def _analysis(entry_score=89, confluence=9.5):
    return {
        "technical_dossier": {"confluence_score": confluence},
        "entry_engine": {
            "direction": Direction.BUY,
            "entry_quality_score_0_to_100": entry_score,
            "score_threshold": 85,
            "selected_strategy": "MOMENTUM_CONTINUATION",
            "setup": {"entry_reference": 2300.0},
            "blockers": [],
            "reasons": [],
        },
    }


def test_low_multi_timeframe_confluence_caps_high_entry_score_and_forces_wait():
    result = _reconcile_entry_engine_quality(_analysis())
    entry = result["entry_engine"]

    assert entry["raw_entry_quality_score_0_to_100"] == 89
    assert entry["cross_layer_confluence_score"] == 9.5
    assert entry["entry_quality_score_0_to_100"] == 9.5
    assert entry["direction"] == Direction.WAIT
    assert entry["setup"] is None
    assert "CROSS_LAYER_CONFLUENCE_BELOW_ENTRY_THRESHOLD" in entry["blockers"]


def test_strong_confluence_preserves_entry_score_when_both_layers_pass():
    result = _reconcile_entry_engine_quality(_analysis(entry_score=89, confluence=91))
    entry = result["entry_engine"]

    assert entry["entry_quality_score_0_to_100"] == 89
    assert entry["direction"] == Direction.BUY
    assert entry["setup"] is not None


def test_missing_layer_score_fails_closed():
    analysis = _analysis()
    analysis["technical_dossier"]["confluence_score"] = None
    result = _reconcile_entry_engine_quality(analysis)
    entry = result["entry_engine"]

    assert entry["entry_quality_score_0_to_100"] == 89
    assert entry["effective_entry_quality_score_0_to_100"] == 0
    assert entry["direction"] == Direction.WAIT
    assert entry["setup"] is None
    assert "CROSS_LAYER_SCORE_UNAVAILABLE" in entry["blockers"]
