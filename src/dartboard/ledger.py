"""Forecast ledger ("예측 성적표"): every opinion is stamped when it is made and scored later.

* Append-only: SQLite triggers refuse UPDATE and DELETE, so a result cannot be edited after the fact.
* Hash chain: each row stores the hash of the previous row, so tampering with the file is detectable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

STANCES = ("buy", "sell", "hold")

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  ticker TEXT NOT NULL,
  market TEXT NOT NULL,
  stance TEXT NOT NULL CHECK (stance IN ('buy','sell','hold')),
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  horizon_days INTEGER NOT NULL CHECK (horizon_days > 0),
  rationale TEXT NOT NULL,
  sources TEXT NOT NULL,
  model TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
  forecast_id INTEGER PRIMARY KEY REFERENCES forecasts(id),
  scored_at TEXT NOT NULL,
  stock_return REAL NOT NULL,
  bench_return REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
  BEGIN SELECT RAISE(ABORT, 'forecasts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
  BEGIN SELECT RAISE(ABORT, 'forecasts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes
  BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
  BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END;
"""

FIELDS = ("created_at", "ticker", "market", "stance", "confidence", "horizon_days", "rationale", "sources", "model")


def _hash(prev_hash: str, record: dict) -> str:
    body = json.dumps({k: record[k] for k in FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Report:
    scored: int
    hit_rate: float | None
    brier: float | None  # lower is better; 0.25 = coin-flip confidence
    mean_excess_buy: float | None
    mean_excess_sell: float | None


class Ledger:
    def __init__(self, path: str = ":memory:", hold_band: float = 0.02, clock=None):
        # LangGraph runs sync nodes in worker threads, so the connection is shared behind a lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        self.hold_band = hold_band  # |excess return| within this band counts as a correct "hold"
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def append(self, ticker: str, stance: str, confidence: float, horizon_days: int,
               rationale: str, sources: list[str], model: str, market: str = "Y") -> int:
        """market: OpenDART corp_cls ('Y' KOSPI, 'K' KOSDAQ, ...) - picks the benchmark index when scoring."""
        if stance not in STANCES:
            raise ValueError(f"stance must be one of {STANCES}")
        with self.lock:  # read-prev-hash and insert must not interleave
            row = self.db.execute("SELECT hash FROM forecasts ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else "genesis"
            rec = {"created_at": self.clock(), "ticker": ticker, "market": market, "stance": stance,
                   "confidence": float(confidence),
                   "horizon_days": int(horizon_days), "rationale": rationale,
                   "sources": json.dumps(sources, ensure_ascii=False), "model": model}
            cur = self.db.execute(
                f"INSERT INTO forecasts ({', '.join(FIELDS)}, prev_hash, hash) "
                f"VALUES ({', '.join('?' * (len(FIELDS) + 2))})",
                (*(rec[k] for k in FIELDS), prev, _hash(prev, rec)),
            )
            self.db.commit()
            return cur.lastrowid

    def verify_chain(self) -> int | None:
        """Returns None when the chain is intact, otherwise the id of the first bad row."""
        prev = "genesis"
        for row in self.db.execute(f"SELECT id, {', '.join(FIELDS)}, prev_hash, hash FROM forecasts ORDER BY id"):
            rec = dict(zip(FIELDS, row[1:1 + len(FIELDS)]))
            if row[-2] != prev or row[-1] != _hash(prev, rec):
                return row[0]
            prev = row[-1]
        return None

    def due(self, today_iso: str) -> list[tuple[int, str, str, str, int]]:
        """Unscored forecasts whose horizon has passed: (id, ticker, market, created_at, horizon_days)."""
        return self.db.execute(
            """SELECT f.id, f.ticker, f.market, f.created_at, f.horizon_days
               FROM forecasts f LEFT JOIN outcomes o ON o.forecast_id = f.id
               WHERE o.forecast_id IS NULL AND date(f.created_at, '+' || f.horizon_days || ' days') <= date(?)""",
            (today_iso,),
        ).fetchall()

    def score(self, forecast_id: int, stock_return: float, bench_return: float) -> None:
        """Returns are measured by the caller from adjusted prices over the forecast horizon."""
        with self.lock:
            self.db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?)",
                            (forecast_id, self.clock(), float(stock_return), float(bench_return)))
            self.db.commit()

    def report(self) -> Report:
        rows = self.db.execute(
            """SELECT f.stance, f.confidence, o.stock_return - o.bench_return
               FROM forecasts f JOIN outcomes o ON o.forecast_id = f.id"""
        ).fetchall()
        if not rows:
            return Report(0, None, None, None, None)
        hits, brier = [], []
        for stance, conf, excess in rows:
            hit = excess > 0 if stance == "buy" else excess < 0 if stance == "sell" else abs(excess) <= self.hold_band
            hits.append(hit)
            brier.append((conf - (1.0 if hit else 0.0)) ** 2)

        def mean(xs):
            return sum(xs) / len(xs) if xs else None

        return Report(
            scored=len(rows),
            hit_rate=mean([1.0 if h else 0.0 for h in hits]),
            brier=mean(brier),
            mean_excess_buy=mean([e for s, _, e in rows if s == "buy"]),
            mean_excess_sell=mean([e for s, _, e in rows if s == "sell"]),
        )
