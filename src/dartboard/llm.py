"""LLM fallback chain: local vLLM (4090) -> Groq -> Gemini, all through OpenAI-compatible endpoints.

Settings measured before this project started (see docs/verification.md):
  * library defaults: one 429 with Retry-After made a call take 61 s (the SDK retried twice)
  * max_retries=0 and a 1.5 s connect timeout: the same path took 0.03 s, a sleeping PC 1.5 s
"""
from __future__ import annotations

import os
from typing import TypeVar

import httpx2  # OpenAI SDK 3.x and Pydantic AI 2.x use httpx2, not httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

T = TypeVar("T", bound=BaseModel)

# name -> (base URL env, API key env, model env)
PROVIDERS = {
    "local": ("LOCAL_LLM_URL", "LOCAL_LLM_KEY", "LOCAL_LLM_MODEL"),  # vLLM on the 4090 PC, via Tailscale
    "groq": ("GROQ_URL", "GROQ_API_KEY", "GROQ_MODEL"),
    "gemini": ("GEMINI_URL", "GEMINI_API_KEY", "GEMINI_MODEL"),
}


def _model(name: str) -> OpenAIChatModel | None:
    url_env, key_env, model_env = PROVIDERS[name]
    url, model = os.getenv(url_env), os.getenv(model_env)
    if not (url and model):
        return None  # not configured: skip this provider
    client = AsyncOpenAI(
        base_url=url,
        api_key=os.getenv(key_env, "none"),
        max_retries=0,  # never wait out a Retry-After; move on to the next provider
        http_client=httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=1.5)),
    )
    return OpenAIChatModel(model, provider=OpenAIProvider(openai_client=client))


def build_agent(output_type: type[T], instructions: str) -> Agent | None:
    """Returns None when no provider is configured."""
    order = os.getenv("LLM_ORDER", "local,groq,gemini").split(",")
    models = [m for m in (_model(n.strip()) for n in order) if m is not None]
    if not models:
        return None
    return Agent(FallbackModel(*models), output_type=output_type, instructions=instructions)


async def run(agent: Agent | None, prompt: str):
    """Structured output, or None when every provider failed (the caller falls back to rules + a human)."""
    if agent is None:
        return None
    try:
        return (await agent.run(prompt)).output
    except Exception:  # FallbackExceptionGroup when all providers failed
        return None
