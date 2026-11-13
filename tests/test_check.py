from datetime import date
from pathlib import Path

from dartboard import check
from dartboard.dart import Disclosure

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = str(ROOT / "watchlist.example.yaml")
ENV = {"DART_API_KEY": "dart-secret-key-123", "DISCORD_BOT_TOKEN": "discord.secret.token", "DISCORD_CHANNEL_ID": "1",
       "DISCORD_APPROVER_USER_ID": "2", "GROQ_URL": "http://groq", "GROQ_MODEL": "m"}


def row(day, close, change, sign="2", lock="00"):
    return {"stck_bsop_date": day, "stck_clpr": str(close), "prdy_vrss": str(change), "prdy_vrss_sign": sign,
            "flng_cls_code": lock}


def test_daily_close_matches_the_next_days_change_when_it_is_the_regular_close():
    rows = [row("20261103", 1100, 100), row("20261102", 1000, 0, "3"), row("20261104", 1050, 50, "5")]
    assert check.regular_close_consistent(rows) == (2, 2)


def test_a_close_that_includes_after_market_trades_is_caught():
    # the regular session closed at 1000, but the stored close is the 20:00 after-market price 1010
    rows = [row("20261102", 1010, 0, "3"), row("20261103", 1100, 100)]
    assert check.regular_close_consistent(rows) == (1, 0)


def test_ex_rights_days_are_not_compared():
    rows = [row("20261102", 1000, 0, "3"), row("20261103", 500, 0, "3", lock="02")]
    assert check.regular_close_consistent(rows) == (0, 0)


def test_secrets_never_appear_in_the_report():
    text = check.mask("401 for key dart-secret-key-123 and token discord.secret.token", ENV)
    assert "secret" not in text and text.count("***") == 2


def test_missing_settings_stop_the_check_early():
    results = check.run({"DART_API_KEY": "x"}, EXAMPLE)
    assert [(r.name, r.status) for r in results] == [("설정", "fail")]
    assert "DISCORD_BOT_TOKEN" in results[0].detail


def test_settings_say_what_is_optional_and_missing():
    result, cfg = check.check_settings(ENV, EXAMPLE)
    assert result.status == "pass" and "있음: Groq" in result.detail and "하트비트" in result.detail
    assert check.check_settings(dict(ENV, GROQ_URL=""), EXAMPLE)[0].status == "fail"  # no model at all


def test_dart_counts_each_target_type():
    def list_fn(key, bgn, end, filing_type=None):
        return [Disclosure("1", "20261113", "c", "회사", "005930", "공시", "Y", filing_type)] * {"B001": 2}.get(filing_type, 1)

    r = check.check_dart("k", date(2026, 11, 13), list_fn)
    assert r.status == "pass" and "B001 2건 · I001 1건 · I002 1건" in r.detail and "Y 4" in r.detail
    assert check.check_dart("k", date(2026, 11, 13), lambda *a, **k: []).status == "fail"


def test_discord_names_the_bot_channel_and_approver():
    _, cfg = check.check_settings(ENV, EXAMPLE)
    answers = {"/users/@me": {"username": "dartboard"}, "/channels/1": {"name": "alerts"},
               "/users/2": {"username": "owner"}}
    r = check.check_discord(cfg, lambda path, token: answers[path])
    assert r.status == "pass" and "#alerts" in r.detail and "owner" in r.detail


def test_one_broken_service_does_not_hide_the_others(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "check_dart", lambda *a: (_ for _ in ()).throw(RuntimeError("dart-secret-key-123 bad")))
    monkeypatch.setattr(check, "check_discord", lambda cfg: check.Result("Discord", "pass", "ok"))
    monkeypatch.setattr(check, "check_models", lambda cfg: [check.Result("AI groq", "pass", "ok")])
    results = check.run(dict(ENV, DATA_DIR=str(tmp_path)), EXAMPLE, date(2026, 11, 13))
    status = {r.name: r.status for r in results}
    assert status["OpenDART"] == "fail" and status["Discord"] == "pass" and status["한국투자"] == "skip"
    assert all("dart-secret-key-123" not in r.detail for r in results)
    text = check.report(results)
    assert text.splitlines()[-1].startswith("통과 ") and "실패한 항목을 고친 뒤" in text
