import json

import pytest
import yaml

from qre_agent.spend import BudgetError, Guard, Reply, read_log

KEY = "sk-test-0123456789abcdef"
PRICES = {"cheap": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}  # $ per 1M tokens


class FakeClient:
    def __init__(self):
        self.reply = Reply("ok", 1000, 500, 200)
        self.calls = []
        self.api_key = KEY  # a real client holds its key; the guard must never touch it

    def complete(self, model, messages, max_tokens):
        self.calls.append((model, messages, max_tokens))
        return self.reply


@pytest.fixture
def guard(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(yaml.safe_dump({"caps": {"dev": 0.01, "eval": 1}, "prices": PRICES}))
    return Guard("dev", budgets, tmp_path / "spend.jsonl")


def test_call_is_logged_with_tokens_and_cost(guard):
    client = FakeClient()
    assert guard.complete(client, "cheap", [{"role": "user", "content": "hi"}], 600).text == "ok"
    (record,) = read_log(guard.log_path)
    assert (record["phase"], record["model"], record["status"]) == ("dev", "cheap", "ok")
    assert (record["input_tokens"], record["output_tokens"], record["cached_tokens"]) == (
        1000,
        500,
        200,
    )
    # 800 fresh * 1.0 + 200 cached * 0.1 + 500 out * 2.0, per 1M
    assert record["cost_usd"] == pytest.approx((800 + 20 + 1000) / 1e6)
    assert guard.remaining() == pytest.approx(0.01 - 1820 / 1e6)


def test_call_that_could_exceed_the_cap_is_refused_before_sending(guard):
    client = FakeClient()
    with pytest.raises(BudgetError, match="exceeds the remaining"):
        guard.complete(client, "cheap", [{"role": "user", "content": "hi"}], 10_000)  # $0.02 worst
    assert client.calls == []
    assert read_log(guard.log_path) == []


def test_failed_call_is_logged_at_its_worst_case_and_counts_toward_the_cap(guard):
    class Failing:
        def complete(self, model, messages, max_tokens):
            raise ConnectionError("dropped")

    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(ConnectionError):
        guard.complete(Failing(), "cheap", messages, 1000)
    (record,) = read_log(guard.log_path)
    prompt_tokens = len(json.dumps(messages))
    assert record["status"] == "failed"
    assert (record["input_tokens"], record["output_tokens"]) == (prompt_tokens, 1000)
    worst = (prompt_tokens * 1.0 + 1000 * 2.0) / 1e6
    assert record["cost_usd"] == pytest.approx(worst)
    assert guard.remaining() == pytest.approx(0.01 - worst)


def test_cap_counts_earlier_spend_and_only_this_phase(guard, tmp_path):
    other = Guard("eval", tmp_path / "budgets.yaml", guard.log_path)
    for _ in range(3):  # each call costs 0.00182 of the 0.01 cap
        guard.complete(FakeClient(), "cheap", [], 1000)
    other.complete(FakeClient(), "cheap", [], 1000)  # eval is unaffected
    assert guard.remaining() == pytest.approx(0.01 - 3 * 1820 / 1e6)
    with pytest.raises(BudgetError):
        guard.complete(FakeClient(), "cheap", [], 4000)  # worst case $0.008 > 0.00454 left
    assert len(read_log(guard.log_path)) == 4


def test_unknown_model_and_unknown_phase_are_refused(guard, tmp_path):
    client = FakeClient()
    with pytest.raises(BudgetError, match="no price"):
        guard.complete(client, "unpriced", [], 10)
    assert client.calls == []
    with pytest.raises(BudgetError, match="unknown phase"):
        Guard("nope", tmp_path / "budgets.yaml", guard.log_path)


def test_phase_defaults_to_the_environment(monkeypatch, tmp_path, guard):
    monkeypatch.setenv("QRE_PHASE", "eval")
    assert Guard(budgets_path=tmp_path / "budgets.yaml").phase == "eval"


def test_key_never_appears_in_the_log_or_the_output(guard, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    guard.complete(FakeClient(), "cheap", [{"role": "user", "content": "hi"}], 600)
    with pytest.raises(BudgetError) as refused:
        guard.complete(FakeClient(), "cheap", [], 10_000)
    with pytest.raises(BudgetError) as unpriced:
        guard.complete(FakeClient(), "unpriced", [], 10)
    out = capsys.readouterr()
    lines = guard.log_path.read_text(encoding="utf-8").splitlines()
    assert lines and all(json.loads(line) for line in lines)
    for text in (*lines, str(refused.value), str(unpriced.value), out.out, out.err):
        assert KEY not in text


def test_default_budgets_file_is_consistent():
    from qre_agent.spend import BUDGETS_PATH

    budgets = yaml.safe_load(BUDGETS_PATH.read_text(encoding="utf-8"))
    assert budgets["caps"] == {"dev": 10, "eval": 25, "phase3": 12, "ci": 3, "demo": 5}
    assert sum(budgets["caps"].values()) == 55
