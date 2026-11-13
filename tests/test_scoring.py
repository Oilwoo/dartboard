from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from dartboard.ledger import Judgment, Ledger
from dartboard.prices import KST, Bar, KisError
from dartboard.scoring import (
    ScoreRun,
    calendar_mismatches,
    entry_session,
    is_official,
    plan,
    score_due,
)

# January 2026: Monday the 5th to Friday the 16th, with Wednesday the 7th as a market holiday in some tests
WEEKDAYS = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
MON, TUE, WED, THU, FRI, MON2, TUE2 = WEEKDAYS[:7]


def kst(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def test_a_judgment_before_0850_enters_at_that_days_open():
    assert entry_session(WEEKDAYS, kst(MON, 8, 49)) == MON
    assert entry_session(WEEKDAYS, kst(MON, 8, 50)) == TUE   # too late to reach the opening auction
    assert entry_session(WEEKDAYS, kst(MON, 14, 0)) == TUE   # during the session: never the same day's prices
    assert entry_session(WEEKDAYS, kst(date(2026, 1, 10), 11, 0)) == MON2  # Saturday


def test_exit_is_the_close_of_the_fifth_session_counting_entry_as_one():
    p = plan(WEEKDAYS, kst(MON, 20, 0), "20260105", 5)
    assert (p.entry, p.exit, p.on_time) == (TUE, MON2, True)
    assert plan(WEEKDAYS, kst(MON, 20, 0), "20260105", 1).exit == TUE  # one session: open to close


def test_holidays_come_from_the_index_calendar():
    without_wed = [d for d in WEEKDAYS if d != WED]
    assert plan(without_wed, kst(MON, 20, 0), "20260105", 5).exit == TUE2


def test_the_exit_must_have_happened():
    assert plan(WEEKDAYS[:5], kst(MON, 20, 0), "20260105", 5) is None


def test_a_judgment_made_after_the_first_session_following_the_receipt_date_is_late():
    assert plan(WEEKDAYS, kst(MON, 8, 0), "20260105", 5).on_time        # pre-market filing, same-day open
    assert plan(WEEKDAYS, kst(TUE, 8, 0), "20260105", 5).on_time        # judged overnight, next open
    assert not plan(WEEKDAYS, kst(TUE, 10, 0), "20260105", 5).on_time   # the PC was off a whole day


def judgment(rcept_no, ticker="005930", market="Y", **changes):
    j = Judgment(rcept_no=rcept_no, rcept_dt=rcept_no[:8], corp_code="c" + ticker, corp_name="회사", ticker=ticker,
                 market=market, filing_type="B001", report_nm="공시", base_title="공시", kind="new", route="universe",
                 status="forecast", p_up=0.6, summary="요약", model="local", prompt_version="v1",
                 faith_checked=0, faith_supported=0)
    return replace(j, **changes)


def bars(days, start, step):
    return [Bar(d, start + step * i, start + step * i + step / 2) for i, d in enumerate(days)]


class FakePrices:
    def __init__(self, data, broken=()):
        self.data, self.broken, self.calls = data, set(broken), []

    def daily(self, code, start, end, index=False):
        self.calls.append((code, index, start, end))
        if code in self.broken:
            raise KisError("EGW00201 초당 거래건수를 초과하였습니다.")
        return [b for b in self.data.get(code, []) if start <= b.day <= end]


def ledger_at(*times):
    """A ledger whose judgments are stamped with `times`; the ledger's own creation stamp comes first."""
    ticks = iter(("2026-01-01T00:00:00+00:00",) + times)
    return Ledger(clock=lambda: next(ticks, "2026-02-01T00:00:00+00:00"))


def test_score_due_uses_the_market_benchmark_and_regular_session_prices():
    led = ledger_at("2026-01-05T11:00:00+00:00", "2026-01-05T11:00:00+00:00")  # Monday 20:00 KST
    kospi = led.record_judgment(judgment("20260105000001", "005930", "Y"))
    kosdaq = led.record_judgment(judgment("20260105000002", "035720", "K"))
    prices = FakePrices({"005930": bars(WEEKDAYS, 100, 2), "035720": bars(WEEKDAYS, 50, 1),
                         "0001": bars(WEEKDAYS, 1000, 10), "1001": bars(WEEKDAYS, 800, 4)})
    run = score_due(led, prices, TUE2)  # Monday the 12th closed yesterday: the 5-session window has ended
    assert (run.recorded, run.failed) == (4, 0)  # 1 and 5 sessions for both; 20 sessions still waiting
    out = {(o.judgment_seq, o.horizon): o for _, o in led.outcomes()}
    o = out[(kospi, 5)]
    assert (o.entry_day, o.exit_day, o.on_time) == ("2026-01-06", "2026-01-12", True)
    assert (o.stock_open, o.stock_close) == (102, 111)          # Tuesday's open, Monday the 12th's close
    assert (o.index_open, o.index_close) == (1010, 1055)
    assert out[(kosdaq, 5)].index_open == 804                    # KOSDAQ index for a KOSDAQ company
    assert ("1001", True, MON, MON2) in prices.calls


def test_todays_bar_is_never_used_because_its_close_may_not_be_final():
    led = ledger_at("2026-01-05T11:00:00+00:00")
    led.record_judgment(judgment("20260105000001"))
    prices = FakePrices({"005930": bars(WEEKDAYS, 100, 2), "0001": bars(WEEKDAYS, 1000, 10)})
    run = score_due(led, prices, MON2)  # the 5th session is today: wait for tomorrow
    assert {o.horizon for _, o in led.outcomes()} == {1}
    assert run.waiting == 1


def test_a_halt_inside_the_window_is_recorded_as_unscorable():
    led = ledger_at("2026-01-05T11:00:00+00:00")
    led.record_judgment(judgment("20260105000001"))
    halted = [b for b in bars(WEEKDAYS, 100, 2) if b.day != THU]
    score_due(led, FakePrices({"005930": halted, "0001": bars(WEEKDAYS, 1000, 10)}), TUE2)
    out = {o.horizon: o for _, o in led.outcomes()}
    assert out[1].status == "scored"
    assert out[5].status == "unscorable" and "2026-01-08" in out[5].reason


def test_one_failed_price_call_does_not_stop_the_others_and_stays_due():
    led = ledger_at(*["2026-01-05T11:00:00+00:00"] * 3)
    led.record_judgment(judgment("20260105000001", "000660"))
    led.record_judgment(judgment("20260105000002", "000661"))
    fine = led.record_judgment(judgment("20260105000003", "005930"))
    prices = FakePrices({"005930": bars(WEEKDAYS, 100, 2), "0001": bars(WEEKDAYS, 1000, 10)},
                        broken={"000660", "000661"})
    run = score_due(led, prices, TUE2)
    assert run.failed == 2 and {o.judgment_seq for _, o in led.outcomes()} == {fine}
    assert len(led.unscored((1, 5, 20))) == 3  # the failed ones keep all horizons, the fine one keeps 20


def test_only_new_on_time_five_session_forecasts_are_official():
    led = ledger_at("2026-01-06T01:00:00+00:00", "2026-01-06T01:00:00+00:00")  # Tuesday 10:00 KST
    late = led.record_judgment(judgment("20260105000001"))                       # filed Monday
    follow = led.record_judgment(judgment("20260106000002", kind="follow_up"))   # filed Tuesday
    score_due(led, FakePrices({"005930": bars(WEEKDAYS, 100, 2), "0001": bars(WEEKDAYS, 1000, 10)}),
              date(2026, 1, 14))
    out = {(o.judgment_seq, o.horizon): o for _, o in led.outcomes()}
    assert not out[(late, 5)].on_time
    assert not is_official(led.judgment(late), out[(late, 5)])
    assert out[(follow, 5)].on_time and not is_official(led.judgment(follow), out[(follow, 5)])
    assert is_official(replace(led.judgment(follow), kind="new"), out[(follow, 5)])
    assert not is_official(replace(led.judgment(follow), kind="new"), out[(follow, 1)])


def test_nothing_pending_makes_no_price_calls():
    prices = FakePrices({})
    assert score_due(Ledger(), prices, TUE2) == ScoreRun(0, 0, 0) and prices.calls == []


@pytest.mark.parametrize("hour", [7, 8])
def test_receipt_date_and_judgment_on_the_same_morning_are_on_time(hour):
    assert plan(WEEKDAYS, kst(MON, hour, 30), "20260105", 5).on_time


@pytest.mark.parametrize("halted", [TUE, MON2])
def test_a_halt_on_the_entry_or_exit_day_is_unscorable_not_a_crash(halted):
    led = ledger_at("2026-01-05T11:00:00+00:00")
    led.record_judgment(judgment("20260105000001"))
    stock = [b for b in bars(WEEKDAYS, 100, 2) if b.day != halted]
    score_due(led, FakePrices({"005930": stock, "0001": bars(WEEKDAYS, 1000, 10)}), TUE2)
    assert {o.horizon: o.status for _, o in led.outcomes()}[5] == "unscorable"


class TodayLeaks(FakePrices):
    def daily(self, code, start, end, index=False):  # a source that ignores the end date
        return super().daily(code, start, date(2026, 12, 31), index)


def test_even_if_the_source_returns_todays_bar_it_is_not_a_session_yet():
    led = ledger_at("2026-01-05T11:00:00+00:00")
    led.record_judgment(judgment("20260105000001"))
    score_due(led, TodayLeaks({"005930": bars(WEEKDAYS, 100, 2), "0001": bars(WEEKDAYS, 1000, 10)}), MON2)
    assert {o.horizon for _, o in led.outcomes()} == {1}


def test_a_failed_index_call_keeps_that_markets_forecasts_due_and_counts_them():
    led = ledger_at(*["2026-01-05T11:00:00+00:00"] * 3)
    led.record_judgment(judgment("20260105000001", "035720", "K"))
    led.record_judgment(judgment("20260105000002", "035721", "K"))
    fine = led.record_judgment(judgment("20260105000003", "005930", "Y"))
    prices = FakePrices({"005930": bars(WEEKDAYS, 100, 2), "0001": bars(WEEKDAYS, 1000, 10)}, broken={"1001"})
    run = score_due(led, prices, TUE2)
    assert (run.recorded, run.waiting, run.failed) == (2, 1, 2)
    assert {o.judgment_seq for _, o in led.outcomes()} == {fine}


def test_counts_of_waiting_forecasts():
    led = ledger_at(*["2026-01-05T11:00:00+00:00"] * 2, *["2026-01-12T11:00:00+00:00"] * 2)
    for i, ticker in enumerate(("005930", "000660", "035420", "051910")):
        led.record_judgment(judgment(f"2026010{5 if i < 2 else 9}00000{i}", ticker))
    days = [d for d in (date(2026, 1, 5) + timedelta(i) for i in range(90)) if d.weekday() < 5]
    data = {t: bars(days, 100, 2) for t in ("005930", "000660", "035420", "051910")}
    run = score_due(led, FakePrices(dict(data, **{"0001": bars(days, 1000, 10)})), TUE2)
    # two judged on the 5th: 1 and 5 sessions done, 20 waiting; two judged on the 12th: nothing due yet
    assert (run.recorded, run.waiting, run.failed) == (4, 4, 0)
    assert score_due(led, FakePrices(dict(data, **{"0001": bars(days, 1000, 10)})), date(2026, 2, 20)).waiting == 0


def test_last_session_entry_with_nothing_after_it_is_still_on_time():
    assert plan([MON], kst(MON, 8, 0), "20260105", 1).on_time


def test_calendar_mismatches_find_a_missing_index_bar():
    open_days = {d: d.weekday() < 5 for d in (date(2026, 1, 5) + timedelta(i) for i in range(7))}
    assert calendar_mismatches(WEEKDAYS, open_days, MON, date(2026, 1, 11)) == []
    without_thu = [d for d in WEEKDAYS if d != THU]
    assert calendar_mismatches(without_thu, open_days, MON, date(2026, 1, 11)) == [THU]
    assert calendar_mismatches(without_thu, open_days, MON, WED) == []  # outside the compared range
