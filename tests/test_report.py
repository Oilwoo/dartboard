from datetime import datetime, timedelta, timezone

import pytest

from dartboard.ledger import Judgment, Ledger, Outcome
from dartboard.report import (
    BASE_RATE_MIN,
    MIN_OFFICIAL,
    Row,
    Scorecard,
    _pct,
    _verdict,
    brier,
    format_scorecard,
    interval,
    official_rows,
    rank_ic,
    scorecard,
    skill,
)


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=self.n)).isoformat()


def judge(led, i, p=0.6, filing_type="B001", model="local", kind="new", faithful=True, status="forecast"):
    return led.record_judgment(Judgment(
        rcept_no=f"20260105{i:06d}", rcept_dt="20260105", corp_code="c", corp_name="회사", ticker="005930",
        market="Y", filing_type=filing_type, report_nm="공시", base_title="공시", kind=kind, route="universe",
        status=status, p_up=p if status == "forecast" else None, summary="", model=model, prompt_version="v1",
        faith_checked=1, faith_supported=1 if faithful else 0))


def score(led, seq, up, day="2026-01-06", horizon=5, on_time=True, status="scored"):
    prices = dict(stock_open=100.0, stock_close=110.0 if up else 90.0, index_open=100.0, index_close=100.0)
    led.record_outcome(Outcome(judgment_seq=seq, horizon=horizon, status=status, entry_day=day, exit_day=day,
                               on_time=on_time, **(prices if status == "scored" else {})))


def test_brier_and_skill_against_always_fifty_percent():
    led = Ledger(clock=Clock())
    score(led, judge(led, 1, p=0.8), up=True)
    score(led, judge(led, 2, p=0.6), up=False)
    sc = scorecard(led)
    assert brier(sc.official) == pytest.approx((0.2 ** 2 + 0.6 ** 2) / 2)
    assert sc.skill_half == pytest.approx(1 - 0.2 / 0.25)


def test_base_rate_uses_only_outcomes_recorded_before_the_judgment():
    led = Ledger(clock=Clock())
    early = [judge(led, i) for i in range(BASE_RATE_MIN)]
    other_type = judge(led, 900, filing_type="I002")
    for seq in early:
        score(led, seq, up=True)
    later = judge(led, 999)
    score(led, later, up=False)
    score(led, other_type, up=False)
    rows, _, _ = official_rows(led)
    assert rows[0].base == 0.5                    # judged before any outcome existed
    assert rows[BASE_RATE_MIN].base == 1.0        # 30 earlier outcomes, all up
    assert rows[-1].base == 0.5                   # another filing type has no history of its own


def test_follow_ups_late_entries_halts_and_failures_are_counted_but_not_scored():
    led = Ledger(clock=Clock())
    score(led, judge(led, 1), up=True)
    score(led, judge(led, 2, kind="follow_up"), up=True)
    score(led, judge(led, 3), up=True, on_time=False)
    score(led, judge(led, 4), up=True, status="unscorable")
    score(led, judge(led, 5), up=True, horizon=20)
    judge(led, 6, status="failed")
    sc = scorecard(led)
    assert (len(sc.official), sc.late, sc.unscorable, sc.follow_ups, sc.failed, sc.judged) == (1, 1, 1, 1, 1, 6)


def many(led, n, right):
    """n official forecasts over 20 entry days; `right` decides whether p points the right way."""
    for i in range(n):
        up = i % 2 == 0
        p = (0.8 if up else 0.2) if right(i) else (0.2 if up else 0.8)
        score(led, judge(led, i, p=p), up=up, day=f"2026-02-{i % 20 + 1:02d}")


def test_no_claim_before_one_hundred_official_outcomes():
    led = Ledger(clock=Clock())
    many(led, 40, right=lambda i: True)
    assert "참고용" in format_scorecard(scorecard(led))


def test_a_clearly_better_forecaster_beats_the_baseline_after_one_hundred():
    led = Ledger(clock=Clock())
    many(led, 120, right=lambda i: i % 10 != 0)  # right nine times in ten
    sc = scorecard(led)
    assert sc.ci_half[0] > 0
    line = next(x for x in format_scorecard(sc).splitlines() if x.startswith("기준선 '늘 50%'"))
    assert line.endswith("→ 기준선보다 낫다")


