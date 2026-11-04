"""Discord runtime: polls DART, asks for decisions with buttons, and posts daily scores.

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

from .config import Settings, load, load_dotenv
from .discord_bot import alert_embed, ask_approval
from .faithfulness import FaithfulnessReport
from .graph import State
from .ledger import Ledger, Report
from .pipeline import Pipeline, SeenStore, llm_summarizer
from .prices import Kis
from .scoring import score_due

KST = ZoneInfo("Asia/Seoul")
CATCH_UP_DAYS = 3  # the day's first poll looks back over weekends, late filings and downtime
log = logging.getLogger("dartboard")


def format_report(r: Report) -> str:
    if r.scored == 0:
        return "아직 채점된 의견이 없습니다."

    def pct(x):
        return "-" if x is None else f"{x * 100:+.1f}%p"

    return (f"채점 {r.scored}건 · 적중률 {r.hit_rate * 100:.0f}% · Brier {r.brier:.3f} (0.25면 동전 던지기 수준)\n"
            f"매수 의견 평균 초과수익 {pct(r.mean_excess_buy)} · 매도 의견 평균 {pct(r.mean_excess_sell)}")


def is_market_window(now: datetime) -> bool:
    return now.weekday() < 5 and 7 <= now.hour < 21


def poll_start(today: date, last_polled: date | None) -> date:
    return today if last_polled == today else today - timedelta(days=CATCH_UP_DAYS)


class DartboardBot(discord.Client):
    def __init__(self, cfg: Settings):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self._channel: discord.abc.Messageable | None = None
        self._last_polled: date | None = None

    async def setup_hook(self) -> None:
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._saver_cm = AsyncSqliteSaver.from_conn_string(os.path.join(self.cfg.data_dir, "checkpoints.db"))
        saver = await self._saver_cm.__aenter__()
        self.ledger = Ledger(os.path.join(self.cfg.data_dir, "ledger.db"))
        self.pipeline = Pipeline(
            dart_key=self.cfg.dart_key, watchlist=self.cfg.watchlist, ledger=self.ledger,
            seen=SeenStore(os.path.join(self.cfg.data_dir, "state.db")),
            summarize=llm_summarizer(self.cfg.summary_max_chars), notify=self.notify, approve=self.approve,
            checkpointer=saver)
        self.kis = (Kis(self.cfg.kis_key, self.cfg.kis_secret, self.cfg.kis_env,
                        os.path.join(self.cfg.data_dir, "kis_token.json")) if self.cfg.kis_key else None)

        @self.tree.command(name="report", description="예측 성적표 요약")
        async def report(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(format_report(self.ledger.report()))

        await self.tree.sync()
        self.poll.change_interval(minutes=self.cfg.poll_minutes)
        self.poll.start()
        self.daily_score.start()

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
        r = s.get("result") or {}
        faith = FaithfulnessReport(s.get("faith_checked", 0), s.get("faith_supported", 0), list(s.get("unsupported") or []))
        stance = r.get("stance") if s.get("forecast_id") else None
        summary = r.get("summary") or "요약에 실패했습니다. 원문 링크를 확인하세요."
        embed = alert_embed(s["corp_name"], s["report_nm"], summary, faith, stance, s["rcept_no"])
        await (await self.channel()).send(embed=embed)

    async def approve(self, q: dict) -> bool:
        faith = FaithfulnessReport(q.get("checked", 0), q.get("supported", 0), list(q.get("unsupported") or []))
        embed = alert_embed(q.get("corp_name", ""), q.get("report_nm", ""), q.get("summary") or "요약 실패",
                            faith, None, q["rcept_no"])
        text = (f"**검토 요청** · 요약에 원문에서 찾지 못한 숫자가 있습니다. 의견: `{q.get('stance', 'none')}`\n"
                f"승인하면 성적표에 기록합니다. {self.cfg.review_timeout_s // 60}분 안에 응답이 없으면 기록하지 않습니다.")
        return await ask_approval(await self.channel(), self.cfg.approver_id, text, self.cfg.review_timeout_s, embed)

    @tasks.loop(minutes=10)
    async def poll(self) -> None:
        now = datetime.now(KST)
        if not is_market_window(now):
            return
        try:
            await self.pipeline.run_once(now.date(), since=poll_start(now.date(), self._last_polled))
            self._last_polled = now.date()
        except Exception:
            log.exception("poll failed")

    @poll.before_loop
    async def _poll_waits_for_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(time=dtime(18, 30, tzinfo=KST))
    async def daily_score(self) -> None:
        if self.kis is None:
            return
        try:
            results = await asyncio.to_thread(score_due, self.ledger, self.kis, datetime.now(KST).date())
        except Exception:
            log.exception("scoring failed")
            return
        if results:
            done = sum(r.scored for r in results)
            await (await self.channel()).send(
                f"오늘 채점 {done}건 · 주가 확인이 안 돼 다음에 다시 볼 의견 {len(results) - done}건\n"
                + format_report(self.ledger.report()))

    @daily_score.before_loop
    async def _score_waits_for_ready(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load()
    DartboardBot(cfg).run(cfg.discord_token, log_handler=None)
