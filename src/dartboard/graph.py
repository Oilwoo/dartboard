"""Stage 1 workflow (LangGraph): summarize a filing -> check its numbers -> record the opinion -> notify.

When the summary contains numbers that are not in the original filing, the graph pauses with
`interrupt()` and waits for a human decision (sent from Discord), then resumes from the checkpoint.
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
    report_nm: str
    source_text: str
    result: dict | None     # FilingSummary as a plain dict: checkpoints only store JSON-safe values
    faith_score: float
    unsupported: list[str]
    decision: str           # "keep" | "discard" (human), "auto" when no review was needed
    forecast_id: int | None
    notified: bool


Summarizer = Callable[[str], Awaitable[FilingSummary | None]]
Notifier = Callable[[State], Awaitable[None]]


def build(summarize: Summarizer, ledger: Ledger, notify: Notifier, model_name: str, checkpointer):
    def result(s: State) -> FilingSummary | None:
        return FilingSummary(**s["result"]) if s.get("result") else None

    async def summarize_node(s: State) -> State:
        r = await summarize(s["source_text"])
        return {"result": r.model_dump() if r else None}

    def check_node(s: State) -> State:
        r = result(s)
        if r is None:  # every model failed: rules only, the human sees the raw filing
            return {"faith_score": 0.0, "unsupported": ["요약 실패"]}
        rep = check(r.summary, s["source_text"])
        return {"faith_score": rep.score, "unsupported": rep.unsupported}

    def route(s: State) -> str:
        return "record" if s["faith_score"] == 1.0 else "review"

    def review_node(s: State) -> State:
        answer = interrupt({"rcept_no": s["rcept_no"], "unsupported": s["unsupported"],
                            "question": "원문에 없는 숫자가 있습니다. 이 요약을 기록할까요? (keep/discard)"})
        return {"decision": "keep" if answer == "keep" else "discard"}

    def record_node(s: State) -> State:
        r = result(s)
        if s.get("decision") == "discard" or r is None or r.stance == "none":
            return {"forecast_id": None, "decision": s.get("decision", "auto")}
        fid = ledger.append(ticker=s["ticker"], stance=r.stance, confidence=r.confidence,
                            horizon_days=r.horizon_days, rationale=r.summary,
                            sources=[f"dart:{s['rcept_no']}"], model=model_name)
        return {"forecast_id": fid, "decision": s.get("decision", "auto")}

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
