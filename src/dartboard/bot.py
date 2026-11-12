"""Discord runtime: polls DART, asks for decisions with buttons, scores daily and posts the ledger's head hash.

Run `python -m dartboard` in a folder with .env and watchlist.yaml.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from . import llm
from .config import Settings, load, load_dotenv
from .discord_bot import alert_embed, ask_approval
from .faithfulness import FaithfulnessReport
from .graph import Answer, State
from .ledger import Ledger
from .pipeline import INSTRUCTIONS, Pipeline, SeenStore, llm_answerer
from .prices import Kis
from .report import format_scorecard, scorecard
from .scoring import ScoreRun, score_due

KST = ZoneInfo("Asia/Seoul")
CATCH_UP_DAYS = 3  # every poll looks back this far: weekends, late filings, and a backlog left while the PC was off
log = logging.getLogger("dartboard")


def is_market_window(now: datetime) -> bool:
    return now.weekday() < 5 and 7 <= now.hour < 21


def poll_start(today: date) -> date:
    return today - timedelta(days=CATCH_UP_DAYS)


def daily_check_text(today: date, broken_at: int | None, count: int, head: str, run: ScoreRun | None) -> str:
    chain = "해시 체인 이상 없음" if broken_at is None else f"경고: {broken_at}번 기록부터 해시 체인이 맞지 않습니다"
    lines = [f"성적표 점검 {today.isoformat()} · {chain} · 기록 {count}개", f"끝 해시 `{head}`"]
    if run is not None:
        lines.append(f"오늘 채점 기록 {run.recorded}건 · 기간이 남은 예측 {run.waiting}건 · 주가 조회 실패 {run.failed}건")
    return "\n".join(lines)


class DartboardBot(discord.Client):
    def __init__(self, cfg: Settings):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self._channel: discord.abc.Messageable | None = None

    async def setup_hook(self) -> None:
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._saver_cm = AsyncSqliteSaver.from_conn_string(os.path.join(self.cfg.data_dir, "checkpoints.db"))
        saver = await self._saver_cm.__aenter__()
        self.ledger = Ledger(os.path.join(self.cfg.data_dir, "ledger.db"))
        watchlist_agent = llm.build_agent(Answer, INSTRUCTIONS, self.cfg.llm_order)
        universe_agent = llm.build_agent(Answer, INSTRUCTIONS, self.cfg.universe_llm_order)
        if universe_agent is None:
            log.warning("no model configured for UNIVERSE_LLM_ORDER=%s: only watchlist filings are judged",
                        self.cfg.universe_llm_order)
        self.pipeline = Pipeline(
            dart_key=self.cfg.dart_key, watchlist=self.cfg.watchlist, ledger=self.ledger,
            seen=SeenStore(os.path.join(self.cfg.data_dir, "state.db")),
            answer=llm_answerer(watchlist_agent, universe_agent, self.cfg.summary_max_chars),
            notify=self.notify, approve=self.approve, checkpointer=saver, judge_universe=universe_agent is not None)
        self.kis = (Kis(self.cfg.kis_key, self.cfg.kis_secret, self.cfg.kis_env,
                        os.path.join(self.cfg.data_dir, "kis_token.json")) if self.cfg.kis_key else None)

        @self.tree.command(name="report", description="예측 성적표 요약")
        async def report(interaction: discord.Interaction) -> None:
            text = await asyncio.to_thread(lambda: format_scorecard(scorecard(self.ledger)))
            await interaction.response.send_message(text[:2000])

        await self.tree.sync()
        self.poll.change_interval(minutes=self.cfg.poll_minutes)
        self.poll.start()
        self.daily_check.start()

    async def on_ready(self) -> None:
        picked = await self.pipeline.resume_pending()  # safe to repeat, so reconnects are harmless
        log.info("ready as %s; watching %d stocks; %d pending decision(s) picked up",
                 self.user, len(self.cfg.watchlist), picked)

    async def close(self) -> None:
        if hasattr(self, "_saver_cm"):
            await self._saver_cm.__aexit__(None, None, None)
        await super().close()

    async def channel(self) -> discord.abc.Messageable:
        if self._channel is None:
            self._channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(self.cfg.channel_id)
        return self._channel

    async def notify(self, s: State) -> None:
        a = s.get("answer")
        faith = FaithfulnessReport(s.get("faith_checked", 0), s.get("faith_supported", 0), list(s.get("unsupported") or []))
        summary = a["summary"] if a else "AI가 형식에 맞는 답을 내지 못했습니다(실패로 기록). 원문 링크를 확인하세요."
        embed = alert_embed(s["corp_name"], s["report_nm"], summary, faith, a["p_up"] if a else None, s["rcept_no"])
        if s.get("kind") == "follow_up":
            embed.set_footer(text="정정·철회 같은 후속 공시: 기록하지만 공식 채점에는 넣지 않습니다.")
        await (await self.channel()).send(embed=embed)

    async def approve(self, q: dict) -> bool:
        faith = FaithfulnessReport(q.get("checked", 0), q.get("supported", 0), list(q.get("unsupported") or []))
        embed = alert_embed(q.get("corp_name", ""), q.get("report_nm", ""), q.get("summary", ""), faith,
                            q.get("p_up"), q["rcept_no"])
        text = ("**요약 확인 요청** · 요약에 원문에서 찾지 못한 숫자가 있습니다.\n"
                "판단은 이미 성적표에 기록됐고, 승인·거절은 사람 결정으로 따로 남습니다. "
                f"{self.cfg.review_timeout_s // 60}분 안에 응답이 없으면 거절로 남습니다.")
        return await ask_approval(await self.channel(), self.cfg.approver_id, text, self.cfg.review_timeout_s, embed)

    @tasks.loop(minutes=10)
    async def poll(self) -> None:
        now = datetime.now(KST)
        if not is_market_window(now):
            return
        try:
            await self.pipeline.run_once(now.date(), since=poll_start(now.date()))
        except Exception:
            log.exception("poll failed")

    @poll.before_loop
    async def _poll_waits_for_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(time=dtime(18, 30, tzinfo=KST))
    async def daily_check(self) -> None:
        today = datetime.now(KST).date()
        run = None
        if self.kis is not None:
            try:
                run = await asyncio.to_thread(score_due, self.ledger, self.kis, today)
            except Exception:
                log.exception("scoring failed")
        broken_at = await asyncio.to_thread(self.ledger.verify_chain)
        count, head = self.ledger.head()
        text = daily_check_text(today, broken_at, count, head, run)
        text += "\n" + await asyncio.to_thread(lambda: format_scorecard(scorecard(self.ledger)))
        await (await self.channel()).send(text[:2000])

    @daily_check.before_loop
    async def _check_waits_for_ready(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load()
    if not llm.configured(cfg.llm_order):
        raise SystemExit(f"no model configured for LLM_ORDER={cfg.llm_order}: "
                         "set the URL and MODEL of at least one provider in .env")
    DartboardBot(cfg).run(cfg.discord_token, log_handler=None)
