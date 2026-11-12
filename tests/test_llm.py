"""The real fallback chain against fake OpenAI-compatible servers (no network, no keys)."""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from dartboard import llm
from dartboard.graph import Answer

FAKE = os.path.join(os.path.dirname(__file__), "support", "fake_openai.py")
ARGS = json.dumps({"summary": "요약", "p_up": 0.55})
BAD_ARGS = json.dumps({"summary": "요약", "p_up": 1.7})  # not a probability


@pytest.fixture(scope="module")
def servers():
    procs = [subprocess.Popen([sys.executable, FAKE, port, mode, name, args])
             for port, mode, name, args in [("18502", "429", "groq", ARGS), ("18503", "ok", "gemini", ARGS),
                                            ("18504", "ok", "vllm", ARGS), ("18505", "ok", "bad", BAD_ARGS)]]
    time.sleep(1.5)
    yield
    for p in procs:
        p.terminate()


def configure(monkeypatch, local_url, gemini_url="http://127.0.0.1:18503/v1"):
    monkeypatch.setenv("LLM_ORDER", "local,groq,gemini")
    monkeypatch.setenv("LOCAL_LLM_URL", local_url)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen")
    monkeypatch.setenv("GROQ_URL", "http://127.0.0.1:18502/v1")
    monkeypatch.setenv("GROQ_MODEL", "gpt-oss")
    monkeypatch.setenv("GEMINI_URL", gemini_url)
    monkeypatch.setenv("GEMINI_MODEL", "flash")


def run(order=None):
    agent = llm.build_agent(Answer, "Summarize the filing.", order)
    t = time.perf_counter()
    out = asyncio.run(llm.run(agent, "공시 원문"))
    return out, time.perf_counter() - t


def test_pc_on_uses_local_vllm(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18504/v1")
    (summary, model), _ = run()
    assert summary.summary.endswith("[vllm]") and model == "qwen"


def test_pc_off_and_groq_rate_limited_falls_to_gemini_fast(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18501/v1")  # nothing listening = PC off
    (summary, model), secs = run()
    assert summary.summary.endswith("[gemini]") and model == "flash"  # the ledger must name the fallback
    assert secs < 3, secs  # with library defaults this path took ~61 s


def test_everything_down_raises_unavailable_so_the_filing_is_tried_later(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18501/v1", gemini_url="http://127.0.0.1:18509/v1")
    with pytest.raises(llm.Unavailable):
        run()


def test_an_answer_that_never_validates_is_a_failed_judgment(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18505/v1")
    out, _ = run()
    assert out is None


def test_the_universe_route_uses_only_the_local_model(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18501/v1")  # PC off; Gemini would answer, but must not be asked
    with pytest.raises(llm.Unavailable):
        run("local")
    assert llm.configured("local,gemini") == ["local", "gemini"]


def test_nothing_configured(monkeypatch):
    for envs in llm.PROVIDERS.values():
        for e in envs:
            monkeypatch.delenv(e, raising=False)
    assert llm.build_agent(Answer, "x") is None
    assert llm.configured("local,groq,gemini") == []
    with pytest.raises(llm.Unavailable):
        asyncio.run(llm.run(None, "공시"))


def test_unknown_provider_name_is_an_error():
    with pytest.raises(ValueError):
        llm.configured("local,openai")
