"""Pre-flight check before starting the bot: every outside service, with the fewest calls that prove it works.

    docker compose run --rm bot python -m dartboard.check

Secrets are never printed; error text from a service is shown with any secret value masked.
Exit code 0 when nothing failed.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Callable

from . import llm
from .config import Settings, load, load_dotenv
from .dart import list_disclosures
from .graph import Answer
from .heartbeat import ping
from .ledger import Ledger, OldLedgerError
from .pipeline import INSTRUCTIONS
from .prices import BENCHMARK, KST, Kis
from .scope import TARGET_TYPES

SECRET_ENVS = ("DART_API_KEY", "DISCORD_BOT_TOKEN", "KIS_APP_KEY", "KIS_APP_SECRET", "GROQ_API_KEY",
               "GEMINI_API_KEY", "LOCAL_LLM_KEY", "HEALTHCHECK_URL")
SAMPLE = ("회사: 테스트전자 (005930, 코스피)\n공시: 주요사항보고서(자기주식취득결정) (접수일 20261103)\n\n"
          "원문:\n취득예정주식수 | 1,250,000주\n취득예정금액 | 25,000,000,000원\n취득목적 | 주주가치 제고")
MIN_FREE_BYTES = 2 * 1024 ** 3


@dataclass
class Result:
    name: str
    status: str  # pass / fail / skip
    detail: str


def mask(text: str, env: dict) -> str:
    for key in SECRET_ENVS:
        value = env.get(key, "")
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text


def regular_close_consistent(rows: list[dict]) -> tuple[int, int]:
    """(days compared, days matching). KRX's change vs the previous day is measured from the previous regular
    close, so close(D) - change(D) must equal close(D-1) unless close(D-1) included after-market trades."""
    by_day = sorted((r for r in rows if r.get("stck_bsop_date") and r.get("stck_clpr")),
                    key=lambda r: r["stck_bsop_date"])
    compared = matched = 0
    for prev, cur in pairwise(by_day):
        if cur.get("flng_cls_code", "00") not in ("", "00"):  # ex-rights days reset the base price
            continue
        change = abs(float(cur.get("prdy_vrss") or 0))
        if cur.get("prdy_vrss_sign") in ("4", "5"):  # 하한, 하락
            change = -change
        compared += 1
        matched += float(cur["stck_clpr"]) - change == float(prev["stck_clpr"])
    return compared, matched


def check_settings(env: dict, watchlist_path: str) -> tuple[Result, Settings | None]:
    try:
        cfg = load(env, watchlist_path)
    except SystemExit as e:
        return Result("설정", "fail", str(e)), None
    optional = {"한국투자(채점)": cfg.kis_key, "집 PC 모델": env.get("LOCAL_LLM_URL") and env.get("LOCAL_LLM_MODEL"),
                "Groq": env.get("GROQ_URL") and env.get("GROQ_MODEL"),
                "Gemini": env.get("GEMINI_URL") and env.get("GEMINI_MODEL"), "하트비트": cfg.healthcheck_url}
    have = [k for k, v in optional.items() if v]
    missing = [k for k, v in optional.items() if not v]
    if not llm.configured(cfg.llm_order, env):
        return Result("설정", "fail", f"LLM_ORDER={cfg.llm_order}에 설정된 AI가 없습니다"), cfg
    return Result("설정", "pass", f"필수 4개 있음 · 관심 종목 {len(cfg.watchlist)}개 · 있음: {', '.join(have) or '-'}"
                  f" · 비어 있음: {', '.join(missing) or '-'}"), cfg


def check_storage(data_dir: str) -> Result:
    try:
        os.makedirs(data_dir, exist_ok=True)
        led = Ledger(os.path.join(data_dir, "ledger.db"))
        count, _ = led.head()
        led.db.close()
    except OldLedgerError as e:
        return Result("저장소", "fail", str(e))
    except OSError as e:
        return Result("저장소", "fail", f"{data_dir}에 쓸 수 없습니다: {e}")
    free = shutil.disk_usage(data_dir).free
    status = "pass" if free >= MIN_FREE_BYTES else "fail"
    return Result("저장소", status, f"성적표 기록 {count}개 · 남은 디스크 {free / 1024 ** 3:.1f}GB")


def check_dart(key: str, today: date, list_fn: Callable = list_disclosures) -> Result:
    counts, markets = {}, collections.Counter()
    for t in TARGET_TYPES:
        found = list_fn(key, today - timedelta(days=3), today, filing_type=t)
        counts[t] = len(found)
        markets.update(d.corp_cls or "?" for d in found)
    if not any(counts.values()):
        return Result("OpenDART", "fail", "최근 3일 대상 공시가 0건입니다(키나 종류 코드를 확인)")
    return Result("OpenDART", "pass", "최근 3일 " + " · ".join(f"{t} {n}건" for t, n in counts.items())
                  + " · 시장 구분 " + ", ".join(f"{k} {v}" for k, v in sorted(markets.items())))


def discord_get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"https://discord.com/api/v10{path}", headers={
        "Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://github.com/Oilwoo/dartboard, 0.1)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def check_discord(cfg: Settings, get: Callable[[str, str], dict] = discord_get) -> Result:
    me = get("/users/@me", cfg.discord_token)
    channel = get(f"/channels/{cfg.channel_id}", cfg.discord_token)
    approver = get(f"/users/{cfg.approver_id}", cfg.discord_token)
    backup = (get(f"/channels/{cfg.backup_channel_id}", cfg.discord_token)
              if cfg.backup_channel_id and cfg.backup_channel_id != cfg.channel_id else channel)
    return Result("Discord", "pass", f"봇 {me.get('username')} · 알림 채널 #{channel.get('name')} · "
                  f"백업 채널 #{backup.get('name')} · 승인자 {approver.get('username')}")


def check_models(cfg: Settings) -> list[Result]:
    names = list(dict.fromkeys(llm.configured(cfg.llm_order) + llm.configured(cfg.universe_llm_order)))
    results = []
    for name in names:
        try:
            out = asyncio.run(llm.run(llm.build_agent(Answer, INSTRUCTIONS, name), SAMPLE))
        except llm.Unavailable as e:
            results.append(Result(f"AI {name}", "fail", f"답을 받지 못함: {str(e)[:300]}"))
            continue
        if out is None:
            results.append(Result(f"AI {name}", "fail", "답이 형식(요약 + 확률)에 끝내 맞지 않음"))
        else:
            answer, model = out
            results.append(Result(f"AI {name}", "pass", f"모델 {model} · p={answer.p_up:.2f} · 도구 호출(required) 통과"))
    if "local" not in names:
        results.append(Result("AI local", "skip", "집 PC 모델이 없어 관심 종목 밖 공시(종목군)는 판단하지 않습니다"))
    return results


def check_kis(kis: Kis | None, today: date) -> list[Result]:
    if kis is None:
        return [Result("한국투자", "skip", "KIS 키가 없어 채점을 하지 않습니다")]
    results = []
    rows = kis.daily_rows("005930", today - timedelta(days=21), today - timedelta(days=1), adjusted=False)
    compared, matched = regular_close_consistent(rows)
    if compared == 0:
        results.append(Result("한국투자 일봉", "fail", "비교할 일봉이 없습니다"))
    else:
        status = "pass" if matched == compared else "fail"
        meaning = ("종가 = 정규장 종가" if status == "pass"
                   else "종가에 정규장 밖 가격이 섞였을 수 있음: 채점 가격 출처를 다시 정해야 함")
        results.append(Result("한국투자 일봉", status, f"전일 대비로 맞춰 본 {compared}일 중 {matched}일 일치 → {meaning}"))
    for market, code in BENCHMARK.items():
        bars = kis.daily(code, today - timedelta(days=14), today - timedelta(days=1), index=True)
        results.append(Result(f"한국투자 지수 {code}", "pass" if bars else "fail",
                              f"{market} 기준 지수 일봉 {len(bars)}개" + ("" if bars else ": 응답 필드 이름을 확인")))
    if kis.env == "real":
        days = kis.open_days(today - timedelta(days=7))
        results.append(Result("한국투자 휴장일", "pass" if days else "fail",
                              f"{len(days)}일치 · 개장일 {sum(days.values())}일 (하루 1번만 부르라는 API)"))
    return results


def check_heartbeat(url: str) -> Result:
    if not url:
        return Result("하트비트", "skip", "HEALTHCHECK_URL이 없어 봇이 멈춰도 바깥 알림이 없습니다")
    return Result("하트비트", "pass" if ping(url) else "fail", "점검 신호를 한 번 보냄")


def run(env: dict, watchlist_path: str = "watchlist.yaml", today: date | None = None) -> list[Result]:
    today = today or datetime.now(KST).date()
    settings, cfg = check_settings(env, watchlist_path)
    results = [settings]
    if cfg is None:
        return results
    steps: list[tuple[str, Callable[[], list[Result]]]] = [
        ("저장소", lambda: [check_storage(cfg.data_dir)]),
        ("OpenDART", lambda: [check_dart(cfg.dart_key, today)]),
        ("Discord", lambda: [check_discord(cfg)]),
        ("AI", lambda: check_models(cfg)),
        ("한국투자", lambda: check_kis(Kis(cfg.kis_key, cfg.kis_secret, cfg.kis_env,
                                         os.path.join(cfg.data_dir, "kis_token.json")) if cfg.kis_key else None, today)),
        ("하트비트", lambda: [check_heartbeat(cfg.healthcheck_url)]),
    ]
    for name, step in steps:
        try:
            results += step()
        except Exception as e:  # one broken service must not hide the state of the others
            results.append(Result(name, "fail", mask(f"{type(e).__name__}: {e}", env)[:400]))
    return [Result(r.name, r.status, mask(r.detail, env)) for r in results]


def report(results: list[Result]) -> str:
    label = {"pass": "통과", "fail": "실패", "skip": "건너뜀"}
    lines = [f"[{label[r.status]}] {r.name}: {r.detail}" for r in results]
    n = collections.Counter(r.status for r in results)
    verdict = ("실패한 항목을 고친 뒤 다시 실행하세요." if n["fail"]
               else "봇을 시작해도 됩니다: docker compose up -d && docker compose logs -f")
    lines.append(f"통과 {n['pass']} · 실패 {n['fail']} · 건너뜀 {n['skip']} → {verdict}")
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    results = run(dict(os.environ))
    print(report(results))
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
