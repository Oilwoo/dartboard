"""Scores due forecasts: stock return minus benchmark return over the same window.

Entry: the open of the first trading day AFTER the forecast date (KST). Opinions are often made
during market hours, so using that day's prices would leak information we did not have.
Exit: the close of the last trading day on or before forecast date + horizon_days.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .ledger import Ledger
from .prices import BENCHMARK, Bar

KST = ZoneInfo("Asia/Seoul")
MAX_GAP_DAYS = 10  # last bar further than this before the exit date: halted or delisted -> manual check


class PriceSource(Protocol):
    def daily(self, code: str, start: date, end: date, index: bool = False) -> list[Bar]: ...


def window_return(bars: list[Bar], made_on: date, horizon_days: int) -> float | None:
    after = [b for b in bars if b.day > made_on]
    if not after:
        return None
    exit_by = made_on + timedelta(days=horizon_days)
    held = [b for b in after if b.day <= exit_by]
    if not held or (exit_by - held[-1].day).days > MAX_GAP_DAYS:
        return None
    return held[-1].close / held[0].open - 1


def kst_date(created_at: str) -> date:
    return datetime.fromisoformat(created_at).astimezone(KST).date()


@dataclass(frozen=True)
class ScoreResult:
    forecast_id: int
    ticker: str
    stock_return: float | None
    bench_return: float | None

    @property
    def scored(self) -> bool:
        return self.stock_return is not None and self.bench_return is not None


def score_due(ledger: Ledger, prices: PriceSource, today: date) -> list[ScoreResult]:
    results = []
    for fid, ticker, market, created_at, horizon in ledger.due(today.isoformat()):
        made_on = kst_date(created_at)
        end = made_on + timedelta(days=horizon)
        stock = window_return(prices.daily(ticker, made_on, end), made_on, horizon)
        bench = window_return(prices.daily(BENCHMARK.get(market, "0001"), made_on, end, index=True), made_on, horizon)
        r = ScoreResult(fid, ticker, stock, bench)
        if r.scored:
            ledger.score(fid, stock, bench)
        results.append(r)  # unscored ones stay due and are retried next run
    return results
