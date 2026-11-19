from datetime import datetime, timezone

from app import learning_store


class FakeConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))
        return self


def test_xm_quote_sample_is_stored_as_quote_not_trade_delta(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(learning_store, "_connect", lambda: fake)
    stamp = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)

    assert learning_store.save_xm_quote_sample(
        symbol="GOLD", timestamp=stamp, bid=2650.0, ask=2650.2,
        last=2650.1, spread_points=20, point=0.01, digits=2,
    )
    assert len(fake.calls) == 1
    query, params = fake.calls[0]
    assert "INSERT INTO xm_quote_samples" in query
    assert params[0] == stamp
    assert params[1] == "GOLD"
    assert params[2:4] == (2650.0, 2650.2)


def test_xm_quote_sample_rejects_naive_timestamp(monkeypatch):
    def unexpected_connect():
        raise AssertionError("database must not be contacted for naive timestamps")

    monkeypatch.setattr(learning_store, "_connect", unexpected_connect)
    assert not learning_store.save_xm_quote_sample(
        symbol="GOLD", timestamp=datetime(2026, 11, 19, 10, 0),
        bid=2650.0, ask=2650.2, last=2650.1,
        spread_points=20, point=0.01, digits=2,
    )
