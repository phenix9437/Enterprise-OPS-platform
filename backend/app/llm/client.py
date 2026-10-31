"""Thin abstraction over the LLM provider.

The rest of the app only knows `LLMClient.parse(...)`. Swapping providers, or using a fake in
tests, means implementing this one method.
"""
import time
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from app.core.config import get_settings

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Any failure to get a usable structured answer from the model."""


@dataclass
class LLMUsage:
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    usage: LLMUsage


class LLMClient(Protocol):
    async def parse(self, messages: list[dict], response_model: type[T]) -> LLMResult[T]: ...


class OpenAIClient:
    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)
        self._model = model

    async def parse(self, messages: list[dict], response_model: type[T]) -> LLMResult[T]:
        start = time.perf_counter()
        try:
            completion = await self._client.chat.completions.parse(
                model=self._model, messages=messages, response_format=response_model
            )
        except APIError as exc:  # includes timeouts, rate limits, connection and status errors
            raise LLMError(f"OpenAI request failed: {exc.__class__.__name__}: {exc}") from exc

        message = completion.choices[0].message
        if message.refusal:
            raise LLMError(f"Model refused the request: {message.refusal}")
        if message.parsed is None:
            raise LLMError("Model returned no parsable structured output")

        usage = completion.usage
        return LLMResult(
            parsed=message.parsed,
            usage=LLMUsage(
                model=completion.model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
            ),
        )


_client: OpenAIClient | None = None


def get_llm_client() -> LLMClient:
    """FastAPI dependency. Tests override this with a fake."""
    global _client
    s = get_settings()
    if not s.openai_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "LLM is not configured (OPENAI_API_KEY missing)")
    if _client is None:
        _client = OpenAIClient(s.openai_api_key, s.openai_model, s.llm_timeout_seconds, s.llm_sdk_max_retries)
    return _client
