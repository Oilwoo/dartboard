"""Scores forecasts with KRX regular-session prices.

Entry is the open of the first session after the judgment was recorded. The opening auction takes orders
until 09:00, so a judgment recorded before 08:50 KST still gets that day's open. Exit is the close of the
horizon-th session, the entry session counted as 1. Trading sessions are the days the benchmark index has a
bar, and only days before today count, so every close used is final.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from .ledger import Judgment, Ledger, Outcome
from .prices import BENCHMARK, KST, Bar

log = logging.getLogger(__name__)

ENTRY_CUTOFF = time(8, 50)
HORIZONS = (1, 5, 20)
OFFICIAL_HORIZON = 5


class PriceSource(Protocol):
    def daily(self, code: str, start: date, end: date, index: bool = False) -> list[Bar]: ...


def to_kst(recorded_at: str) -> datetime:
    return datetime.fromisoformat(recorded_at).astimezone(KST)


def entry_session(sessions: list[date], judged: datetime) -> date | None:
    """The first session whose open comes after the judgment; None if it has not happened yet."""
    for day in sessions:
        if day > judged.date() or (day == judged.date() and judged.time() < ENTRY_CUTOFF):
            return day
    return None


def first_session_after(sessions: list[date], day: date) -> date | None:
    return next((s for s in sessions if s > day), None)


def calendar_mismatches(sessions: list[date], open_days: dict[date, bool], start: date, end: date) -> list[date]:
    """Days in [start, end] where the index calendar and KIS's holiday calendar disagree. A missing index bar
    would silently shift every exit after it, so the daily check reports these."""
    return [d for d, is_open in sorted(open_days.items()) if start <= d <= end and is_open != (d in sessions)]


@dataclass(frozen=True)
class Plan:
    entry: date
    exit: date
    on_time: bool


def plan(sessions: list[date], judged: datetime, rcept_dt: str, horizon: int) -> Plan | None:
    """Entry and exit sessions for one horizon, or None while the exit session is still to come."""
    entry = entry_session(sessions, judged)
    if entry is None:
        return None
    i = sessions.index(entry) + horizon - 1
    if i >= len(sessions):
        return None
    received = datetime.strptime(rcept_dt, "%Y%m%d").date()
    on_time = entry <= (first_session_after(sessions, received) or entry)
    return Plan(entry, sessions[i], on_time)


def measure(seq: int, horizon: int, p: Plan, sessions: list[date], stock: list[Bar], index: list[Bar]) -> Outcome:
    by_day = {b.day: b for b in stock}
    idx = {b.day: b for b in index}
    held = [d for d in sessions if p.entry <= d <= p.exit]
    base = dict(judgment_seq=seq, horizon=horizon, entry_day=p.entry.isoformat(), exit_day=p.exit.isoformat(),
                on_time=p.on_time)
    missing = [d for d in held if d not in by_day]
    if missing:  # a halt or delisting: counted in the report, never filled in with a guess
        return Outcome(status="unscorable", reason=f"no stock price on {missing[0].isoformat()}", **base)
    return Outcome(status="scored", stock_open=by_day[p.entry].open, stock_close=by_day[p.exit].close,
                   index_open=idx[p.entry].open, index_close=idx[p.exit].close, **base)


@dataclass(frozen=True)
class ScoreRun:
    recorded: int   # new outcomes written (scored or unscorable)
    waiting: int    # forecasts with a horizon that has not ended yet
    failed: int     # forecasts whose prices could not be fetched this time; tried again next run


def score_due(ledger: Ledger, prices: PriceSource, today: date) -> ScoreRun:
    pending = ledger.unscored(HORIZONS)
    if not pending:
        return ScoreRun(0, 0, 0)
    last_day = today - timedelta(days=1)
    first_day = min(to_kst(e.recorded_at).date() for e, _, _ in pending)
    index_bars: dict[str, list[Bar]] = {}
    for market in sorted({j.market for _, j, _ in pending}):
        try:
            index_bars[market] = [b for b in prices.daily(BENCHMARK[market], first_day, last_day, index=True)
                                  if b.day < today]
        except Exception:
            log.exception("index prices for market %s failed; its forecasts stay due", market)

    recorded = waiting = failed = 0
    for e, j, horizons in pending:
        if j.market not in index_bars:
            failed += 1
            continue
        sessions = [b.day for b in index_bars[j.market]]
        due = {h: p for h in horizons if (p := plan(sessions, to_kst(e.recorded_at), j.rcept_dt, h))}
        if not due:
            waiting += 1
            continue
        try:
            first = min(p.entry for p in due.values())
            stock = prices.daily(j.ticker, first, max(p.exit for p in due.values()))
        except Exception:
            # one failed price call (rate limit, network) must not stop the other forecasts
            log.exception("prices for judgment %s (%s) failed; it stays due", e.seq, j.ticker)
            failed += 1
            continue
        for h, p in due.items():
            ledger.record_outcome(measure(e.seq, h, p, sessions, stock, index_bars[j.market]))
            recorded += 1
        if len(due) < len(horizons):
            waiting += 1
    return ScoreRun(recorded, waiting, failed)


def is_official(j: Judgment, o: Outcome) -> bool:
    """The one forecast per event that counts: a new filing, scored over 5 sessions, entered on time."""
    return (j.kind == "new" and j.status == "forecast" and o.horizon == OFFICIAL_HORIZON
            and o.status == "scored" and o.on_time)
