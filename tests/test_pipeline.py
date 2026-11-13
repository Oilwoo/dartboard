"""The pipeline end to end, with fake DART, LLM and Discord."""
import asyncio
from datetime import date

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from dartboard import llm, pipeline
from dartboard.dart import Disclosure, RateLimited
from dartboard.graph import Answer
from dartboard.ledger import Ledger
from dartboard.pipeline import PROMPT_VERSION, Pipeline, SeenStore

SOURCE = "취득예정주식수 : 1,250,000주, 발행주식총수 대비 비율 : 2.3%"
FAITHFUL = "1,250,000주(발행주식의 2.3%)를 취득합니다."
UNFAITHFUL = "발행주식의 3.2%를 취득합니다."
DAY = date(2026, 11, 3)
WATCHED, OTHER = "005930", "000020"


def filing(rcept_no, stock_code=WATCHED, report_nm="주요사항보고서(자기주식취득결정)", filing_type="B001",
           corp_cls="Y", corp_name="테스트전자"):
    return Disclosure(rcept_no, "20261103", "00126380", corp_name, stock_code, report_nm, corp_cls, filing_type)


def make(*, answers=None, filings=None, approve=None, notify=None, doc_fn=None, checkpointer=None, ledger=None,
         seen=None, judge_universe=True):
    log = {"notified": [], "questions": [], "docs": [], "answered": [], "listed": []}
    answers = answers or {}

    async def answer(state):
        log["answered"].append(state["rcept_no"])
        a = answers.get(state["rcept_no"], FAITHFUL)
        if isinstance(a, Exception):
            raise a
        return None if a is None else (Answer(summary=a, p_up=0.6), "qwen-test")

    async def default_notify(state):
        log["notified"].append(state["rcept_no"])

    async def default_approve(q):
        log["questions"].append(q)
        return True

    def default_doc(key, rcept_no):
        log["docs"].append(rcept_no)
        return SOURCE

    def list_fn(key, bgn, end, filing_type=None):
        log["listed"].append(filing_type)
        return [d for d in (filings if filings is not None else [filing("A1")]) if d.filing_type == filing_type]

    p = Pipeline(dart_key="key", watchlist={WATCHED: "테스트전자"}, ledger=ledger if ledger is not None else Ledger(),
                 seen=seen or SeenStore(), answer=answer, notify=notify or default_notify,
                 approve=approve or default_approve, checkpointer=checkpointer or InMemorySaver(),
                 judge_universe=judge_universe, list_fn=list_fn, doc_fn=doc_fn or default_doc)
    return p, log


def test_watchlist_goes_to_discord_universe_is_recorded_and_the_rest_is_skipped():
    ledger = Ledger()
    filings = [filing("A1"),
               filing("B1", OTHER, filing_type="I002", report_nm="영업(잠정)실적(공정공시)"),
               filing("C1", OTHER, filing_type="I001", report_nm="기업설명회(IR)개최"),
               filing("D1", OTHER, corp_cls="N"),
               filing("E1", OTHER, corp_name="에이스팩1호")]

    async def go():
        p, log = make(filings=filings, ledger=ledger)
        assert await p.run_once(DAY) == ["A1", "B1"]
        assert await p.run_once(DAY) == []  # already handled
        return p, log

    p, log = asyncio.run(go())
    assert log["listed"][:3] == ["B001", "I001", "I002"]
    assert log["notified"] == ["A1"] and log["docs"] == ["A1", "B1"]
    routes = {j.rcept_no: (j.route, j.filing_type, j.prompt_version) for _, j in ledger.judgments()}
    assert routes == {"A1": ("watchlist", "B001", PROMPT_VERSION), "B1": ("universe", "I002", PROMPT_VERSION)}
    assert [p.seen.status(r) for r in ("C1", "D1", "E1")] == ["admin", "out_of_scope", "out_of_scope"]


def test_follow_up_is_judged_and_linked_but_marked_as_follow_up():
    ledger = Ledger()
    filings = [filing("20261103000001"), filing("20261103000002", report_nm="[기재정정]주요사항보고서(자기주식취득결정)")]
    p, _ = make(filings=filings, ledger=ledger)
    asyncio.run(p.run_once(DAY))
    follow = ledger.judgments()[1][1]
    assert (follow.kind, follow.linked_rcept_no) == ("follow_up", "20261103000001")


