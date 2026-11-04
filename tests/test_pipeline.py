"""The Stage 1 pipeline end to end, with fake DART, LLM and Discord."""
import asyncio
from datetime import date

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from dartboard.dart import Disclosure, RateLimited
from dartboard.graph import FilingSummary
from dartboard.ledger import Ledger
from dartboard.pipeline import Pipeline, SeenStore

SOURCE = "취득예정주식수 : 1,250,000주, 발행주식총수 대비 비율 : 2.3%"
FAITHFUL = "1,250,000주(발행주식의 2.3%)를 취득합니다."
UNFAITHFUL = "발행주식의 3.2%를 취득합니다."
DAY = date(2026, 11, 3)


def filing(rcept_no, stock_code="005930", corp_cls="Y"):
    return Disclosure(rcept_no, "20261103", "00126380", "테스트전자", stock_code, "자기주식취득결정", corp_cls)


def make(*, summaries=None, filings=None, approve=None, notify=None, doc_fn=None,
         checkpointer=None, ledger=None, seen=None):
    log = {"notified": [], "questions": [], "docs": []}
    summaries = summaries or {}

    async def summarize(state):
        text = summaries.get(state["rcept_no"], FAITHFUL)
        return None if text is None else (FilingSummary(summary=text, stance="buy", confidence=0.6, horizon_days=20),
                                          "qwen-test")

    async def default_notify(state):
        log["notified"].append(state["rcept_no"])

    async def default_approve(q):
        log["questions"].append(q)
        return True

    def default_doc(key, rcept_no):
        log["docs"].append(rcept_no)
        return SOURCE

    p = Pipeline(dart_key="key", watchlist={"005930": "테스트전자", "035720": "카카오"},
                 ledger=ledger if ledger is not None else Ledger(), seen=seen or SeenStore(),
                 summarize=summarize, notify=notify or default_notify, approve=approve or default_approve,
                 checkpointer=checkpointer or InMemorySaver(),
                 list_fn=lambda key, bgn, end: list(filings if filings is not None else [filing("A1")]),
                 doc_fn=doc_fn or default_doc)
    return p, log


def test_watchlist_filing_is_recorded_once_and_others_are_ignored():
    ledger = Ledger()

    async def go():
        p, log = make(filings=[filing("A1"), filing("B1", stock_code="000000"), filing("K1", "035720", "K")],
                      ledger=ledger)
        assert await p.run_once(DAY) == ["A1", "K1"]
        assert await p.run_once(DAY) == []  # already handled
        return p, log

    p, log = asyncio.run(go())
    assert log["notified"] == ["A1", "K1"] and log["docs"] == ["A1", "K1"]
    assert p.seen.status("A1") == "auto" and p.seen.status("B1") is None
    assert [(row[1], row[2]) for row in ledger.due("2030-01-01")] == [("005930", "Y"), ("035720", "K")]


def test_question_waits_in_the_background_while_other_filings_move_on():
    ledger = Ledger()

    async def go():
        gate, questions = asyncio.Event(), []

        async def approve(q):
            questions.append(q)
            await gate.wait()
            return True

        p, log = make(summaries={"A1": UNFAITHFUL}, filings=[filing("A1"), filing("A2")], approve=approve,
                      ledger=ledger)
        assert await p.run_once(DAY) == ["A1", "A2"]
        await asyncio.sleep(0)
        assert p.seen.status("A1") == "pending" and p.seen.status("A2") == "auto"
        assert log["notified"] == ["A2"]  # A2 was not held up by A1's open question
        gate.set()
        await p.drain()
        return p, log, questions

    p, log, questions = asyncio.run(go())
    assert p.seen.status("A1") == "keep" and log["notified"] == ["A2", "A1"]
    q = questions[0]
    assert (q["corp_name"], q["summary"], q["unsupported"]) == ("테스트전자", UNFAITHFUL, ["3.2%"])
    assert (q["checked"], q["supported"]) == (1, 0)
    assert len(ledger.due("2030-01-01")) == 2


