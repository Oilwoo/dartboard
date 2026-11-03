from datetime import date, datetime

from dartboard.bot import format_report, is_market_window, poll_start
from dartboard.ledger import Report


def test_report_text():
    assert format_report(Report(0, None, None, None, None)) == "아직 채점된 의견이 없습니다."
    text = format_report(Report(4, 0.75, 0.18, 0.031, None))
    assert "채점 4건" in text and "적중률 75%" in text and "+3.1%p" in text and "매도 의견 평균 -" in text


def test_polls_only_on_weekdays_between_7_and_21():
    assert is_market_window(datetime(2026, 11, 3, 9, 0))       # Tuesday
    assert not is_market_window(datetime(2026, 11, 3, 21, 0))
    assert not is_market_window(datetime(2026, 11, 7, 10, 0))  # Saturday


def test_first_poll_of_the_day_sweeps_the_last_three_days():
    today = date(2026, 11, 3)
    assert poll_start(today, None) == date(2026, 10, 31)              # just started
    assert poll_start(today, date(2026, 11, 2)) == date(2026, 10, 31)  # new day
    assert poll_start(today, today) == today
