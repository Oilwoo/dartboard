from datetime import date, datetime

from dartboard.bot import daily_check_text, is_market_window, poll_start
from dartboard.scoring import ScoreRun


def test_daily_check_posts_the_full_head_hash_and_warns_on_a_broken_chain():
    head = "ab" * 32
    text = daily_check_text(date(2026, 11, 12), None, 7, head, ScoreRun(3, 10, 1))
    assert "해시 체인 이상 없음" in text and f"`{head}`" in text and "기록 7개" in text
    assert "채점 기록 3건" in text and "주가 조회 실패 1건" in text
    assert "경고: 5번 기록부터" in daily_check_text(date(2026, 11, 12), 5, 7, head, None)


def test_polls_only_on_weekdays_between_7_and_21():
    assert is_market_window(datetime(2026, 11, 3, 9, 0))       # Tuesday
    assert not is_market_window(datetime(2026, 11, 3, 21, 0))
    assert not is_market_window(datetime(2026, 11, 7, 10, 0))  # Saturday


def test_every_poll_sweeps_the_last_three_days():
    # a backlog left while the home PC was off is judged in batches over several polls
    assert poll_start(date(2026, 11, 3)) == date(2026, 10, 31)
