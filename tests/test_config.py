from pathlib import Path

import pytest

from dartboard.config import load, load_dotenv, load_watchlist

ROOT = Path(__file__).resolve().parent.parent
ENV = {"DART_API_KEY": "d", "DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "123", "DISCORD_APPROVER_USER_ID": "456"}


def test_dotenv_fills_missing_values_only(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\nDART_API_KEY="abc"\nPOLL_MINUTES=5\nEMPTY=\n\nNOT A SETTING\n', encoding="utf-8")
    env = {"POLL_MINUTES": "15"}
    load_dotenv(str(f), env)
    assert env == {"DART_API_KEY": "abc", "POLL_MINUTES": "15", "EMPTY": ""}


def test_watchlist_codes_stay_exactly_as_written(tmp_path):
    f = tmp_path / "w.yaml"
    f.write_text('stocks:\n  000660: SK하이닉스\n  "005930": 삼성전자\n  0126Z0: 신형코드\n', encoding="utf-8")
    assert load_watchlist(str(f)) == {"000660": "SK하이닉스", "005930": "삼성전자", "0126Z0": "신형코드"}


def test_short_code_is_rejected(tmp_path):
    f = tmp_path / "w.yaml"
    f.write_text("stocks:\n  5930: 삼성전자\n", encoding="utf-8")
    with pytest.raises(ValueError, match="5930"):
        load_watchlist(str(f))


EXAMPLE = str(ROOT / "watchlist.example.yaml")


def test_example_watchlist_loads():
    assert load_watchlist(EXAMPLE)["005930"] == "삼성전자"


def test_settings_from_environment():
    cfg = load(dict(ENV, KIS_APP_KEY="k", POLL_MINUTES="5"), watchlist_path=EXAMPLE)
    assert (cfg.channel_id, cfg.approver_id, cfg.poll_minutes, cfg.kis_key, cfg.kis_env) == (123, 456, 5, "k", "real")
    # the rest of the market goes only to the home PC's model unless UNIVERSE_LLM_ORDER says otherwise
    assert (cfg.llm_order, cfg.universe_llm_order) == ("local,groq,gemini", "local")


def test_missing_settings_are_named():
    with pytest.raises(SystemExit, match="DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID"):
        load({"DART_API_KEY": "d", "DISCORD_APPROVER_USER_ID": "1"}, watchlist_path=EXAMPLE)


def test_missing_watchlist_says_how_to_create_it(tmp_path):
    with pytest.raises(SystemExit, match="cp watchlist.example.yaml"):
        load(ENV, watchlist_path=str(tmp_path / "watchlist.yaml"))


def test_backup_channel_and_heartbeat_settings():
    cfg = load(dict(ENV, BACKUP_CHANNEL_ID="789", HEALTHCHECK_URL="https://hc-ping.com/x"), watchlist_path=EXAMPLE)
    assert (cfg.backup_channel_id, cfg.healthcheck_url) == (789, "https://hc-ping.com/x")
    assert load(ENV, watchlist_path=EXAMPLE).backup_channel_id is None
    with pytest.raises(SystemExit, match="BACKUP_CHANNEL_ID"):
        load(dict(ENV, BACKUP_CHANNEL_ID="#backups"), watchlist_path=EXAMPLE)
