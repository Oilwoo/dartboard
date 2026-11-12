"""Ledger ("성적표"): every judgment, human review and outcome in one append-only, hash-chained log.

Each entry has a key (one judgment per filing, one review per judgment, one outcome per judgment and
horizon), so a step that runs again after a crash finds its entry instead of writing a second one.
Triggers refuse UPDATE and DELETE; the hash chain makes an edit made around the app detectable.
That is tamper-evident, not tamper-proof: the daily head hash posted outside the server is the check.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('judgment', 'review', 'outcome')),
  key TEXT NOT NULL UNIQUE,
  recorded_at TEXT NOT NULL,
  body TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
  BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
  BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS meta_no_update BEFORE UPDATE ON meta
  BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS meta_no_delete BEFORE DELETE ON meta
  BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
"""

GENESIS = "genesis"


@dataclass(frozen=True)
class Judgment:
    """What the AI made of one filing. A failed judgment has no probability and still counts."""
    rcept_no: str
    rcept_dt: str            # receipt date YYYYMMDD; OpenDART gives no time of day
    corp_code: str
    corp_name: str
    ticker: str
    market: str              # Y KOSPI, K KOSDAQ: picks the benchmark index
    filing_type: str         # B001, I001 or I002
    report_nm: str
    base_title: str          # report_nm without correction prefixes, for linking follow-ups
    kind: str                # new / follow_up
    route: str               # watchlist (alerts, human review) / universe (recorded only)
    status: str              # forecast / failed
    p_up: float | None       # probability of beating the benchmark over 5 trading sessions
    summary: str
    model: str               # the model that actually answered
    prompt_version: str
    faith_checked: int
    faith_supported: int
    unsupported: list[str] = field(default_factory=list)
    linked_rcept_no: str | None = None  # the earlier filing a follow-up corrects, when it could be found

    def __post_init__(self):
        if self.kind not in ("new", "follow_up"):
            raise ValueError(f"kind must be new or follow_up, not {self.kind!r}")
        if self.route not in ("watchlist", "universe"):
            raise ValueError(f"route must be watchlist or universe, not {self.route!r}")
        if self.market not in ("Y", "K"):
            raise ValueError(f"market must be Y or K, not {self.market!r}")
        if self.status == "forecast":
            if not isinstance(self.p_up, (int, float)) or not 0 <= self.p_up <= 1:
                raise ValueError(f"a forecast needs a probability between 0 and 1, not {self.p_up!r}")
        elif self.status == "failed":
            if self.p_up is not None:
                raise ValueError("a failed judgment has no probability")
        else:
            raise ValueError(f"status must be forecast or failed, not {self.status!r}")


@dataclass(frozen=True)
class Outcome:
    """Prices for one forecast over one horizon, or why it could not be scored."""
    judgment_seq: int
    horizon: int             # trading sessions, the entry session counted as 1
    status: str              # scored / unscorable
    entry_day: str           # ISO date of the entry session
    exit_day: str
    on_time: bool            # entry no later than the first session after the receipt date
    stock_open: float | None = None
    stock_close: float | None = None
    index_open: float | None = None
    index_close: float | None = None
    reason: str = ""

    def __post_init__(self):
        prices = (self.stock_open, self.stock_close, self.index_open, self.index_close)
        if self.status == "scored":
            if not all(isinstance(x, (int, float)) and x > 0 for x in prices):
                raise ValueError("a scored outcome needs four positive prices")
        elif self.status != "unscorable":
            raise ValueError(f"status must be scored or unscorable, not {self.status!r}")
        if self.horizon < 1:
            raise ValueError("horizon must be at least one session")

    @property
    def stock_return(self) -> float:
        return self.stock_close / self.stock_open - 1

    @property
    def bench_return(self) -> float:
        return self.index_close / self.index_open - 1

    @property
    def excess(self) -> float:
        return self.stock_return - self.bench_return


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    key: str
    recorded_at: str  # UTC ISO time the ledger stamped
    body: dict


def _canonical(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev: str, kind: str, key: str, recorded_at: str, body: str) -> str:
    return hashlib.sha256("\n".join((prev, kind, key, recorded_at, body)).encode("utf-8")).hexdigest()


class OldLedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: str = ":memory:", clock=None):
        # LangGraph runs sync nodes in worker threads, so one connection is shared behind a lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "forecasts" in tables and "entries" not in tables:
            # never convert or drop the stage-1 records: the official ledger starts in a new file
            raise OldLedgerError(f"{path} is a stage-1 ledger; move it aside and start a new file")
        self.db.executescript(SCHEMA)
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('created_at', ?)", (self.clock(),))
            self.db.commit()

    # --- writing -------------------------------------------------------------------------------

    def _append(self, kind: str, key: str, body: dict) -> int:
        """The entry's seq. If the key is already there, nothing is written and its seq is returned."""
        text = _canonical(body)
        with self.lock:  # reading the last hash and inserting must not interleave
            found = self.db.execute("SELECT seq, body FROM entries WHERE key=?", (key,)).fetchone()
            if found:
                if found[1] != text:
                    raise ValueError(f"{key} is already recorded with different content")
                return found[0]
            row = self.db.execute("SELECT hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            prev, at = (row[0] if row else GENESIS), self.clock()
            cur = self.db.execute(
                "INSERT INTO entries (kind, key, recorded_at, body, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, key, at, text, prev, _hash(prev, kind, key, at, text)))
            self.db.commit()
            return cur.lastrowid

    def record_judgment(self, j: Judgment) -> int:
        return self._append("judgment", f"judgment:{j.rcept_no}", asdict(j))

    def record_review(self, judgment_seq: int, decision: str) -> int:
        if decision not in ("keep", "discard"):
            raise ValueError("decision must be keep or discard")
        self._judgment_entry(judgment_seq)
        return self._append("review", f"review:{judgment_seq}", {"judgment_seq": judgment_seq, "decision": decision})

    def record_outcome(self, o: Outcome) -> int:
        j = self.judgment(o.judgment_seq)
        if j.status != "forecast":
            raise ValueError("only a forecast can have an outcome")
        return self._append("outcome", f"outcome:{o.judgment_seq}:{o.horizon}", asdict(o))

    # --- reading -------------------------------------------------------------------------------

    def entries(self, kind: str | None = None) -> list[Entry]:
        sql, args = "SELECT seq, kind, key, recorded_at, body FROM entries", ()
        if kind:
            sql, args = sql + " WHERE kind=?", (kind,)
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [Entry(s, k, key, at, json.loads(b)) for s, k, key, at, b in rows]

    def _judgment_entry(self, seq: int) -> Entry:
        with self.lock:
            row = self.db.execute("SELECT seq, kind, key, recorded_at, body FROM entries WHERE seq=? AND kind='judgment'",
                                  (seq,)).fetchone()
        if row is None:
            raise KeyError(f"no judgment with seq {seq}")
        return Entry(row[0], row[1], row[2], row[3], json.loads(row[4]))

    def judgment(self, seq: int) -> Judgment:
        return Judgment(**self._judgment_entry(seq).body)

    def judgments(self) -> list[tuple[Entry, Judgment]]:
        return [(e, Judgment(**e.body)) for e in self.entries("judgment")]

    def reviews(self) -> dict[int, str]:
        return {e.body["judgment_seq"]: e.body["decision"] for e in self.entries("review")}

    def outcomes(self) -> list[tuple[Entry, Outcome]]:
        return [(e, Outcome(**e.body)) for e in self.entries("outcome")]

    def unscored(self, horizons: tuple[int, ...]) -> list[tuple[Entry, Judgment, list[int]]]:
        """Forecasts still missing an outcome for some horizon, with the horizons they miss."""
        done: dict[int, set[int]] = {}
        for _, o in self.outcomes():
            done.setdefault(o.judgment_seq, set()).add(o.horizon)
        out = []
        for e, j in self.judgments():
            missing = [h for h in horizons if h not in done.get(e.seq, set())]
            if j.status == "forecast" and missing:
                out.append((e, j, missing))
        return out

    def find_original(self, corp_code: str, base_title: str, before_rcept_no: str) -> str | None:
        """The latest earlier filing of this company with the same title, if the ledger has one."""
        with self.lock:
            row = self.db.execute(
                """SELECT json_extract(body, '$.rcept_no') FROM entries
                   WHERE kind='judgment' AND json_extract(body, '$.corp_code')=?
                     AND json_extract(body, '$.base_title')=? AND json_extract(body, '$.rcept_no') < ?
                   ORDER BY json_extract(body, '$.rcept_no') DESC LIMIT 1""",
                (corp_code, base_title, before_rcept_no)).fetchone()
        return row[0] if row else None

    # --- checking ------------------------------------------------------------------------------

    def verify_chain(self) -> int | None:
        """None when the chain is intact, otherwise the seq of the first bad entry."""
        prev = GENESIS
        with self.lock:
            rows = self.db.execute(
                "SELECT seq, kind, key, recorded_at, body, prev_hash, hash FROM entries ORDER BY seq").fetchall()
        for seq, kind, key, at, body, prev_hash, h in rows:
            if prev_hash != prev or h != _hash(prev, kind, key, at, body):
                return seq
            prev = h
        return None

    def head(self) -> tuple[int, str]:
        """(number of entries, hash of the last one): post it outside the server to anchor the chain."""
        with self.lock:
            count = self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            row = self.db.execute("SELECT hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return count, row[0] if row else GENESIS