def test_question_waits_in_the_background_while_other_filings_move_on():
    ledger = Ledger()

    async def go():
        gate, questions = asyncio.Event(), []

        async def approve(q):
            questions.append(q)
            await gate.wait()
            return True

        p, log = make(answers={"A1": UNFAITHFUL}, filings=[filing("A1"), filing("A2")], approve=approve,
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
    assert (q["corp_name"], q["summary"], q["unsupported"], q["p_up"]) == ("테스트전자", UNFAITHFUL, ["3.2%"], 0.6)
    assert len(ledger.judgments()) == 2 and list(ledger.reviews().values()) == ["keep"]


def test_restart_while_waiting_asks_again_and_continues_from_the_checkpoint(tmp_path):
    files = {k: str(tmp_path / f"{k}.db") for k in ("checkpoints", "ledger", "state")}

    async def first_process():
        never = asyncio.Event()

        async def approve(q):
            await never.wait()
            return True

        async with AsyncSqliteSaver.from_conn_string(files["checkpoints"]) as saver:
            p, _ = make(answers={"A1": UNFAITHFUL}, approve=approve, checkpointer=saver,
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
            raise AssertionError("the filing must not be downloaded or judged again")

        async with AsyncSqliteSaver.from_conn_string(files["checkpoints"]) as saver:
            p, log = make(approve=approve, doc_fn=no_download, checkpointer=saver,
                          ledger=Ledger(files["ledger"]), seen=SeenStore(files["state"]))
            assert await p.resume_pending() == 1
            await p.drain()
        return asked, log

    asyncio.run(first_process())
    asked, log = asyncio.run(second_process())
    assert asked[0]["rcept_no"] == "A1" and asked[0]["summary"] == UNFAITHFUL
    assert SeenStore(files["state"]).status("A1") == "discard" and log["answered"] == []
    ledger = Ledger(files["ledger"])
    assert len(ledger.judgments()) == 1 and list(ledger.reviews().values()) == ["discard"]
    assert ledger.verify_chain() is None


def test_failed_notification_is_retried_without_a_second_ledger_entry():
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
    assert attempts == ["A1", "A1"] and log["docs"] == ["A1"] and log["answered"] == ["A1"]
    assert len(ledger.judgments()) == 1 and ledger.verify_chain() is None


def test_failed_question_stays_pending_and_is_asked_again():
    attempts = []

    async def approve(q):
        attempts.append(q["rcept_no"])
        if len(attempts) == 1:
            raise RuntimeError("channel not found")
        return True

    async def go():
        p, _ = make(answers={"A1": UNFAITHFUL}, approve=approve)
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


def test_a_failed_answer_is_recorded_and_shown_without_asking_anyone():
    ledger = Ledger()
    p, log = make(answers={"A1": None}, ledger=ledger)
    asyncio.run(p.run_once(DAY))
    [(_, j)] = ledger.judgments()
    assert j.status == "failed" and log["questions"] == [] and log["notified"] == ["A1"]
    assert p.seen.status("A1") == "auto"


def test_unreachable_model_stops_the_route_and_resumes_later_without_downloading_again():
    ledger = Ledger()
    others = [filing(f"U{i}", OTHER) for i in range(3)]
    answers = {f"U{i}": llm.Unavailable("PC off") for i in range(3)}

    async def go():
        p, log = make(filings=[filing("A1")] + others, answers=answers, ledger=ledger)
        assert await p.run_once(DAY) == ["A1"]             # the watchlist went ahead
        assert log["answered"] == ["A1", "U0"]               # the universe stopped at the first failure
        assert p.seen.status("U0") is None
        answers.clear()                                       # the PC is back
        assert await p.run_once(DAY) == ["U0", "U1", "U2"]
        return log

    log = asyncio.run(go())
    assert log["docs"] == ["A1", "U0", "U1", "U2"]           # U0 continued from its checkpoint
    assert len(ledger.judgments()) == 4


def test_universe_is_judged_in_small_batches(monkeypatch):
    monkeypatch.setattr(pipeline, "UNIVERSE_PER_RUN", 2)
    p, log = make(filings=[filing(f"U{i}", OTHER) for i in range(5)])

    async def go():
        return [await p.run_once(DAY) for _ in range(3)]

    assert asyncio.run(go()) == [["U0", "U1"], ["U2", "U3"], ["U4"]]


def test_universe_off_leaves_other_companies_untouched():
    p, log = make(filings=[filing("A1"), filing("U1", OTHER)], judge_universe=False)
    assert asyncio.run(p.run_once(DAY)) == ["A1"]
    assert p.seen.status("U1") is None and log["docs"] == ["A1"]


def test_finished_filings_leave_no_checkpoints_but_open_questions_keep_theirs():
    async def go():
        never = asyncio.Event()

        async def approve(q):
            await never.wait()
            return True

        p, _ = make(answers={"A2": UNFAITHFUL}, filings=[filing("A1"), filing("A2")], approve=approve)
        await p.run_once(DAY)
        done = await p.app.aget_state(p._cfg("A1"))
        waiting = await p.app.aget_state(p._cfg("A2"))
        for task in list(p._tasks):
            task.cancel()
        await asyncio.gather(*p._tasks, return_exceptions=True)
        return done, waiting

    done, waiting = asyncio.run(go())
    assert not done.values           # recorded in the ledger, so its checkpoints were dropped
    assert waiting.interrupts        # still needs the saved state to resume after the human answers


def test_only_the_part_the_model_reads_is_kept_and_the_prompt_says_so(monkeypatch):
    prompts = []

    async def fake_run(agent, prompt):
        prompts.append(prompt)
        return Answer(summary="요약", p_up=0.5), "local"

    monkeypatch.setattr(llm, "run", fake_run)
    seen_state = {}

    async def notify(state):
        seen_state.update(state)

    p = Pipeline(dart_key="k", watchlist={WATCHED: "테스트전자"}, ledger=Ledger(), seen=SeenStore(),
                 answer=pipeline.llm_answerer(None, None), notify=notify, approve=None,
                 checkpointer=InMemorySaver(), max_chars=10,
                 list_fn=lambda key, bgn, end, filing_type=None: [filing("A1")] if filing_type == "B001" else [],
                 doc_fn=lambda key, rcept_no: "가나다라마바사아자차카타파하")
    asyncio.run(p.run_once(DAY))
    assert seen_state["source_text"] == "가나다라마바사아자차" and seen_state["truncated"]
    assert "원문 (앞부분 10자만 사용):\n가나다라마바사아자차" in prompts[0] and "(005930, 코스피)" in prompts[0]
