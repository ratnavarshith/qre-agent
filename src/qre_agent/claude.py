"""Anthropic Messages API (official SDK) with tool use, behind the same interface as
llm.OpenRouter: it takes the agent's OpenAI-style messages and returns a llm.ChatReply, so it can
take over a run mid-conversation as reliability.Reliable's fallback. The SDK's own retries are
off; Reliable does the retrying."""

import json
import re

import anthropic

from .llm import TIMEOUT_S, ChatReply, ProviderError, retry_after, status_kind
from .spend import api_key

MODEL = "claude-haiku-4-5"  # the fallback model; Anthropic's id for Claude Haiku 4.5
TOOL_ID = re.compile(r"[^a-zA-Z0-9_-]")  # Anthropic tool_use ids allow only these characters


class Claude:
    """`schemas` are tools.SCHEMAS-style tool descriptions; they are sent with every call.
    `sdk` replaces the SDK client (tests)."""

    def __init__(self, schemas=(), timeout=TIMEOUT_S, sdk=None):
        self.tools = [
            {"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
            for s in schemas
        ]
        self.seed = None  # the Messages API takes no seed
        self.sdk = sdk or anthropic.Anthropic(
            api_key=api_key("ANTHROPIC_API_KEY"), max_retries=0, timeout=timeout
        )

    def complete(self, model, messages, max_tokens):
        system, turns = to_anthropic(messages)
        kwargs = {"system": system} if system else {}
        if self.tools:
            kwargs["tools"] = self.tools
        try:
            r = self.sdk.messages.create(
                model=model, max_tokens=max_tokens, messages=turns, **kwargs
            )
        except anthropic.APITimeoutError:
            raise ProviderError("timeout", "Anthropic timed out") from None
        except anthropic.APIStatusError as e:
            raise ProviderError(
                status_kind(e.status_code),
                f"Anthropic HTTP {e.status_code}: {e.message}",
                e.status_code,
                retry_after(e.response.headers.get("retry-after")),
            ) from None
        except anthropic.APIConnectionError as e:
            raise ProviderError("connection", f"Anthropic unreachable: {e}") from None
        return parse(r)


def _tool_id(call_id):
    return TOOL_ID.sub("_", call_id)


def _text(content):
    if isinstance(content, list):  # blocks, e.g. a system prompt marked for caching
        return "".join(b.get("text", "") for b in content)
    return content or ""


def _input(arguments):
    """A tool call's arguments as the object tool_use needs; {} when they were not valid JSON
    (the agent already sent that call's error back as its result)."""
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def to_anthropic(messages):
    """(system prompt, turns) from OpenAI-style messages: tool calls become tool_use blocks, tool
    results tool_result blocks in a user turn, and consecutive user content one user turn."""
    system, turns = [], []
    for m in messages:
        if m["role"] == "system":
            system.append(_text(m["content"]))
            continue
        if m["role"] == "assistant":
            blocks = [{"type": "text", "text": t} for t in [_text(m.get("content"))] if t.strip()]
            blocks += [
                {"type": "tool_use", "id": _tool_id(c["id"]), "name": c["function"]["name"],
                 "input": _input(c["function"]["arguments"])}
                for c in m.get("tool_calls") or []
            ]  # fmt: skip
            turns.append(
                {"role": "assistant", "content": blocks or [{"type": "text", "text": "."}]}
            )
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": _tool_id(m["tool_call_id"]),
                     "content": m["content"]}  # fmt: skip
        else:
            block = {"type": "text", "text": _text(m["content"])}
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"].append(block)
        else:
            turns.append({"role": "user", "content": [block]})
    return "\n\n".join(system), turns


def parse(r):
    """A llm.ChatReply from a Messages API response. input_tokens counts all prompt tokens,
    cache reads and writes included, as for OpenRouter."""
    usage = getattr(r, "usage", None)
    if usage is None:
        raise ProviderError("missing_usage", "Anthropic reply has no usage")
    text = "".join(b.text for b in r.content if b.type == "text")
    message = {"role": "assistant", "content": text}
    calls = [
        {"id": b.id, "type": "function",
         "function": {"name": b.name, "arguments": json.dumps(b.input)}}
        for b in r.content if b.type == "tool_use"
    ]  # fmt: skip
    if calls:
        message["tool_calls"] = calls
    cached = getattr(usage, "cache_read_input_tokens", None) or 0
    written = getattr(usage, "cache_creation_input_tokens", None) or 0
    return ChatReply(
        text=text,
        input_tokens=usage.input_tokens + cached + written,
        output_tokens=usage.output_tokens,
        cached_tokens=cached,
        cache_write_tokens=written,
        message=message,
        finish_reason=r.stop_reason,
    )
