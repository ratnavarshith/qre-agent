"""Fault injection for the Phase 3 reliability experiment. `Injected` sits between the agent (or
reliability.Reliable) and spend.Guard, with the guard's interface. See docs/agent-design.md."""

import random
from collections import Counter

from opentelemetry import trace

from .llm import ChatReply, ProviderError

KINDS = ("timeout", "rate_limit", "server", "malformed_json")
RETRY_AFTER_S = 1.0  # the Retry-After an injected 429 carries
# A tool call cut off mid-argument, as when a reply runs out of tokens.
MALFORMED = '{"code": "from qiskit import QuantumCircuit\\ncircuit = QuantumCircuit('


class InjectedFault(ProviderError):
    """A fault made up by Injected; the provider never saw the call."""


class Injected:
    """Before each call, with probability `rate`, injects one fault of a kind drawn uniformly from
    `kinds` instead of calling: a timeout, a 429 (with Retry-After), a 5xx, or a reply whose tool
    call has malformed JSON arguments. An injected fault never reaches the guard or the provider,
    so it costs nothing and is not in the spend log. The draws come from random.Random(seed), so
    a run is reproducible. `counts` has the faults injected by kind, `calls` every call."""

    def __init__(self, guard, rate, seed, kinds=KINDS, retry_after_s=RETRY_AFTER_S):
        if unknown := set(kinds) - set(KINDS):
            raise ValueError(f"unknown fault kinds {sorted(unknown)}, expected some of {KINDS}")
        self.guard, self.rate, self.kinds, self.retry_after_s = guard, rate, kinds, retry_after_s
        self.rng, self.counts, self.calls = random.Random(seed), Counter(), 0

    @property
    def phase(self):
        return self.guard.phase

    @property
    def prices(self):
        return self.guard.prices

    def draw(self):
        """The fault for the next call, or None."""
        return self.rng.choice(self.kinds) if self.rng.random() < self.rate else None

    def complete(self, client, model, messages, max_tokens):
        self.calls += 1
        kind = self.draw()
        if kind is None:
            return self.guard.complete(client, model, messages, max_tokens)
        self.counts[kind] += 1
        trace.get_current_span().add_event("fault_injected", {"fault": kind, "model": model})
        if kind == "malformed_json":
            return malformed_reply(self.calls)
        after = self.retry_after_s if kind == "rate_limit" else None
        raise InjectedFault(kind, f"injected {kind}", retry_after=after)


def malformed_reply(n):
    """A free reply that calls build_circuit with arguments that are not valid JSON."""
    call = {"id": f"injected_{n}", "type": "function",
            "function": {"name": "build_circuit", "arguments": MALFORMED}}  # fmt: skip
    message = {"role": "assistant", "content": "", "tool_calls": [call]}
    return ChatReply("", 0, 0, message=message, finish_reason="tool_calls",
                     faults=("malformed_json",))  # fmt: skip
