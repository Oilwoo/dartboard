import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dartboard.graph import FilingSummary, build
from dartboard.ledger import Ledger

SOURCE = "취득예정주식수 : 1,250,000주, 발행주식총수 대비 비율 : 2.3%"
INPUT = {"rcept_no": "20261103000247", "corp_name": "테스트전자", "ticker": "005930",
         "report_nm": "자기주식취득결정", "source_text": SOURCE}


def make(summary_text, stance="buy", model="gemini-flash"):
    sent = []

    async def summarize(_state):
        return FilingSummary(summary=summary_text, stance=stance, confidence=0.6, horizon_days=20), model

    async def notify(state):
        sent.append(state)

    ledger = Ledger()
    app = build(summarize, ledger, notify, checkpointer=InMemorySaver())
    return app, ledger, sent


def test_faithful_summary_is_recorded_and_notified_without_review():
    app, ledger, sent = make("1,250,000주(발행주식의 2.3%)를 취득합니다.")
    out = asyncio.run(app.ainvoke(INPUT, {"configurable": {"thread_id": "t1"}}))
    assert out["decision"] == "auto" and out["forecast_id"] == 1 and out["notified"]
    assert len(sent) == 1 and ledger.verify_chain() is None
    assert ledger.db.execute("SELECT model FROM forecasts").fetchone() == ("gemini-flash",)  # who really answered


def test_unfaithful_summary_pauses_then_human_keeps_it():
    app, ledger, sent = make("발행주식의 3.2%를 취득합니다.")
    cfg = {"configurable": {"thread_id": "t2"}}
    first = asyncio.run(app.ainvoke(INPUT, cfg))
    assert "__interrupt__" in first and sent == []          # paused, nothing recorded or sent yet
    assert first["__interrupt__"][0].value["unsupported"] == ["3.2%"]
    out = asyncio.run(app.ainvoke(Command(resume="keep"), cfg))
    assert out["decision"] == "keep" and out["forecast_id"] == 1 and len(sent) == 1


def test_unfaithful_summary_discarded_by_human_is_not_recorded():
    app, ledger, sent = make("발행주식의 3.2%를 취득합니다.")
    cfg = {"configurable": {"thread_id": "t3"}}
    asyncio.run(app.ainvoke(INPUT, cfg))
    out = asyncio.run(app.ainvoke(Command(resume="discard"), cfg))
    assert out["decision"] == "discard" and out["forecast_id"] is None
    assert ledger.report().scored == 0 and len(sent) == 1  # the human still gets the outcome message


def test_stance_none_is_notified_but_not_recorded():
    app, ledger, sent = make("1,250,000주를 취득합니다.", stance="none")
    out = asyncio.run(app.ainvoke(INPUT, {"configurable": {"thread_id": "t4"}}))
    assert out["forecast_id"] is None and len(sent) == 1
