"""Talks to the model through any OpenAI-compatible endpoint (Groq by default, Ollama for local)."""

from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from typing import Callable, Iterator

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

from excel_qa.logger import log

load_dotenv()

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
MAX_RATE_LIMIT_WAIT_SECONDS = 25

# The UI sets this to show "continuing in Ns" while we wait out a rate limit.
on_rate_limit_wait: Callable[[int], None] | None = None


def main_model_name() -> str:
    """The model that writes SQL and words the answers (LLM_MODEL)."""
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def helper_model_name() -> str:
    """Optional second model for picking tables and suggesting questions (LLM_SELECT_MODEL)."""
    return os.getenv("LLM_SELECT_MODEL") or main_model_name()


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key or api_key.startswith("gsk_your"):
        raise RuntimeError("No API key found. Copy .env.example to .env and set GROQ_API_KEY.")
    # max_retries=0 because rate-limit waits are handled below, where the UI can show them
    return OpenAI(base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL), api_key=api_key, timeout=30, max_retries=0)


def _call_model(**request):
    """One API call. On a rate limit, waits the time the provider asks for and tries again."""
    for attempt in range(3):
        try:
            return get_client().chat.completions.create(**request)
        except RateLimitError as error:
            retry_after = error.response.headers.get("retry-after") if error.response is not None else None
            wait_seconds = int(float(retry_after)) + 1 if retry_after else 10
            if attempt == 2 or wait_seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
                raise
            log.info("RATE LIMIT  %s asks to wait %ss, waiting", request.get("model"), wait_seconds)
            if on_rate_limit_wait:
                on_rate_limit_wait(wait_seconds)
            time.sleep(wait_seconds)


_THINKING_BLOCK = re.compile(r"<think>.*?</think>", re.S)


def remove_thinking(text: str) -> str:
    """Reasoning models may put <think>...</think> before the answer."""
    text = _THINKING_BLOCK.sub("", text)
    return text.split("<think>")[0].strip()              # also handles a block cut off by max_tokens


def ask_model(messages: list[dict], temperature: float = 0.0, max_tokens: int = 700,
              model: str | None = None, **extra) -> str:
    """One complete reply. max_tokens is tight because providers count it against the rate limit."""
    response = _call_model(model=model or main_model_name(), messages=messages,
                           temperature=temperature, max_tokens=max_tokens, **extra)
    return remove_thinking(response.choices[0].message.content or "")


def ask_helper_model(messages: list[dict], max_tokens: int = 250, temperature: float = 0.0) -> str:
    """A call to the helper model, with low reasoning effort if LLM_SELECT_REASONING is set."""
    extra = {}
    if os.getenv("LLM_SELECT_MODEL") and os.getenv("LLM_SELECT_REASONING"):
        extra["reasoning_effort"] = os.getenv("LLM_SELECT_REASONING")
    return ask_model(messages, temperature, max_tokens, model=helper_model_name(), **extra)


def stream_model_reply(messages: list[dict], temperature: float = 0.0, max_tokens: int = 300) -> Iterator[str]:
    """Yields the reply piece by piece so the UI can type it out. <think> blocks are held back."""
    stream = _call_model(model=main_model_name(), messages=messages,
                         temperature=temperature, max_tokens=max_tokens, stream=True)
    pending, inside_thinking = "", False
    for chunk in stream:
        piece = chunk.choices[0].delta.content if chunk.choices else None
        if not piece:
            continue
        pending += piece
        if not inside_thinking and pending.lstrip().startswith("<think>"):
            inside_thinking = True
        if inside_thinking:
            if "</think>" not in pending:
                continue
            pending = pending.split("</think>", 1)[1].lstrip()
            inside_thinking = False
        # "<thi" could be the start of a tag that is still arriving, so hold it for now
        if pending and not "<think>".startswith(pending.lstrip()[:7]):
            yield pending
            pending = ""
    if pending and not inside_thinking:
        yield pending
