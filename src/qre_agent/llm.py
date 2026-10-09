"""OpenRouter chat completions (OpenAI-compatible) with tool calling, as a spend.Guard client.
OpenRouter reports what it billed for each call (usage.cost); the guard logs that next to the
budgets.yaml estimate and warns when they disagree."""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .spend import Reply, api_key

URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 180
CACHE_CONTROL = {"type": "ephemeral"}


@dataclass(frozen=True)
class ChatReply(Reply):
    message: dict = field(default_factory=dict)  # the assistant message, to send back next turn
    finish_reason: str | None = None
    reasoning_tokens: int = 0  # part of output_tokens

    @property
    def tool_calls(self):
        return self.message.get("tool_calls") or []


class OpenRouter:
    """`schemas` are tools.SCHEMAS-style tool descriptions; they are sent with every call. With
    `cache`, calls to anthropic/ models mark the system prompt and the last tool definition with
    cache_control, so Anthropic caches the tools and system prompt (its prefix order is tools,
    system, messages); other models cache on their own or not at all."""

    def __init__(self, schemas=(), seed=None, url=URL, timeout=TIMEOUT_S, cache=False):
        api_key("OPENROUTER_API_KEY")  # fail here, before any call, on a missing or malformed key
        self.tools = [{"type": "function", "function": s} for s in schemas]
        self.seed, self.url, self.timeout, self.cache = seed, url, timeout, cache

    def body(self, model, messages, max_tokens):
        tools = self.tools
        if self.cache and model.startswith("anthropic/"):
            messages, tools = mark_cacheable(messages, tools)
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "usage": {"include": True},  # usage accounting: the cost OpenRouter billed
        }
        if tools:
            body["tools"] = tools
        if self.seed is not None:
            body["seed"] = self.seed
        return body

    def complete(self, model, messages, max_tokens):
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.body(model, messages, max_tokens)).encode(),
            headers={
                "Authorization": f"Bearer {api_key('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                return parse(json.load(r))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"OpenRouter HTTP {e.code}: {e.read().decode(errors='replace')}"
            ) from None


class Pacer:
    """Keeps calls at least 60 / max_rpm seconds apart (so at most max_rpm in any minute), for
    providers that rate-limit an account. One pacer is shared by every client of a model. This
    is not a retry: a failed call still fails. `waited` is the total time slept."""

    def __init__(self, max_rpm, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = 60 / max_rpm, clock, sleep
        self.last, self.waited = None, 0.0

    def wait(self):
        """Sleeps until the next call may go; returns the seconds slept."""
        wait = 0.0 if self.last is None else max(0.0, self.last + self.interval - self.clock())
        if wait:
            self.sleep(wait)
            self.waited += wait
        self.last = self.clock()
        return wait


class Paced:
    """A client whose calls wait for the pacer first; other attributes (tools, seed) come from
    the wrapped client."""

    def __init__(self, client, pacer):
        self.client, self.pacer = client, pacer

    def __getattr__(self, name):
        return getattr(self.client, name)

    def complete(self, model, messages, max_tokens):
        self.pacer.wait()
        return self.client.complete(model, messages, max_tokens)


def mark_cacheable(messages, tools):
    """Copies of messages and tools with cache_control on the system message's text and on the
    last tool. The originals are left alone: the agent keeps and re-sends them."""
    messages = [
        {**m, "content": [{"type": "text", "text": m["content"], "cache_control": CACHE_CONTROL}]}
        if m["role"] == "system" and isinstance(m["content"], str)
        else m
        for m in messages
    ]
    if tools:
        tools = [*tools[:-1], {**tools[-1], "cache_control": CACHE_CONTROL}]
    return messages, tools


def parse(data):
    """Normalize a chat completion: input_tokens counts all prompt tokens, cached included."""
    if data.get("error"):
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    choice, usage = data["choices"][0], data["usage"]
    m = choice["message"]
    message = {"role": "assistant", "content": m.get("content") or ""}
    for key in ("tool_calls", "reasoning_details"):  # reasoning_details keeps thinking across turns
        if m.get(key):
            message[key] = m[key]
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    return ChatReply(
        text=message["content"],
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        cached_tokens=prompt.get("cached_tokens") or 0,
        cache_write_tokens=prompt.get("cache_write_tokens") or 0,
        reported_cost_usd=usage.get("cost"),
        message=message,
        finish_reason=choice.get("finish_reason"),
        reasoning_tokens=completion.get("reasoning_tokens") or 0,
    )
