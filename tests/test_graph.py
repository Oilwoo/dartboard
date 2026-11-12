import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dartboard import llm
from dartboard.graph import Answer, build
from dartboard.ledger import Ledger

SOURCE = "취득예정주식수 : 1,250,000주, 발행주식총수 대비 비율 : 2.3%"
FILING = {"rcept_no": "20261103000247", "rcept_dt": "20261103", "corp_code": "00126380", "corp_name": "테스트전자",
          "ticker": "005930", "market": "Y", "filing_type": "B001", "report_nm": "주요사항보고서(자기주식취득결정)",
          "kind": "new", "route": "watchlist", "source_text": SOURCE}
FAITHFUL = "1,250,000주(발행주식의 2.3%)를 취득합니다."
UNFAITHFUL = "발행주식의 3.2%를 취득합니다."


def make(summary=FAITHFUL, p_up=0.62, model="gemini-flash", ledger=None):
    sent = []

    async def answer(_state):
        if summary is None:
            return None  # the model answered, but never in the required shape
        return Answer(summary=summary, p_up=p_up), model

    async def notify(state):
        sent.append(state)

    ledger = ledger or Ledger()
    return build(answer, ledger, notify, checkpointer=InMemorySaver(), prompt_version="v1"), ledger, sent


def run(app, inp, thread="t"):
    return asyncio.run(app.ainvoke(inp, {"configurable": {"thread_id": thread}}))


def test_faithful_judgment_is_recorded_then_notified_without_review():
    app, ledger, sent = make()
    out = run(app, FILING)
    assert out["decision"] == "auto" and out["notified"] and len(sent) == 1
    [(_, j)] = ledger.judgments()
    assert (j.status, j.p_up, j.model, j.prompt_version, j.route) == ("forecast", 0.62, "gemini-flash", "v1", "watchlist")
    assert ledger.verify_chain() is None


def test_unfaithful_summary_is_recorded_first_then_the_human_decision_is_added():
    app, ledger, sent = make(UNFAITHFUL)
    cfg = {"configurable": {"thread_id": "t2"}}
    first = asyncio.run(app.ainvoke(FILING, cfg))
    q = first["__interrupt__"][0].value
    assert q["unsupported"] == ["3.2%"] and q["p_up"] == 0.62 and sent == []
    assert len(ledger.judgments()) == 1 and ledger.reviews() == {}   # judged and recorded, decision pending
    out = asyncio.run(app.ainvoke(Command(resume="discard"), cfg))
    assert out["decision"] == "discard" and len(sent) == 1
    assert ledger.reviews() == {out["judgment_seq"]: "discard"}      # rejected, yet still in the ledger
    assert ledger.unscored((5,))                                      # and still scored later (Q21)


def test_a_failed_answer_is_recorded_as_a_failed_judgment_and_the_human_is_told():
    app, ledger, sent = make(summary=None)
    out = run(app, FILING)
    [(_, j)] = ledger.judgments()
    assert (j.status, j.p_up) == ("failed", None) and out["decision"] == "auto" and len(sent) == 1


def test_no_reachable_model_records_nothing_so_the_filing_can_be_tried_again():
    async def answer(_state):
        raise llm.Unavailable("PC off and free tiers exhausted")

    ledger = Ledger()
    app = build(answer, ledger, lambda s: None, checkpointer=InMemorySaver())
    with pytest.raises(llm.Unavailable):
        run(app, FILING)
    assert ledger.judgments() == []


def test_universe_filings_are_recorded_without_discord_or_review():
    app, ledger, sent = make(UNFAITHFUL)
    out = run(app, dict(FILING, route="universe"))
    assert "__interrupt__" not in out and sent == []
    [(_, j)] = ledger.judgments()
    assert j.route == "universe" and j.unsupported == ["3.2%"]       # the failed check is kept for the report


def test_follow_up_is_linked_to_the_original_filing():
    ledger = Ledger()
    app, _, _ = make(ledger=ledger)
    run(app, FILING, "a")
    run(app, dict(FILING, rcept_no="20261105000100", kind="follow_up",
                  report_nm="[기재정정]주요사항보고서(자기주식취득결정)"), "b")
    follow = ledger.judgments()[1][1]
    assert follow.kind == "follow_up" and follow.linked_rcept_no == "20261103000247"
