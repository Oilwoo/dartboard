"""The scorecard: how good the official forecasts are, measured against simple baselines.

Brier score = mean of (p - y)^2, where y is 1 when the stock beat its benchmark. Two baselines: always 50%,
and the base rate (the share of earlier official outcomes of the same filing type that beat the benchmark).
Skill = 1 - Brier / baseline Brier; above 0 means better than the baseline. The confidence interval
resamples whole trading days, because filings that enter on the same day move with the same market.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass

from .ledger import Ledger
from .scoring import OFFICIAL_HORIZON, is_official

ROUND_TRIP_COST = 0.0023  # about 0.014% commission each way plus the 0.20% sale tax (2026)
MIN_OFFICIAL = 100        # no claim about skill before this many official outcomes
BASE_RATE_MIN = 30        # the base-rate baseline says 50% until this many earlier outcomes exist
BOOTSTRAP_DRAWS = 2000


@dataclass(frozen=True)
class Row:
    p: float
    y: int              # 1 when the excess return was above zero
    base: float         # the base-rate baseline's forecast for this filing
    day: str            # entry session, the unit the bootstrap resamples
    excess: float
    model: str
    faithful: bool      # every number in the summary was found in the filing
    review: str | None  # keep / discard when a human was asked


def brier(rows: list[Row]) -> float:
    return sum((r.p - r.y) ** 2 for r in rows) / len(rows)


def skill(rows: list[Row], baseline: str) -> float | None:
    if baseline not in ("half", "base"):
        raise ValueError(f"baseline must be half or base, not {baseline!r}")
    ref = sum(((0.5 if baseline == "half" else r.base) - r.y) ** 2 for r in rows)
    if ref == 0:
        return None
    return 1 - sum((r.p - r.y) ** 2 for r in rows) / ref


def interval(rows: list[Row], baseline: str, draws: int = BOOTSTRAP_DRAWS, seed: int = 0) -> tuple[float, float] | None:
    """95% interval of the skill score, resampling entry days with replacement."""
    days: dict[str, list[Row]] = {}
    for r in rows:
        days.setdefault(r.day, []).append(r)
    groups = list(days.values())
    if len(groups) < 2:
        return None
    rng, values = random.Random(seed), []
    for _ in range(draws):
        sample = [r for _ in groups for r in rng.choice(groups)]
        s = skill(sample, baseline)
        if s is not None:
            values.append(s)
    if not values:
        return None
    values.sort()
    return values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]


def _ranks(xs: list[float]) -> list[float]:
    """1-based ranks; tied values share the average of their ranks."""
    order = sorted(range(len(xs)), key=xs.__getitem__)
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def rank_ic(rows: list[Row]) -> float | None:
    """Spearman correlation between p and the excess return: do higher probabilities go with bigger gains?"""
    if len(rows) < 3:
        return None
    a, b = _ranks([r.p for r in rows]), _ranks([r.excess for r in rows])
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return None if va == 0 or vb == 0 else cov / (va * vb) ** 0.5


@dataclass(frozen=True)
class Scorecard:
    judged: int
    failed: int
    follow_ups: int
    official: list[Row]
    late: int                 # scored, but entered after the first session following the receipt date
    unscorable: int           # halted or delisted inside the window
    skill_half: float | None
    skill_base: float | None
    ci_half: tuple[float, float] | None
    ci_base: tuple[float, float] | None


def official_rows(ledger: Ledger) -> tuple[list[Row], int, int]:
    """Official rows in recording order, plus counts of late and unscorable 5-session outcomes."""
    judgments = {e.seq: (e, j) for e, j in ledger.judgments()}
    reviews = ledger.reviews()
    # the base rate may use only outcomes recorded before the judgment, so it never looks ahead
    history: dict[str, tuple[list[str], list[int]]] = {}
    rows, late, unscorable = [], 0, 0
    for oe, o in ledger.outcomes():
        e, j = judgments[o.judgment_seq]
        if o.horizon != OFFICIAL_HORIZON or j.kind != "new":
            continue
        if o.status == "unscorable":
            unscorable += 1
            continue
        if not is_official(j, o):
            late += 1
            continue
        times, ups = history.setdefault(j.filing_type, ([], [0]))
        n = bisect.bisect_left(times, e.recorded_at)
        base = ups[n] / n if n >= BASE_RATE_MIN else 0.5
        y = 1 if o.excess > 0 else 0
        rows.append(Row(j.p_up, y, base, o.entry_day, o.excess, j.model,
                        j.faith_checked == j.faith_supported, reviews.get(e.seq)))
        times.append(oe.recorded_at)
        ups.append(ups[-1] + y)
    return rows, late, unscorable


def scorecard(ledger: Ledger) -> Scorecard:
    judgments = [j for _, j in ledger.judgments()]
    rows, late, unscorable = official_rows(ledger)
    return Scorecard(
        judged=len(judgments),
        failed=sum(j.status == "failed" for j in judgments),
        follow_ups=sum(j.kind == "follow_up" for j in judgments),
        official=rows, late=late, unscorable=unscorable,
        skill_half=skill(rows, "half") if rows else None,
        skill_base=skill(rows, "base") if rows else None,
        ci_half=interval(rows, "half"), ci_base=interval(rows, "base"),
    )


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _verdict(n: int, ci: tuple[float, float] | None) -> str:
    if n < MIN_OFFICIAL:
        return f"참고용 (공식 판정은 {MIN_OFFICIAL}건부터)"
    if ci and ci[0] > 0:
        return "기준선보다 낫다"
    return "기준선보다 낫다고 말할 수 없다"


def format_scorecard(sc: Scorecard) -> str:
    head = (f"판단 {sc.judged}건 (실패 {sc.failed} · 후속 공시 {sc.follow_ups}) · "
            f"공식 채점 {len(sc.official)}건 · 늦은 진입 {sc.late} · 채점 불가(거래정지 등) {sc.unscorable}")
    rows = sc.official
    if not rows:
        return head + "\n아직 공식 채점 결과가 없습니다."
    lines = [head, f"Brier {brier(rows):.3f} (늘 50%라고 답하면 0.250, 낮을수록 좋음)"]
    for name, s, ci in (("늘 50%", sc.skill_half, sc.ci_half), ("기저율", sc.skill_base, sc.ci_base)):
        span = f" [95% {_pct(ci[0])} ~ {_pct(ci[1])}]" if ci else ""
        value = "-" if s is None else _pct(s)
        lines.append(f"기준선 '{name}' 대비 개선 {value}{span} → {_verdict(len(rows), ci)}")
    split = [("숫자 검사 통과", [r for r in rows if r.faithful]), ("숫자 검사 실패", [r for r in rows if not r.faithful]),
             ("사람이 거절", [r for r in rows if r.review == "discard"])]
    lines.append(" · ".join(f"{name} {len(rs)}건 Brier {brier(rs):.3f}" for name, rs in split if rs))
    models: dict[str, list[Row]] = {}
    for r in rows:
        models.setdefault(r.model, []).append(r)
    lines.append("모델별: " + " · ".join(f"{m} {len(rs)}건 {brier(rs):.3f}" for m, rs in sorted(models.items())))
    ic = rank_ic(rows)
    if ic is not None:
        lines.append(f"순위 상관(IC, 확률과 초과수익): {ic:+.2f}")
    bought = [r for r in rows if r.p > 0.5]
    if bought:
        mean = sum(r.excess for r in bought) / len(bought)
        lines.append(f"참고: p>50%인 것만 샀다면 {len(bought)}건, 평균 초과수익 {_pct(mean)}, "
                     f"왕복 비용 {ROUND_TRIP_COST:.2%} 빼면 {_pct(mean - ROUND_TRIP_COST)}")
    return "\n".join(lines)
