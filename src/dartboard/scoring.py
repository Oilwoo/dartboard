"""Scores due forecasts: the stock's return minus its market index's return over the same window.

Entry is the open of the first trading day after the forecast date (KST): an opinion made during
market hours must not be scored with prices it may already have seen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .ledger import Ledger
from .prices import BENCHMARK, KST, Bar

MAX_GAP_DAYS = 10  # a longer gap before the exit date means a halt or delisting: leave it for a human


class PriceSource(Protocol):
    def daily(self, code: str, start: date, end: date, index: bool = False) -> list[Bar]: ...


def window_return(bars: list[Bar], made_on: date, horizon_days: int) -> float | None:
    exit_by = made_on + timedelta(days=horizon_days)
    held = [b for b in bars if made_on < b.day <= exit_by]
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
        results.append(r)  # unscored ones stay due and are tried again on the next run
    return results
