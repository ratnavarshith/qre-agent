"""Retries and a fallback provider around spend.Guard. `Reliable` has the guard's interface
(`complete(client, model, messages, max_tokens)`, `phase`, `prices`), so the agent uses either.
Every attempt goes through a guard, so each one is budget-checked and a failed one is logged at
its worst case, as without retries. See docs/agent-design.md."""

import json
import random
import time
from dataclasses import replace

from opentelemetry import trace

from .llm import ProviderError

MAX_RETRIES = 3
BASE_S, CAP_S = 1.0, 30.0  # backoff: up to BASE_S * 2^attempt, at most CAP_S


def backoff(attempt, rng, base=BASE_S, cap=CAP_S):
    """Exponential backoff with full jitter: uniform in [0, min(cap, base * 2^attempt)]."""
    return rng.uniform(0, min(cap, base * 2**attempt))


def malformed_calls(reply):
    """The tool calls in a reply whose arguments are not valid JSON."""
    bad = []
    for call in getattr(reply, "message", {}).get("tool_calls") or []:
        try:
            json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            bad.append(call)
    return bad


class Malformed(ProviderError):
    """A reply with a tool call that isn't valid JSON, raised inside Reliable to be retried;
    `reply` is the reply and `model` the model that wrote it."""

    def __init__(self, reply, model):
        super().__init__("malformed_json", "tool call arguments are not valid JSON")
        self.reply, self.model = reply, model


class Reliable:
    """Retries timeouts, 429s, 5xx, replies without usage and replies whose tool-call arguments
    are not valid JSON (`malformed_json`; ProviderError.retryable) up to `max_retries` times,
    sleeping a full-jitter backoff or the provider's Retry-After, whichever is longer. When the
    retries are used up, the same messages go to `fallback`, a (guard, client, model) triple, with
    the same retry rule. Other errors are raised at once.

    A malformed reply was a successful, billed call, so each one is in the spend log. If the last
    attempt on the last provider is still malformed, that reply is returned, and the agent sends
    the JSON error back to the model as a tool result, as it does without this layer.

    The reply carries `model` (who answered), `retries` (attempts sent again to the same
    provider), `fallback` and `faults` (the error kinds seen). A final failure re-raises the last
    ProviderError with `retries`, `fallback` and `faults` set on it. Each failed attempt is an
    `llm_retry` event on the current span."""

    def __init__(self, guard, fallback=None, max_retries=MAX_RETRIES, base_s=BASE_S, cap_s=CAP_S,
                 seed=0, sleep=time.sleep):  # fmt: skip
        self.guard, self.fallback, self.max_retries = guard, fallback, max_retries
        self.base_s, self.cap_s = base_s, cap_s
        self.rng, self.sleep = random.Random(seed), sleep
        self.waits = []  # every backoff slept, for tests and the trace

    @property
    def phase(self):
        return self.guard.phase

    @property
    def prices(self):
        return self.guard.prices | (self.fallback[0].prices if self.fallback else {})

    def complete(self, client, model, messages, max_tokens):
        routes = [(self.guard, client, model)] + ([self.fallback] if self.fallback else [])
        retries, faults, span = 0, [], trace.get_current_span()
        for n, (guard, c, m) in enumerate(routes):
            for attempt in range(self.max_retries + 1):
                try:
                    reply = guard.complete(c, m, messages, max_tokens)
                    if malformed_calls(reply):
                        raise Malformed(reply, m)
                except ProviderError as e:
                    e.retries, e.fallback = retries, n > 0
                    if not e.retryable:
                        e.faults = (*faults, e.kind)
                        raise
                    faults.append(e.kind)
                    e.faults = tuple(faults)
                    last = e
                    if attempt == self.max_retries:
                        span.add_event("llm_retry", {"error.type": e.kind, "model": m, "wait_s": 0})
                        break  # on to the fallback, if there is one
                    wait = max(backoff(attempt, self.rng, self.base_s, self.cap_s),
                               e.retry_after or 0)  # fmt: skip
                    span.add_event("llm_retry", {"error.type": e.kind, "model": m, "wait_s": wait})
                    self.waits.append(wait)
                    self.sleep(wait)
                    retries += 1
                    continue
                return replace(reply, model=m, retries=retries, fallback=n > 0,
                               faults=(*faults, *getattr(reply, "faults", ())))  # fmt: skip
        if isinstance(last, Malformed):  # the agent handles it as a tool error
            return replace(last.reply, model=last.model, retries=last.retries,
                           fallback=last.fallback, faults=last.faults)  # fmt: skip
        raise last


def build(guard, settings, fallback=None, seed=0, sleep=time.sleep):
    """The guard behind a Reliable when `settings` (a config's `reliability`) has enabled: true,
    else the guard itself. `fallback` is the (guard, client, model) to fall back to."""
    if not settings or not settings.get("enabled"):
        return guard
    return Reliable(guard, fallback, settings.get("max_retries", MAX_RETRIES),
                    settings.get("base_s", BASE_S), settings.get("cap_s", CAP_S), seed, sleep)  # fmt: skip
