from datetime import date

import pytest

from dartboard.ledger import Ledger
from dartboard.prices import Bar
from dartboard.scoring import kst_date, score_due, window_return

MON, TUE, WED, THU, FRI = (date(2026, 1, d) for d in (5, 6, 7, 8, 9))


def test_entry_is_the_next_trading_day_open_not_the_forecast_day():
    bars = [Bar(MON, 100, 105), Bar(TUE, 106, 110), Bar(WED, 111, 121)]
    assert window_return(bars, MON, 2) == pytest.approx(121 / 106 - 1)


def test_exit_is_the_last_close_on_or_before_the_horizon():
    bars = [Bar(THU, 100, 100), Bar(FRI, 101, 120), Bar(date(2026, 1, 12), 121, 150)]
    # made Wednesday, horizon ends Sunday 01-11 -> exits at Friday's close
    assert window_return(bars, WED, 4) == pytest.approx(120 / 100 - 1)


def test_no_prices_after_the_forecast_returns_none():
    assert window_return([Bar(MON, 100, 100)], MON, 5) is None


def test_long_trading_halt_returns_none():
    # one bar, then nothing for a month: suspended or delisted -> needs a human, not a made-up return
    assert window_return([Bar(TUE, 100, 100)], MON, 30) is None


def test_forecast_date_is_taken_in_korean_time():
    assert kst_date("2026-01-01T16:30:00+00:00") == date(2026, 1, 2)


class FakePrices:
    def __init__(self, data):
        self.data, self.calls = data, []

    def daily(self, code, start, end, index=False):
        self.calls.append((code, index))
        return [b for b in self.data.get(code, []) if start <= b.day <= end]


def rising(open_, step):
    return [Bar(d, open_, open_ + step * (i + 1)) for i, d in enumerate((TUE, WED, THU))]


def test_score_due_uses_the_market_benchmark_and_keeps_missing_prices_due():
    ledger = Ledger(clock=lambda: "2026-01-05T01:00:00+00:00")  # Monday 10:00 KST, market open
    kospi = ledger.append("005930", "buy", 0.7, 3, "r", [], "m", market="Y")
    kosdaq = ledger.append("035720", "sell", 0.6, 3, "r", [], "m", market="K")
    halted = ledger.append("999999", "buy", 0.6, 3, "r", [], "m", market="K")
    prices = FakePrices({"005930": rising(100, 10),   # +30%
                         "035720": rising(100, 1),    # +3%
                         "0001": rising(1000, 50),    # KOSPI +15%
                         "1001": rising(1000, 20)})   # KOSDAQ +6%
    results = {r.forecast_id: r for r in score_due(ledger, prices, FRI)}

    assert results[kospi].stock_return == pytest.approx(0.30) and results[kospi].bench_return == pytest.approx(0.15)
    assert results[kosdaq].bench_return == pytest.approx(0.06)
    assert not results[halted].scored
    assert ("0001", True) in prices.calls and ("1001", True) in prices.calls
    assert [row[0] for row in ledger.due(FRI.isoformat())] == [halted]  # retried on the next run
    rep = ledger.report()
    assert rep.scored == 2 and rep.hit_rate == 1.0  # buy beat KOSPI, sell lagged KOSDAQ
