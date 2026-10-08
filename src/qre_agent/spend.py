"""Every LLM call goes through `Guard.complete`: it refuses a call that could push the active
phase over its cap in budgets.yaml, and logs model, tokens and cost of each call to
runs/spend.jsonl.

A client is anything with `complete(model, messages, max_tokens) -> Reply`.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
BUDGETS_PATH = REPO_ROOT / "budgets.yaml"
LOG_PATH = REPO_ROOT / "runs" / "spend.jsonl"
PER_TOKENS = 1_000_000


class BudgetError(Exception):
    """The call was refused; nothing was sent."""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int  # all prompt tokens, cached ones included
    output_tokens: int
    cached_tokens: int = 0


def load_keys():
    """Put the keys from .env into the environment (existing variables win)."""
    load_dotenv(REPO_ROOT / ".env")


def cost(price, input_tokens, output_tokens, cached_tokens=0):
    cached_price = price.get("cached_input", price["input"])
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
        prompt_tokens = len(json.dumps(messages))
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
        self._log(model, "ok", reply.input_tokens, reply.output_tokens, reply.cached_tokens, usd)
        return reply

    def _log(self, model, status, input_tokens, output_tokens, cached_tokens, usd):
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "phase": self.phase,
            "model": model,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": usd,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
