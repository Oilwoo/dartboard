"""LLM fallback chain: local vLLM -> Groq -> Gemini, all through OpenAI-compatible endpoints."""
from __future__ import annotations

import logging
import os

import httpx2  # the HTTP client OpenAI SDK 3.x expects
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

log = logging.getLogger(__name__)

PROVIDERS = {
    "local": ("LOCAL_LLM_URL", "LOCAL_LLM_KEY", "LOCAL_LLM_MODEL"),
    "groq": ("GROQ_URL", "GROQ_API_KEY", "GROQ_MODEL"),
    "gemini": ("GEMINI_URL", "GEMINI_API_KEY", "GEMINI_MODEL"),
}


def _model(name: str) -> OpenAIChatModel | None:
    url_env, key_env, model_env = PROVIDERS[name]
    url, model = os.getenv(url_env), os.getenv(model_env)
    if not (url and model):
        return None
    client = AsyncOpenAI(
        base_url=url,
        api_key=os.getenv(key_env, "none"),
        max_retries=0,  # "retry later" moves on to the next provider instead of waiting
        http_client=httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=1.5)),  # a sleeping PC fails fast
    )
    return OpenAIChatModel(model, provider=OpenAIProvider(openai_client=client))


def build_agent(output_type: type[BaseModel], instructions: str) -> Agent | None:
    """None when no provider is configured."""
    order = os.getenv("LLM_ORDER", "local,groq,gemini").split(",")
    models = [m for m in (_model(name.strip()) for name in order) if m is not None]
    return Agent(FallbackModel(*models), output_type=output_type, instructions=instructions) if models else None


async def run(agent: Agent | None, prompt: str) -> tuple[BaseModel, str] | None:
    """(output, name of the model that answered), or None when every provider failed."""
    if agent is None:
        return None
    try:
        result = await agent.run(prompt)
    except Exception as e:
        log.warning("every LLM provider failed: %r", e)
        return None
    return result.output, result.response.model_name
