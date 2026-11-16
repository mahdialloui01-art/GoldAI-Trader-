from app.main import _demo_candidate_quality_gate


def _analysis(score=85.0, strategy="PULLBACK", setup_score=4, blockers=None):
    return {
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
        }
    }


def test_demo_gate_accepts_high_confluence_confirmed_setup():
    passed, reason, details = _demo_candidate_quality_gate(_analysis())
    assert passed is True
    assert details["confluence_score"] == 85.0
    assert details["selected_strategy"] == "PULLBACK"


def test_demo_gate_rejects_confluence_below_85():
    passed, reason, details = _demo_candidate_quality_gate(_analysis(score=84.99))
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
