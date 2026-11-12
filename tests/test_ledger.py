import sqlite3
from dataclasses import replace

import pytest

from dartboard.ledger import Judgment, Ledger, OldLedgerError, Outcome, _canonical


def ticking_clock():
    ticks = iter(f"2026-01-{d:02d}T09:00:00+00:00" for d in range(1, 29))
    return lambda: next(ticks)


def make(path=":memory:"):
    return Ledger(str(path), clock=ticking_clock())


def judgment(rcept_no="20260105000001", **changes):
    j = Judgment(rcept_no=rcept_no, rcept_dt=rcept_no[:8], corp_code="00126380", corp_name="테스트전자",
                 ticker="005930", market="Y", filing_type="B001", report_nm="주요사항보고서(자기주식취득결정)",
                 base_title="주요사항보고서(자기주식취득결정)", kind="new", route="universe", status="forecast",
                 p_up=0.6, summary="요약", model="local", prompt_version="v1", faith_checked=1, faith_supported=1)
    return replace(j, **changes)


def outcome(seq, horizon=5, **changes):
    o = Outcome(judgment_seq=seq, horizon=horizon, status="scored", entry_day="2026-01-06", exit_day="2026-01-12",
                on_time=True, stock_open=100.0, stock_close=110.0, index_open=1000.0, index_close=1050.0)
    return replace(o, **changes)


def test_judgments_reviews_and_outcomes_share_one_intact_chain():
    led = make()
    a = led.record_judgment(judgment("20260105000001"))
    b = led.record_judgment(judgment("20260105000002", route="watchlist", faith_supported=0, unsupported=["3.2%"]))
    led.record_review(b, "discard")
    led.record_outcome(outcome(a))
    assert led.verify_chain() is None
    assert led.head()[0] == 4
    assert led.reviews() == {b: "discard"}
    assert led.outcomes()[0][1].excess == pytest.approx(0.10 - 0.05)


def test_the_same_step_twice_writes_one_entry():
    # a crash between the ledger write and the checkpoint makes LangGraph run the step again
    led = make()
    first = led.record_judgment(judgment())
    assert led.record_judgment(judgment()) == first
    assert led.record_review(first, "keep") == led.record_review(first, "keep")
    assert led.record_outcome(outcome(first)) == led.record_outcome(outcome(first))
    assert led.head()[0] == 3


def test_a_different_entry_under_the_same_key_is_refused():
    led = make()
    seq = led.record_judgment(judgment())
    with pytest.raises(ValueError):
        led.record_judgment(judgment(p_up=0.9))
    led.record_review(seq, "keep")
    with pytest.raises(ValueError):
        led.record_review(seq, "discard")  # a human decision cannot be changed afterwards


def test_entries_cannot_be_edited_or_deleted():
    led = make()
    led.record_judgment(judgment())
    with pytest.raises(sqlite3.IntegrityError):
        led.db.execute("UPDATE entries SET body='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        led.db.execute("DELETE FROM entries")


def test_tampering_with_the_file_is_detected(tmp_path):
    led = make(tmp_path / "l.db")
    led.record_judgment(judgment("20260105000001"))
    bad = led.record_judgment(judgment("20260105000002"))
    raw = sqlite3.connect(str(tmp_path / "l.db"))  # someone bypasses the app and edits the database
    raw.execute("DROP TRIGGER entries_no_update")
    raw.execute("UPDATE entries SET body=replace(body, '0.6', '0.9') WHERE seq=?", (bad,))
    raw.commit()
    assert Ledger(str(tmp_path / "l.db")).verify_chain() == bad


def test_a_stage_one_ledger_is_never_converted_or_dropped(tmp_path):
    path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
    old.commit()
    with pytest.raises(OldLedgerError):
        Ledger(str(path))
    tables = {r[0] for r in sqlite3.connect(str(path)).execute("SELECT name FROM sqlite_master")}
    assert tables == {"forecasts"}


@pytest.mark.parametrize("changes", [
    {"p_up": 1.2}, {"p_up": None}, {"status": "failed"}, {"status": "maybe"},
    {"kind": "rumor"}, {"route": "everyone"}, {"market": "N"},
])
def test_invalid_judgments_are_refused(changes):
    with pytest.raises(ValueError):
        judgment(**changes)


def test_failed_judgment_is_recorded_but_cannot_have_an_outcome():
    led = make()
    seq = led.record_judgment(judgment(status="failed", p_up=None, summary="", model=""))
    assert led.unscored((1, 5, 20)) == []
    with pytest.raises(ValueError):
        led.record_outcome(outcome(seq))


def test_scored_outcome_needs_prices():
    with pytest.raises(ValueError):
        outcome(1, stock_open=None)
    assert outcome(1, status="unscorable", stock_open=None, stock_close=None, reason="no stock price").status


def test_unscored_lists_the_missing_horizons():
    led = make()
    seq = led.record_judgment(judgment())
    led.record_outcome(outcome(seq, horizon=1))
    [(entry, j, missing)] = led.unscored((1, 5, 20))
    assert entry.seq == seq and missing == [5, 20]


def test_follow_up_finds_the_latest_earlier_filing_with_the_same_title():
    led = make()
    led.record_judgment(judgment("20260105000001"))
    led.record_judgment(judgment("20260106000001"))
    led.record_judgment(judgment("20260106000002", base_title="단일판매공급계약체결"))
    assert led.find_original("00126380", "주요사항보고서(자기주식취득결정)", "20260107000001") == "20260106000001"
    assert led.find_original("00126380", "주요사항보고서(자기주식취득결정)", "20260105000001") is None
    assert led.find_original("99999999", "주요사항보고서(자기주식취득결정)", "20260107000001") is None


def test_hash_format_never_changes_or_old_ledgers_would_stop_verifying():
    # golden value: if this fails, the hashing or JSON format changed and every existing ledger breaks
    led = Ledger(clock=lambda: "2026-01-05T09:00:00+00:00")
    seq = led.record_judgment(judgment())
    led.record_review(seq, "keep")
    led.record_outcome(outcome(seq))
    assert led.head() == (3, "9aa4743a8fef0f6d548b82967c76a548a6969fd788bdcf581a994b5011dd2ed9")
    assert _canonical({"b": 1, "a": "한", "c": [1.5, None]}) == '{"a":"한","b":1,"c":[1.5,null]}'


def test_empty_ledger_head_and_metadata():
    led = Ledger()
    assert led.head() == (0, "genesis")
    assert dict(led.db.execute("SELECT name, value FROM meta"))["schema_version"] == "2"
    assert led.clock().endswith("+00:00")  # stamps are UTC with an offset, never naive local time


def test_entries_keep_their_kind_and_key():
    led = make()
    seq = led.record_judgment(judgment())
    led.record_review(seq, "keep")
    assert [(e.kind, e.key) for e in led.entries()] == [("judgment", "judgment:20260105000001"),
                                                         ("review", f"review:{seq}")]
    assert led._judgment_entry(seq) == led.entries("judgment")[0]
    with pytest.raises(KeyError):
        led._judgment_entry(seq + 1)  # the review is not a judgment


@pytest.mark.parametrize("p", [0.0, 1.0, 0])
def test_certain_forecasts_are_allowed(p):
    assert judgment(p_up=p).p_up == p


def test_prices_must_be_positive():
    assert outcome(1, stock_open=0.5).stock_open == 0.5
    with pytest.raises(ValueError):
        outcome(1, index_close=0.0)
