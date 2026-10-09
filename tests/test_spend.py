import json

import pytest
import yaml

from qre_agent.spend import BudgetError, Guard, Reply, cost, read_log

KEY = "sk-test-0123456789abcdef"
PRICES = {"cheap": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}  # $ per 1M tokens


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


def test_cached_tokens_are_billed_at_the_input_price_without_a_cache_read_price():
    assert cost({"input": 1.0, "output": 2.0}, 1000, 500, 200) == pytest.approx(2000 / 1e6)
    assert cost({"input": 1.0, "cache_read": 0.1, "output": 2.0}, 1000, 500, 200) == pytest.approx(
        1820 / 1e6
    )


def test_every_default_price_is_complete_and_cache_reads_are_not_dearer_than_input():
    from qre_agent.spend import BUDGETS_PATH

    prices = yaml.safe_load(BUDGETS_PATH.read_text(encoding="utf-8"))["prices"]
    assert prices
    for model, p in prices.items():
        assert set(p) == {"input", "output", "cache_read"}, model
        assert 0 < p["cache_read"] <= p["input"] < p["output"], model


def test_worst_case_counts_the_tool_schemas_the_client_sends(guard):
    client = FakeClient()
    client.tools = [{"type": "function", "function": {"description": "x" * 20_000}}]
    with pytest.raises(BudgetError, match="exceeds the remaining"):
        guard.complete(client, "cheap", [], 1000)  # $0.002 without the schemas, $0.022 with
    assert client.calls == []


def test_reported_cost_is_logged_next_to_ours_and_a_mismatch_warns(guard):
    client = FakeClient()
    ours = 1820 / 1e6
    client.reply = Reply("ok", 1000, 500, 200, reported_cost_usd=ours * 1.1)
    guard.complete(client, "cheap", [], 600)
    client.reply = Reply("ok", 1000, 500, 200, reported_cost_usd=ours * 1.3)
    with pytest.warns(UserWarning, match="provider reports"):
        guard.complete(client, "cheap", [], 600)
    within, off = read_log(guard.log_path)
    assert within["cost_usd"] == pytest.approx(ours)
    assert within["reported_cost_usd"] == pytest.approx(ours * 1.1)
    assert off["reported_cost_usd"] == pytest.approx(ours * 1.3)


@pytest.mark.parametrize(
    "name, value",
    [
        ("OPENROUTER_API_KEY", f"<{KEY.replace('sk-test', 'sk-or-v1')}>"),
        ("OPENROUTER_API_KEY", "sk-ant-api03-0123456789"),
        ("ANTHROPIC_API_KEY", "sk-or-v1-0123456789"),
        ("ANTHROPIC_API_KEY", ""),
    ],
)
def test_malformed_keys_are_refused_without_showing_them(monkeypatch, name, value):
    from qre_agent.spend import api_key

    monkeypatch.setenv(name, value)
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    with pytest.raises(RuntimeError, match=name) as refused:
        api_key(name)
    assert not value or value not in str(refused.value)


def test_well_formed_keys_are_returned(monkeypatch):
    from qre_agent.spend import api_key

    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-0123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-0123456789")
    assert api_key("OPENROUTER_API_KEY") == "sk-or-v1-0123456789"
    assert api_key("ANTHROPIC_API_KEY") == "sk-ant-api03-0123456789"
