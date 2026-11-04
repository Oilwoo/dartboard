"""Decides whether a money-moving action runs automatically, needs a human, or is refused.

The LLM never makes this call, so the limits hold whichever model proposed the action.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

import yaml


class Decision(str, Enum):
    AUTO = "auto"
    NEEDS_APPROVAL = "approval"
    DENY = "deny"


@dataclass(frozen=True)
class Action:
    event_id: str  # one per detected event, so the same event never runs twice
    target: str    # e.g. "kis:order"
    amount: int    # KRW


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str
    dry_run: bool


class PolicyEngine:
    def __init__(self, policy: dict, db_path: str = ":memory:", clock=time.time):
        self.policy = policy
        self.clock = clock
        self.db = sqlite3.connect(db_path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (event_id TEXT PRIMARY KEY, at REAL);
            CREATE TABLE IF NOT EXISTS spend (target TEXT, amount INTEGER, at REAL);
            CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value INTEGER);
            """
        )

    @classmethod
    def from_file(cls, path: str, db_path: str) -> PolicyEngine:
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f), db_path)

    def halt(self, on: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO flags VALUES ('halted', ?)", (int(on),))
        self.db.commit()

    def halted(self) -> bool:
        row = self.db.execute("SELECT value FROM flags WHERE name='halted'").fetchone()
        return bool(row and row[0])

    def decide(self, a: Action) -> Verdict:
        dry = bool(self.policy.get("dry_run", True))  # a missing setting means simulate only
        if self.halted():
            return Verdict(Decision.DENY, "kill switch is on", dry)
        if self.db.execute("SELECT 1 FROM seen WHERE event_id=?", (a.event_id,)).fetchone():
            return Verdict(Decision.DENY, "duplicate event (already handled)", dry)
        if a.amount <= 0:
            return Verdict(Decision.DENY, "amount must be positive", dry)

        rule = self.policy.get("auto_approve", {}).get(a.target)
        if rule is None:
            return Verdict(Decision.NEEDS_APPROVAL, "no auto-approve rule for this target", dry)

        now = self.clock()
        if a.amount > rule["max_amount_per_order"]:
            return Verdict(Decision.NEEDS_APPROVAL, "over per-order limit", dry)
        if self._sum(a.target, now - 86_400) + a.amount > rule["max_daily_spend"]:
            return Verdict(Decision.NEEDS_APPROVAL, "would exceed daily spend limit", dry)
        if self._count(a.target, now - 3_600) >= rule["max_orders_per_hour"]:
            return Verdict(Decision.NEEDS_APPROVAL, "hourly order limit reached", dry)
        return Verdict(Decision.AUTO, "within pre-approved limits", dry)

    def record_executed(self, a: Action) -> None:
        """Call after the action really ran, automatically or with approval."""
        now = self.clock()
        self.db.execute("INSERT INTO seen VALUES (?, ?)", (a.event_id, now))
        self.db.execute("INSERT INTO spend VALUES (?, ?, ?)", (a.target, a.amount, now))
        self.db.commit()

    def record_rejected(self, a: Action) -> None:
        """A rejected or unanswered event also counts as handled, so it is not asked again."""
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (a.event_id, self.clock()))
        self.db.commit()

    def _sum(self, target: str, since: float) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM spend WHERE target=? AND at>=?", (target, since)
        ).fetchone()[0]

    def _count(self, target: str, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM spend WHERE target=? AND at>=?", (target, since)
        ).fetchone()[0]
