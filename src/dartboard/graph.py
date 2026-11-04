"""Stage 1 workflow (LangGraph): summarize a filing -> check its numbers -> record the opinion -> notify.

A summary with numbers missing from the filing pauses at `review` until a human answers on Discord.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from .faithfulness import check
from .ledger import Ledger


class FilingSummary(BaseModel):
    summary: str = Field(description="3-5 Korean sentences; copy every figure exactly as written in the filing")
    stance: Literal["buy", "sell", "hold", "none"] = Field(description="'none' unless the filing clearly matters")
    confidence: float = Field(ge=0, le=1)
    horizon_days: int = Field(ge=1, le=120)


class State(TypedDict, total=False):
    rcept_no: str
    corp_name: str
    ticker: str
    market: str           # OpenDART corp_cls: Y KOSPI, K KOSDAQ
    report_nm: str
    source_text: str
    result: dict | None   # FilingSummary as a dict: checkpoints store only JSON-safe values
    model: str            # the model that actually answered
    faith_checked: int
    faith_supported: int
    unsupported: list[str]
    decision: str         # keep / discard (human), auto (no review needed)
    forecast_id: int | None
    notified: bool


Summarizer = Callable[[State], Awaitable[tuple[FilingSummary, str] | None]]
Notifier = Callable[[State], Awaitable[None]]


def build(summarize: Summarizer, ledger: Ledger, notify: Notifier, checkpointer):
    def summary_of(s: State) -> FilingSummary | None:
        return FilingSummary(**s["result"]) if s.get("result") else None

    async def summarize_node(s: State) -> State:
        out = await summarize(s)
        if out is None:
            return {"result": None}
        summary, model = out
        return {"result": summary.model_dump(), "model": model}

    def check_node(s: State) -> State:
        r = summary_of(s)
        if r is None:  # every model failed: a human decides from the original filing
            return {"faith_checked": 0, "faith_supported": 0, "unsupported": ["요약 실패"]}
        rep = check(r.summary, s["source_text"])
        return {"faith_checked": rep.checked, "faith_supported": rep.supported, "unsupported": rep.unsupported}

    def route(s: State) -> str:
        return "review" if s["unsupported"] else "record"

    def review_node(s: State) -> State:
        # the question carries everything needed to ask it again after a restart
        r = s.get("result") or {}
        answer = interrupt({"rcept_no": s["rcept_no"], "corp_name": s["corp_name"], "report_nm": s["report_nm"],
                            "summary": r.get("summary", ""), "stance": r.get("stance", "none"),
                            "checked": s["faith_checked"], "supported": s["faith_supported"],
                            "unsupported": s["unsupported"]})
        return {"decision": "keep" if answer == "keep" else "discard"}

    def record_node(s: State) -> State:
        r = summary_of(s)
        decision = s.get("decision", "auto")
        if decision == "discard" or r is None or r.stance == "none":
            return {"forecast_id": None, "decision": decision}
        fid = ledger.append(ticker=s["ticker"], stance=r.stance, confidence=r.confidence,
                            horizon_days=r.horizon_days, rationale=r.summary, sources=[f"dart:{s['rcept_no']}"],
                            model=s["model"], market=s.get("market", "Y"))
        return {"forecast_id": fid, "decision": decision}

    async def notify_node(s: State) -> State:
        await notify(s)
        return {"notified": True}

    g = StateGraph(State)
    g.add_node("summarize", summarize_node)
    g.add_node("check", check_node)
    g.add_node("review", review_node)
    g.add_node("record", record_node)
    g.add_node("notify", notify_node)
    g.add_edge(START, "summarize")
    g.add_edge("summarize", "check")
    g.add_conditional_edges("check", route, {"record": "record", "review": "review"})
    g.add_edge("review", "record")
    g.add_edge("record", "notify")
    g.add_edge("notify", END)
    return g.compile(checkpointer=checkpointer)