def test_a_coin_flipper_cannot_claim_skill():
    led = Ledger(clock=Clock())
    many(led, 120, right=lambda i: (i // 2) % 2 == 0)  # right half the time
    text = format_scorecard(scorecard(led))
    assert "기준선보다 낫다고 말할 수 없다" in text


def test_interval_needs_at_least_two_days_and_is_reproducible():
    led = Ledger(clock=Clock())
    many(led, 30, right=lambda i: i % 3 != 0)
    rows, _, _ = official_rows(led)
    assert interval(rows, "half") == interval(rows, "half")
    assert interval([r for r in rows if r.day == rows[0].day], "half") is None


def test_report_splits_by_number_check_and_model_and_shows_cost():
    led = Ledger(clock=Clock())
    score(led, judge(led, 1, p=0.7, model="local"), up=True)
    score(led, judge(led, 2, p=0.7, model="gemini", faithful=False), up=False)
    text = format_scorecard(scorecard(led))
    assert "숫자 검사 통과 1건" in text and "숫자 검사 실패 1건" in text
    assert "gemini 1건" in text and "local 1건" in text
    assert "왕복 비용 0.23%" in text


def test_empty_ledger():
    text = format_scorecard(scorecard(Ledger()))
    assert text.endswith("채점 불가(거래정지 등) 0\n아직 공식 채점 결과가 없습니다.")


def test_formatted_lines_exactly():
    rows = [Row(0.6, 1, 0.5, "2026-01-06", 0.1, "local", True, None),
            Row(0.4, 0, 0.5, "2026-01-07", -0.1, "gemini", True, None)]
    sc = Scorecard(judged=2, failed=0, follow_ups=0, official=rows, late=0, unscorable=0,
                   skill_half=0.2, skill_base=None, ci_half=(-0.1, 0.25), ci_base=None)
    lines = format_scorecard(sc).splitlines()
    assert lines[2] == "기준선 '늘 50%' 대비 개선 +20.0% [95% -10.0% ~ +25.0%] → 참고용 (공식 판정은 100건부터)"
    assert lines[3] == "기준선 '기저율' 대비 개선 - → 참고용 (공식 판정은 100건부터)"
    assert lines[4] == "숫자 검사 통과 2건 Brier 0.160"
    assert lines[5] == "모델별: gemini 1건 0.160 · local 1건 0.160"
    assert not any(x.startswith("순위 상관") for x in lines)  # IC needs at least three outcomes


def test_unknown_baseline_is_an_error():
    with pytest.raises(ValueError):
        skill([Row(0.6, 1, 0.5, "d", 0.1, "m", True, None)], "median")


def scored_at(led, seq, stock_close, day="2026-01-06"):
    led.record_outcome(Outcome(judgment_seq=seq, horizon=5, status="scored", entry_day=day, exit_day=day,
                               on_time=True, stock_open=100.0, stock_close=stock_close,
                               index_open=100.0, index_close=100.0))


def test_the_two_baselines_differ_once_a_base_rate_exists():
    led = Ledger(clock=Clock())
    history = [judge(led, i) for i in range(BASE_RATE_MIN)]
    for i, seq in enumerate(history):
        scored_at(led, seq, 110.0 if i < 24 else 90.0, day=f"2026-01-{i % 9 + 1:02d}")  # base rate 80%
    later = [judge(led, 100 + i, p=0.7) for i in range(4)]
    for i, seq in enumerate(later):
        scored_at(led, seq, 110.0 if i else 90.0, day=f"2026-02-{i + 1:02d}")
    sc = scorecard(led)
    assert sc.official[-1].base == pytest.approx(0.8)
    new = sc.official[BASE_RATE_MIN:]
    assert brier(new) == pytest.approx((0.7 ** 2 + 3 * 0.3 ** 2) / 4)
    half = 1 - sum((r.p - r.y) ** 2 for r in sc.official) / sum((0.5 - r.y) ** 2 for r in sc.official)
    base = 1 - sum((r.p - r.y) ** 2 for r in sc.official) / sum((r.base - r.y) ** 2 for r in sc.official)
    assert sc.skill_half == pytest.approx(half) and sc.skill_base == pytest.approx(base) and half != base
    assert sc.ci_half != sc.ci_base and sc.ci_base is not None


def test_another_filing_type_judged_later_does_not_borrow_the_base_rate():
    led = Ledger(clock=Clock())
    for i in range(BASE_RATE_MIN):
        score(led, judge(led, i), up=True)
    score(led, judge(led, 500, filing_type="I002"), up=True)
    assert official_rows(led)[0][-1].base == 0.5


def test_a_tie_with_the_benchmark_is_not_a_win():
    led = Ledger(clock=Clock())
    scored_at(led, judge(led, 1), 100.0)
    assert official_rows(led)[0][0].y == 0


def test_counts_keep_going_after_late_and_halted_outcomes():
    led = Ledger(clock=Clock())
    for i in range(2):
        score(led, judge(led, 10 + i), up=True, status="unscorable")
        score(led, judge(led, 20 + i), up=True, on_time=False)
    score(led, judge(led, 30), up=True)
    sc = scorecard(led)
    assert (sc.unscorable, sc.late, len(sc.official)) == (2, 2, 1)


def test_report_lines_show_exact_numbers():
    led = Ledger(clock=Clock())
    seqs = [judge(led, 1, p=0.9), judge(led, 2, p=0.9), judge(led, 3, p=0.5, faithful=False)]
    scored_at(led, seqs[0], 110.0, "2026-01-06")
    scored_at(led, seqs[1], 100.0, "2026-01-07")
    scored_at(led, seqs[2], 90.0, "2026-01-07")
    led.record_review(seqs[2], "discard")
    text = format_scorecard(scorecard(led))
    assert "숫자 검사 통과 2건" in text and "숫자 검사 실패 1건" in text and "사람이 거절 1건" in text
    # p>50% bought: +10% and 0% -> mean +5.0%, after 0.23% costs +4.8%; the p=50% one is not bought
    assert "p>50%인 것만 샀다면 2건, 평균 초과수익 +5.0%, 왕복 비용 0.23% 빼면 +4.8%" in text
    assert "[95% " in text and "Brier 0.357" in text  # (0.1² + 0.9² + 0.5²) / 3: the tie counts as a loss


def test_a_base_rate_that_is_always_right_has_no_skill_ratio():
    led = Ledger(clock=Clock())
    for i in range(BASE_RATE_MIN):
        score(led, judge(led, i), up=True, day=f"2026-01-{i % 9 + 1:02d}")
    score(led, judge(led, 99, p=0.9), up=True, day="2026-02-02")
    only_late = [r for r in official_rows(led)[0] if r.base == 1.0]
    assert skill(only_late, "base") is None and skill(only_late, "half") == pytest.approx(1 - 0.01 / 0.25)


def test_verdict_boundaries():
    assert _verdict(MIN_OFFICIAL - 1, (0.1, 0.2)).startswith("참고용")
    assert _verdict(MIN_OFFICIAL, (0.01, 0.2)) == "기준선보다 낫다"
    assert _verdict(MIN_OFFICIAL, (0.0, 0.2)) == "기준선보다 낫다고 말할 수 없다"
    assert _verdict(MIN_OFFICIAL, None) == "기준선보다 낫다고 말할 수 없다"
    assert _pct(0.2) == "+20.0%" and _pct(-0.04) == "-4.0%"


def test_two_days_are_enough_for_an_interval():
    led = Ledger(clock=Clock())
    score(led, judge(led, 1, p=0.8), up=True, day="2026-01-06")
    score(led, judge(led, 2, p=0.3), up=False, day="2026-01-07")
    lo, hi = interval(official_rows(led)[0], "half")
    assert lo <= hi


def test_interval_is_pinned_for_a_fixed_sample():
    # golden values: the seed and percentile positions are part of what a published report means
    rows = [Row(0.7 if i % 3 else 0.4, i % 2, 0.5, f"2026-01-{i % 7 + 1:02d}", 0.0, "m", True, None)
            for i in range(21)]
    assert interval(rows, "half") == pytest.approx((-0.3676190476190475, 0.1657142857142857))


def test_rank_ic_measures_whether_higher_probabilities_go_with_bigger_gains():
    def rows(pairs):
        return [Row(p, int(x > 0), 0.5, "d", x, "m", True, None) for p, x in pairs]

    assert rank_ic(rows([(0.2, -0.05), (0.5, 0.0), (0.8, 0.07)])) == pytest.approx(1.0)
    assert rank_ic(rows([(0.2, 0.05), (0.5, 0.0), (0.8, -0.07)])) == pytest.approx(-1.0)
    assert rank_ic(rows([(0.6, 0.01), (0.6, 0.02), (0.6, 0.03)])) is None   # all the same p: undefined
    assert rank_ic(rows([(0.6, 0.01), (0.7, 0.02)])) is None                 # too few
    # ties share their average rank: p ranks 1.5, 1.5, 3 against excess ranks 1, 2, 3
    assert rank_ic(rows([(0.4, -0.01), (0.4, 0.0), (0.9, 0.02)])) == pytest.approx(0.8660254)
    assert rank_ic(rows([(0.4, 0.0), (0.9, 0.02), (0.9, -0.01)])) == pytest.approx(0.0)  # tie at the top
