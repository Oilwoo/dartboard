"""The real fallback chain against fake OpenAI-compatible servers (no network, no keys)."""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from dartboard import llm
from dartboard.graph import FilingSummary

FAKE = os.path.join(os.path.dirname(__file__), "support", "fake_openai.py")
ARGS = json.dumps({"summary": "요약", "stance": "hold", "confidence": 0.5, "horizon_days": 20})


@pytest.fixture(scope="module")
def servers():
    procs = [subprocess.Popen([sys.executable, FAKE, port, mode, name, ARGS])
             for port, mode, name in [("18502", "429", "groq"), ("18503", "ok", "gemini"), ("18504", "ok", "vllm")]]
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


def run():
    agent = llm.build_agent(FilingSummary, "Summarize the filing.")
    t = time.perf_counter()
    out = asyncio.run(llm.run(agent, "공시 원문"))
    return out, time.perf_counter() - t


def test_pc_on_uses_local_vllm(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18504/v1")
    out, _ = run()
    assert out.summary.endswith("[vllm]")


def test_pc_off_and_groq_rate_limited_falls_to_gemini_fast(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18501/v1")  # nothing listening = PC off
    out, secs = run()
    assert out.summary.endswith("[gemini]")
    assert secs < 3, secs  # with library defaults this path took ~61 s


def test_everything_down_returns_none(servers, monkeypatch):
    configure(monkeypatch, "http://127.0.0.1:18501/v1", gemini_url="http://127.0.0.1:18509/v1")
    out, _ = run()
    assert out is None


def test_nothing_configured(monkeypatch):
    for envs in llm.PROVIDERS.values():
        for e in envs:
            monkeypatch.delenv(e, raising=False)
    assert llm.build_agent(FilingSummary, "x") is None
