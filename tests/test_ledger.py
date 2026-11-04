import sqlite3

import pytest

from dartboard.ledger import Ledger


def make(tmp_path=None):
    ticks = iter(f"2026-01-{d:02d}T09:00:00+00:00" for d in range(1, 29))
    return Ledger(str(tmp_path / "l.db") if tmp_path else ":memory:", clock=lambda: next(ticks))


def test_append_and_chain_is_intact():
    led = make()
    led.append("005930", "buy", 0.7, 20, "근거", ["dart:1"], "qwen")
    led.append("000660", "sell", 0.6, 20, "근거", ["dart:2"], "qwen")
    assert led.verify_chain() is None


def test_rows_cannot_be_edited_or_deleted():
    led = make()
    fid = led.append("005930", "buy", 0.7, 20, "근거", ["dart:1"], "qwen")
    with pytest.raises(sqlite3.IntegrityError):
        led.db.execute("UPDATE forecasts SET stance='sell' WHERE id=?", (fid,))
    with pytest.raises(sqlite3.IntegrityError):
        led.db.execute("DELETE FROM forecasts WHERE id=?", (fid,))


def test_tampering_with_the_file_is_detected(tmp_path):
    led = make(tmp_path)
    led.append("005930", "buy", 0.7, 20, "근거", ["dart:1"], "qwen")
    bad = led.append("000660", "sell", 0.6, 20, "근거", ["dart:2"], "qwen")
    raw = sqlite3.connect(str(tmp_path / "l.db"))  # someone bypasses the app and edits the database
    raw.execute("DROP TRIGGER forecasts_no_update")
    raw.execute("UPDATE forecasts SET stance='buy' WHERE id=?", (bad,))
    raw.commit()
    assert Ledger(str(tmp_path / "l.db")).verify_chain() == bad


def test_horizon_counts_korean_calendar_days():
    led = Ledger(clock=lambda: "2026-01-05T22:30:00+00:00")  # Tuesday 07:30 in Korea, still Monday in UTC
    led.append("A", "buy", 0.6, 5, "r", [], "m")
    assert led.due("2026-01-10") == []        # 5 days after Monday would score it a day early
    assert len(led.due("2026-01-11")) == 1    # 5 days after Tuesday


def test_invalid_stance_is_rejected():
    with pytest.raises(ValueError):
        make().append("005930", "moon", 0.9, 20, "x", [], "qwen")


def test_due_scoring_and_report():
    led = make()
    a = led.append("A", "buy", 0.8, 5, "r", [], "m")     # created 01-01, due 01-06
    b = led.append("B", "sell", 0.6, 5, "r", [], "m")    # created 01-02, due 01-07
    c = led.append("C", "hold", 0.5, 30, "r", [], "m")   # not due yet
    assert [r[0] for r in led.due("2026-01-07")] == [a, b]
    led.score(a, stock_return=0.05, bench_return=0.01)   # buy, +4%p excess -> hit
    led.score(b, stock_return=0.03, bench_return=0.01)   # sell, +2%p excess -> miss
    assert [r[0] for r in led.due("2026-01-07")] == []
    rep = led.report()
    assert rep.scored == 2
    assert rep.hit_rate == 0.5
    assert rep.brier == pytest.approx(((0.8 - 1) ** 2 + (0.6 - 0) ** 2) / 2)
    assert rep.mean_excess_buy == pytest.approx(0.04)
    assert rep.mean_excess_sell == pytest.approx(0.02)
    assert c not in [r[0] for r in led.due("2026-01-07")]
