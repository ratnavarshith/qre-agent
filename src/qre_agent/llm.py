"""OpenRouter chat completions (OpenAI-compatible) with tool calling, as a spend.Guard client.
OpenRouter reports what it billed for each call (usage.cost); the guard logs that next to the
budgets.yaml estimate and warns when they disagree."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .spend import Reply, api_key

URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 180


@dataclass(frozen=True)
class ChatReply(Reply):
    message: dict = field(default_factory=dict)  # the assistant message, to send back next turn
    finish_reason: str | None = None
    reasoning_tokens: int = 0  # part of output_tokens

    @property
    def tool_calls(self):
        return self.message.get("tool_calls") or []


class OpenRouter:
    """`schemas` are tools.SCHEMAS-style tool descriptions; they are sent with every call."""

    def __init__(self, schemas=(), seed=None, url=URL, timeout=TIMEOUT_S):
        api_key("OPENROUTER_API_KEY")  # fail here, before any call, on a missing or malformed key
        self.tools = [{"type": "function", "function": s} for s in schemas]
        self.seed, self.url, self.timeout = seed, url, timeout

    def body(self, model, messages, max_tokens):
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "usage": {"include": True},  # usage accounting: the cost OpenRouter billed
        }
        if self.tools:
            body["tools"] = self.tools
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
        reported_cost_usd=usage.get("cost"),
        message=message,
        finish_reason=choice.get("finish_reason"),
        reasoning_tokens=completion.get("reasoning_tokens") or 0,
    )
