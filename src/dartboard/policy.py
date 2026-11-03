"""Policy engine: decides whether an action may run automatically, needs a human, or is refused.

The LLM never decides this. Every money-moving action passes through `PolicyEngine.decide()`,
so the limits hold no matter which model (local, uncensored, cloud) proposed the action.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

import yaml


class Decision(str, Enum):
    AUTO = "auto"            # run now, inside pre-approved limits
    NEEDS_APPROVAL = "approval"  # ask on Discord with [Approve] / [Reject] buttons
    DENY = "deny"            # never run


@dataclass(frozen=True)
class Action:
    event_id: str    # unique per detected event; used to block duplicates
    target: str      # e.g. "upbit:order"
    amount: int      # in KRW


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
    def from_file(cls, path: str, db_path: str) -> "PolicyEngine":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f), db_path)

    # --- kill switch (Discord /stop and /resume) ---------------------------------------
    def halt(self, on: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO flags VALUES ('halted', ?)", (int(on),))
        self.db.commit()

    def halted(self) -> bool:
        row = self.db.execute("SELECT value FROM flags WHERE name='halted'").fetchone()
        return bool(row and row[0])

    # --- the decision ------------------------------------------------------------------
    def decide(self, a: Action) -> Verdict:
        dry = bool(self.policy.get("dry_run", True))  # safe default: simulate only
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
        day_spent = self._sum(a.target, now - 86_400)
        hour_count = self._count(a.target, now - 3_600)
        if a.amount > rule["max_amount_per_order"]:
            return Verdict(Decision.NEEDS_APPROVAL, "over per-order limit", dry)
        if day_spent + a.amount > rule["max_daily_spend"]:
            return Verdict(Decision.NEEDS_APPROVAL, "would exceed daily spend limit", dry)
        if hour_count >= rule["max_orders_per_hour"]:
            return Verdict(Decision.NEEDS_APPROVAL, "hourly order limit reached", dry)
        return Verdict(Decision.AUTO, "within pre-approved limits", dry)

    def record_executed(self, a: Action) -> None:
        """Call after the action really ran (auto or human-approved)."""
        now = self.clock()
        self.db.execute("INSERT INTO seen VALUES (?, ?)", (a.event_id, now))
        self.db.execute("INSERT INTO spend VALUES (?, ?, ?)", (a.target, a.amount, now))
        self.db.commit()

    def record_rejected(self, a: Action) -> None:
        """Rejected or timed-out approvals are also 'handled', so they are not re-asked."""
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (a.event_id, self.clock()))
        self.db.commit()

    def _sum(self, target: str, since: float) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM spend WHERE target=? AND at>=?", (target, since)
        ).fetchone()[0]

    def _count(self, target: str, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM spend WHERE target=? AND at>=?", (target, since)
        ).fetchone()[0]
