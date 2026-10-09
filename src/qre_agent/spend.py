"""Every LLM call goes through `Guard.complete`: it refuses a call that could push the active
phase over its cap in budgets.yaml, and logs model, tokens and cost of each call to
runs/spend.jsonl.

A client is anything with `complete(model, messages, max_tokens) -> Reply`. A client that sends
tool schemas with every call exposes them as `client.tools`, so the worst case counts them.
"""

import json
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
BUDGETS_PATH = REPO_ROOT / "budgets.yaml"
LOG_PATH = REPO_ROOT / "runs" / "spend.jsonl"
PER_TOKENS = 1_000_000
KEY_PREFIXES = {"OPENROUTER_API_KEY": "sk-or-", "ANTHROPIC_API_KEY": "sk-ant-"}
COST_MISMATCH = 0.2  # warn when the provider's cost is off ours by more than this fraction


class BudgetError(Exception):
    """The call was refused; nothing was sent."""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int  # all prompt tokens, cached ones included
    output_tokens: int
    cached_tokens: int = 0
    reported_cost_usd: float | None = None  # what the provider says it billed, if it says


def load_keys():
    """Put the keys from .env into the environment (existing variables win)."""
    load_dotenv(REPO_ROOT / ".env")


def api_key(name):
    """The key from .env, after checking its prefix. Errors name the variable, never the value."""
    load_keys()
    key = os.environ.get(name, "")
    if not key.startswith(KEY_PREFIXES[name]):
        raise RuntimeError(
            f"{name} is missing or malformed: it must start with {KEY_PREFIXES[name]!r} (check .env)"
        )
    return key


def cost(price, input_tokens, output_tokens, cached_tokens=0):
    cached_price = price.get("cache_read", price["input"])
    fresh = input_tokens - cached_tokens
    return (
        fresh * price["input"] + cached_tokens * cached_price + output_tokens * price["output"]
    ) / PER_TOKENS


def read_log(path=LOG_PATH):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def spent(records, phase):
    return sum(r["cost_usd"] for r in records if r["phase"] == phase)


class Guard:
    def __init__(self, phase=None, budgets_path=BUDGETS_PATH, log_path=LOG_PATH):
        """`phase` defaults to the QRE_PHASE environment variable."""
        budgets = yaml.safe_load(Path(budgets_path).read_text(encoding="utf-8"))
        self.phase = phase or os.environ.get("QRE_PHASE")
        if self.phase not in budgets["caps"]:
            raise BudgetError(
                f"unknown phase {self.phase!r}, expected one of {list(budgets['caps'])}"
            )
        self.cap = budgets["caps"][self.phase]
        self.prices = budgets["prices"]
        self.log_path = Path(log_path)

    def remaining(self):
        return self.cap - spent(read_log(self.log_path), self.phase)

    def complete(self, client, model, messages, max_tokens):
        if model not in self.prices:
            raise BudgetError(f"no price for model {model!r} in budgets.yaml")
        price = self.prices[model]
        # Worst case: every character of the prompt is a token, and the reply fills max_tokens.
        tools = getattr(client, "tools", None)
        prompt_tokens = len(json.dumps(messages)) + (len(json.dumps(tools)) if tools else 0)
        worst = cost(price, prompt_tokens, max_tokens)
        if worst > self.remaining():
            raise BudgetError(
                f"phase {self.phase}: worst case ${worst:.4f} exceeds the remaining "
                f"${self.remaining():.4f} of the ${self.cap} cap"
            )
        try:
            reply = client.complete(model=model, messages=messages, max_tokens=max_tokens)
        except Exception:
            # The request may have been billed, so it counts at its worst case.
            self._log(model, "failed", prompt_tokens, max_tokens, 0, worst)
            raise
        usd = cost(price, reply.input_tokens, reply.output_tokens, reply.cached_tokens)
        reported = reply.reported_cost_usd
        if reported is not None and abs(reported - usd) > COST_MISMATCH * usd:
            warnings.warn(
                f"{model}: provider reports ${reported:.6f}, budgets.yaml prices give ${usd:.6f}",
                stacklevel=2,
            )
        self._log(
            model, "ok", reply.input_tokens, reply.output_tokens, reply.cached_tokens, usd, reported
        )
        return reply

    def _log(self, model, status, input_tokens, output_tokens, cached_tokens, usd, reported=None):
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "phase": self.phase,
            "model": model,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": usd,
            "reported_cost_usd": reported,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
