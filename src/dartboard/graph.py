"""Judgment workflow (LangGraph): answer -> check numbers -> prepare -> record -> (review) -> notify.

Every judgment is recorded, failed ones included. When a watchlist summary has numbers that are not in the
filing, a human is asked afterwards and the answer is recorded as a separate review entry. Universe filings
are recorded only: no Discord card, no human.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from .faithfulness import check
from .ledger import Judgment, Ledger
from .scope import base_title


class Answer(BaseModel):
    summary: str = Field(description="3-5 Korean sentences; copy every figure exactly as written in the filing")
    p_up: float = Field(ge=0, le=1, description="probability that the stock's return beats its market index "
                                                "over the next 5 trading sessions")


class State(TypedDict, total=False):
    # the filing, set by the pipeline
    rcept_no: str
    rcept_dt: str
    corp_code: str
    corp_name: str
    ticker: str
    market: str           # Y KOSPI, K KOSDAQ
    filing_type: str
    report_nm: str
    kind: str             # new / follow_up
    route: str            # watchlist / universe
    source_text: str
    # the judgment
    answer: dict | None   # Answer as a dict: checkpoints store only JSON-safe values
    model: str
    prompt_version: str
    faith_checked: int
    faith_supported: int
    unsupported: list[str]
    judgment: dict        # the exact entry to record, fixed before recording so a re-run writes the same
    judgment_seq: int
    decision: str         # auto (no human needed) / keep / discard
    notified: bool


Answerer = Callable[[State], Awaitable[tuple[Answer, str] | None]]
Notifier = Callable[[State], Awaitable[None]]


def build(answer: Answerer, ledger: Ledger, notify: Notifier, checkpointer, prompt_version: str = ""):
    async def answer_node(s: State) -> State:
        out = await answer(s)  # llm.Unavailable propagates: the filing is tried again on the next run
        if out is None:
            return {"answer": None, "model": "", "prompt_version": prompt_version}
        a, model = out
        return {"answer": a.model_dump(), "model": model, "prompt_version": prompt_version}

    def check_node(s: State) -> State:
        if s["answer"] is None:
            return {"faith_checked": 0, "faith_supported": 0, "unsupported": []}
        rep = check(s["answer"]["summary"], s["source_text"])
        return {"faith_checked": rep.checked, "faith_supported": rep.supported, "unsupported": rep.unsupported}

    def prepare_node(s: State) -> State:
        a = s["answer"]
        title = base_title(s["report_nm"])
        link = (ledger.find_original(s["corp_code"], title, s["rcept_no"]) if s["kind"] == "follow_up" else None)
        j = Judgment(rcept_no=s["rcept_no"], rcept_dt=s["rcept_dt"], corp_code=s["corp_code"],
                     corp_name=s["corp_name"], ticker=s["ticker"], market=s["market"],
                     filing_type=s["filing_type"], report_nm=s["report_nm"], base_title=title,
                     kind=s["kind"], route=s["route"], status="forecast" if a else "failed",
                     p_up=a["p_up"] if a else None, summary=a["summary"] if a else "", model=s["model"],
                     prompt_version=s["prompt_version"], faith_checked=s["faith_checked"],
                     faith_supported=s["faith_supported"], unsupported=list(s["unsupported"]),
                     linked_rcept_no=link)
        return {"judgment": asdict(j)}

    def record_node(s: State) -> State:
        return {"judgment_seq": ledger.record_judgment(Judgment(**s["judgment"])), "decision": "auto"}

    def after_record(s: State) -> str:
        if s["route"] == "universe":
            return "end"
        return "review" if s["answer"] and s["unsupported"] else "notify"

    def review_node(s: State) -> State:
        # the question carries everything needed to ask it again after a restart
        answer = interrupt({"rcept_no": s["rcept_no"], "corp_name": s["corp_name"], "report_nm": s["report_nm"],
                            "summary": s["answer"]["summary"], "p_up": s["answer"]["p_up"],
                            "checked": s["faith_checked"], "supported": s["faith_supported"],
                            "unsupported": s["unsupported"]})
        return {"decision": "keep" if answer == "keep" else "discard"}

    def record_review_node(s: State) -> State:
        ledger.record_review(s["judgment_seq"], s["decision"])
        return {}

    async def notify_node(s: State) -> State:
        await notify(s)
        return {"notified": True}

    g = StateGraph(State)
    g.add_node("answer", answer_node)
    g.add_node("check", check_node)
    g.add_node("prepare", prepare_node)
    g.add_node("record", record_node)
    g.add_node("review", review_node)
    g.add_node("record_review", record_review_node)
    g.add_node("notify", notify_node)
    g.add_edge(START, "answer")
    g.add_edge("answer", "check")
    g.add_edge("check", "prepare")
    g.add_edge("prepare", "record")
    g.add_conditional_edges("record", after_record, {"end": END, "review": "review", "notify": "notify"})
    g.add_edge("review", "record_review")
    g.add_edge("record_review", "notify")
    g.add_edge("notify", END)
    return g.compile(checkpointer=checkpointer)
