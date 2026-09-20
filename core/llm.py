"""
llm.py - one thin client for any OpenAI-compatible endpoint.

Groq, OpenRouter, Together, and a local Ollama all speak the OpenAI chat
protocol, so switching provider is just two environment variables:

    LLM_BASE_URL   e.g. https://api.groq.com/openai/v1  or  http://localhost:11434/v1
    LLM_MODEL      e.g. qwen/qwen3.8-27b                or  qwen2.5-coder:7b
    GROQ_API_KEY   the key (any non-empty string for Ollama)

    LLM_SELECT_MODEL   optional. A second, smaller model used only to pick
                       relevant tables. Hosted providers rate-limit per model,
                       so this gives the main model its full budget for SQL.
    LLM_SELECT_REASONING   optional, e.g. "low". Some small models "think" before
                       answering and burn their token allowance on it; this is
                       passed as reasoning_effort for the select model only.

No LangChain: the app makes three kinds of call (select tables, write SQL,
narrate a result) and a direct client is easier to read and debug.
"""

from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from typing import Callable, Iterator

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

from core import log

load_dotenv()

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"


def model_name() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def select_model_name() -> str:
    return os.getenv("LLM_SELECT_MODEL") or model_name()


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key or api_key.startswith("gsk_your"):
        raise RuntimeError(
            "No API key found. Copy .env.example to .env and set GROQ_API_KEY."
        )
    # the library default is a 10 minute timeout, far too long for a chat UI
    return OpenAI(
        base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        api_key=api_key,
        timeout=30,
        max_retries=0,          # rate-limit waits are handled below, where the UI can show them
    )


# Free tiers limit tokens per minute. When the provider says "retry in N seconds"
# we wait and try again instead of failing, as long as N is reasonable.
MAX_RATE_LIMIT_WAIT = 25
on_wait: Callable[[int], None] | None = None      # the UI sets this to show "waiting Ns"


def _create(**kwargs):
    for attempt in range(3):
        try:
            return get_client().chat.completions.create(**kwargs)
        except RateLimitError as e:
            header = e.response.headers.get("retry-after") if e.response is not None else None
            wait = int(float(header)) + 1 if header else 10
            if attempt == 2 or wait > MAX_RATE_LIMIT_WAIT:
                raise
            log.info("RATE LIMIT  %s asks to wait %ss, waiting", kwargs.get("model"), wait)
            if on_wait:
                on_wait(wait)
            time.sleep(wait)


_THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_thinking(text: str) -> str:
    """Reasoning models (Qwen 3, etc.) may put <think>...</think> before the answer."""
    text = _THINK.sub("", text)
    if "<think>" in text:                 # cut off mid-thought by max_tokens
        text = text.split("<think>")[0]
    return text.strip()


def select_chat(messages: list[dict], max_tokens: int = 250, temperature: float = 0.0) -> str:
    """A call to the helper model (table picking, question suggestions)."""
    extra = {}
    if os.getenv("LLM_SELECT_MODEL") and os.getenv("LLM_SELECT_REASONING"):
        extra["reasoning_effort"] = os.getenv("LLM_SELECT_REASONING")
    return chat(messages, temperature=temperature, max_tokens=max_tokens, model=select_model_name(), **extra)


def chat(messages: list[dict], temperature: float = 0.0, max_tokens: int = 700, model: str | None = None, **extra) -> str:
    """
    Single completion. temperature=0 keeps SQL generation deterministic.
    max_tokens is kept tight on purpose: hosted providers count the reserved
    answer length against the tokens-per-minute limit, not just what is used.
    """
    resp = _create(
        model=model or model_name(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )
    return strip_thinking(resp.choices[0].message.content or "")


def chat_stream(messages: list[dict], temperature: float = 0.2, max_tokens: int = 300) -> Iterator[str]:
    """Streaming completion, used for the final answer so the UI types it out."""
    stream = _create(
        model=model_name(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    # hold text back while inside a <think> block, pass everything else through
    buffer, thinking = "", False
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if not delta:
            continue
        buffer += delta
        if not thinking and buffer.lstrip().startswith("<think>"):
            thinking = True
        if thinking:
            if "</think>" in buffer:
                buffer = buffer.split("</think>", 1)[1].lstrip()
                thinking = False
            else:
                continue
        # "<thi" might be the start of a tag that is still arriving
        if buffer and not "<think>".startswith(buffer.lstrip()[:7]):
            yield buffer
            buffer = ""
    if buffer and not thinking:
        yield buffer
