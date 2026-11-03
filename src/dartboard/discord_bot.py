"""Discord delivery: alerts, plus [승인]/[거절] buttons for anything that needs a human.

Discord rule (official docs, "Receiving and Responding"): an interaction must be acknowledged within
3 seconds; the token then stays valid for 15 minutes. So a click is deferred first, handled after.
"""
from __future__ import annotations

import asyncio

import discord

from .faithfulness import FaithfulnessReport


def alert_embed(corp_name: str, report_nm: str, summary: str, faith: FaithfulnessReport,
                stance: str | None, rcept_no: str) -> discord.Embed:
    color = discord.Color.green() if faith.score == 1.0 else discord.Color.orange()
    e = discord.Embed(title=f"{corp_name} · {report_nm}", description=summary[:4000], color=color,
                      url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}")
    e.add_field(name="숫자 검증", value=f"{faith.supported}/{faith.checked} 원문과 일치", inline=True)
    if faith.unsupported:
        e.add_field(name="원문에서 못 찾은 숫자", value=", ".join(faith.unsupported)[:1000], inline=False)
    if stance:
        e.add_field(name="의견 (성적표에 기록됨)", value=stance, inline=True)
    return e


class ApprovalView(discord.ui.View):
    """Only the approver can press the buttons. No answer before the timeout counts as a rejection."""

    def __init__(self, approver_id: int, timeout_s: float):
        super().__init__(timeout=timeout_s)
        self.approver_id = approver_id
        self.result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.approver_id:
            await interaction.response.send_message("승인 권한이 없습니다.", ephemeral=True)
            return False
        return True

    async def _decide(self, interaction: discord.Interaction, approved: bool) -> None:
        await interaction.response.defer()  # acknowledge inside the 3-second window
        if not self.result.done():
            self.result.set_result(approved)
        self.stop()

    @discord.ui.button(label="승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, True)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, False)

    async def on_timeout(self) -> None:
        if not self.result.done():
            self.result.set_result(False)


async def ask_approval(channel: discord.abc.Messageable, approver_id: int, text: str, timeout_s: float) -> bool:
    view = ApprovalView(approver_id, timeout_s)
    await channel.send(text, view=view)
    return await view.result