def test_restart_while_waiting_asks_again_and_continues_from_the_checkpoint(tmp_path):
    files = {k: str(tmp_path / f"{k}.db") for k in ("checkpoints", "ledger", "state")}

    async def first_process():
        never = asyncio.Event()

        async def approve(q):
            await never.wait()
            return True

        async with AsyncSqliteSaver.from_conn_string(files["checkpoints"]) as saver:
            p, _ = make(summaries={"A1": UNFAITHFUL}, approve=approve, checkpointer=saver,
                        ledger=Ledger(files["ledger"]), seen=SeenStore(files["state"]))
            await p.run_once(DAY)
            assert p.seen.status("A1") == "pending"
            for task in list(p._tasks):  # the process dies with the question still open
                task.cancel()
            await asyncio.gather(*p._tasks, return_exceptions=True)

    async def second_process():
        asked = []

        async def approve(q):
            asked.append(q)
            return False

        def no_download(key, rcept_no):
            raise AssertionError("the filing must not be downloaded or summarized again")

        async with AsyncSqliteSaver.from_conn_string(files["checkpoints"]) as saver:
            p, log = make(approve=approve, doc_fn=no_download, checkpointer=saver,
                          ledger=Ledger(files["ledger"]), seen=SeenStore(files["state"]))
            assert await p.resume_pending() == 1
            await p.drain()
        return asked, log

    asyncio.run(first_process())
    asked, log = asyncio.run(second_process())
    assert asked[0]["rcept_no"] == "A1" and asked[0]["summary"] == UNFAITHFUL
    assert SeenStore(files["state"]).status("A1") == "discard"
    assert Ledger(files["ledger"]).due("2030-01-01") == [] and log["notified"] == ["A1"]


def test_failed_notification_is_retried_without_a_second_ledger_row():
    ledger, attempts = Ledger(), []

    async def flaky_notify(state):
        attempts.append(state["rcept_no"])
        if len(attempts) == 1:
            raise RuntimeError("Discord is down")

    async def go():
        p, log = make(notify=flaky_notify, ledger=ledger)
        assert await p.run_once(DAY) == []  # failed: left for the next run
        assert p.seen.status("A1") is None
        assert await p.run_once(DAY) == ["A1"]  # continues at the notify step
        return p, log

    p, log = asyncio.run(go())
    assert attempts == ["A1", "A1"] and log["docs"] == ["A1"]
    assert len(ledger.due("2030-01-01")) == 1 and ledger.verify_chain() is None


def test_failed_question_stays_pending_and_is_asked_again():
    attempts = []

    async def approve(q):
        attempts.append(q["rcept_no"])
        if len(attempts) == 1:
            raise RuntimeError("channel not found")
        return True

    async def go():
        p, _ = make(summaries={"A1": UNFAITHFUL}, approve=approve)
        await p.run_once(DAY)
        await p.drain()
        assert p.seen.status("A1") == "pending"
        await p.run_once(DAY)  # the next poll picks it up again
        await p.drain()
        return p

    p = asyncio.run(go())
    assert attempts == ["A1", "A1"] and p.seen.status("A1") == "keep"


def test_rate_limit_stops_the_run_and_the_filings_are_retried_later():
    limited = {"on": True}

    def doc_fn(key, rcept_no):
        if limited["on"]:
            raise RateLimited("요청 제한을 초과하였습니다.")
        return SOURCE

    async def go():
        p, _ = make(filings=[filing("A1"), filing("A2")], doc_fn=doc_fn)
        assert await p.run_once(DAY) == []
        limited["on"] = False
        return await p.run_once(DAY)

    assert asyncio.run(go()) == ["A1", "A2"]


def test_when_every_model_fails_the_human_decides_from_the_original():
    async def go():
        questions = []

        async def approve(q):
            questions.append(q)
            return False

        p, log = make(summaries={"A1": None}, approve=approve)
        await p.run_once(DAY)
        await p.drain()
        return p, log, questions

    p, log, questions = asyncio.run(go())
    assert questions[0]["unsupported"] == ["요약 실패"] and questions[0]["summary"] == ""
    assert p.seen.status("A1") == "discard" and log["notified"] == ["A1"]
