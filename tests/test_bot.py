import asyncio
from datetime import date, datetime

from dartboard import bot
from dartboard.backup import Verdict
from dartboard.bot import backup_text, daily_check_text, is_market_window, poll_start
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


def test_daily_check_reports_the_calendar_cross_check():
    head = "cd" * 32
    assert "지수 일봉과 휴장일 API 일치" in daily_check_text(date(2026, 11, 12), None, 1, head, None, [])
    warn = daily_check_text(date(2026, 11, 12), None, 1, head, None, [date(2026, 11, 10)])
    assert "경고: 지수 일봉과 휴장일 API가 다른 날 2026-11-10" in warn
    assert "거래일 대조" not in daily_check_text(date(2026, 11, 12), None, 1, head, None, None)


def test_backup_message_says_whether_the_restore_check_passed_and_where_the_file_is():
    ok = backup_text("b.tar.gz", 3 * 1024 ** 2, Verdict(True, 5, "h"), uploaded=True)
    assert ok == "백업 b.tar.gz · 3.0 MiB · 복원 검사 통과 · 이 메시지에 첨부"
    bad = backup_text("b.tar.gz", 30 * 1024 ** 2, Verdict(False, problems=["hash chain breaks at entry 4"]), False)
    assert "경고: 복원 검사 실패 (hash chain breaks at entry 4)" in bad and "20 MiB" in bad


def test_a_failing_daily_step_is_reported_and_does_not_stop_the_loop(monkeypatch):
    pings = []
    monkeypatch.setattr(bot, "ping", lambda url, ok=True: pings.append((url, ok)))

    async def broken():
        raise RuntimeError("Discord 503")

    async def fine():
        return None

    assert asyncio.run(bot.guarded("daily check", broken, "https://hc")) is False
    assert pings == [("https://hc", False)]  # the monitor alerts the owner
    assert asyncio.run(bot.guarded("daily check", fine, "https://hc")) is True and len(pings) == 1
