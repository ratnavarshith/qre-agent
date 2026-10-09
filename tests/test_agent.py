import json

import pytest
import yaml
from qiskit import QuantumCircuit

from qre_agent.agent import final_answer, run
from qre_agent.llm import ChatReply
from qre_agent.spend import Guard, read_log
from qre_agent.tools import SandboxError, Toolbox

# 4-qubit QFT at p = 1e-3, from results/comparison
SURFACE = {"physical_qubits": 140015, "runtime_ns": 1934400, "physical_qubit_seconds": 270.845016,
           "logical_qubits": 15, "t_count": 9, "rotation_count": 9, "error": 4.64e-4,
           "assumptions": {"physical_error_rate": 0.001}}  # fmt: skip
BICYCLE = {"physical_qubits": 1226, "runtime_ns": 75295028.7, "physical_qubit_seconds":
           92.3117051862, "logical_qubits": 12, "t_count": 9, "rotation_count": 9}  # fmt: skip
ANSWER = """{"summary": "Surface needs 140,015 physical qubits, two-gross 1,226, assuming p = 1e-3.",
 "estimates": [
  {"result_id": "r1", "architecture": "surface", "physical_qubits": 140015,
   "runtime_ns": 1.93e6, "physical_qubit_seconds": 271},
  {"result_id": "r2", "architecture": "bicycle", "physical_qubits": 1226,
   "runtime_ns": 7.53e7, "physical_qubit_seconds": 92.3}]}"""  # written precision matters


class StubToolbox(Toolbox):
    """Canned estimates, so the loop is tested without the estimators."""

    def build_circuit(self, code):
        if code == "bad":
            raise SandboxError("NameError: name 'QFT' is not defined")
        self.circuits["c1"] = QuantumCircuit(4)
        return {"circuit_id": "c1", "num_qubits": 4}

    def estimate_surface(self, circuit_id, **_):
        return self._record("surface", circuit_id, SURFACE)

    def estimate_bicycle(self, circuit_id, **_):
        return self._record("bicycle", circuit_id, BICYCLE)


def call(name, arguments, i=0):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": args}}  # fmt: skip


def reply(text="", *calls):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = list(calls)
    return ChatReply(text, 1000, 100, 400, reported_cost_usd=None, message=message)


class FakeClient:
    """Replays `replies` in order (the last one forever) and keeps what it was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def complete(self, model, messages, max_tokens):
        self.sent.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


ESTIMATE = (
    reply("", call("build_circuit", {"code": "qft"})),
    reply("", call("estimate_surface", {"circuit_id": "c1"}),
          call("estimate_bicycle", {"circuit_id": "c1"})),
)  # fmt: skip


@pytest.fixture
def guard(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"dev": 1}, "prices": prices}))
    return Guard("dev", budgets, tmp_path / "spend.jsonl")


def go(client, guard, tmp_path, **kw):
    return run("Compare a 4-qubit QFT", client, guard, "fake", "t1", StubToolbox(),
               runs_dir=tmp_path, **kw)  # fmt: skip


def trace(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_answer_is_verified_and_the_run_is_traced(guard, tmp_path):
    result = go(FakeClient(*ESTIMATE, reply(ANSWER)), guard, tmp_path)
    assert (result.stop_reason, result.steps) == ("answered", 3)
    assert result.verification == {"passed": True, "failures": []}
    assert result.answer["estimates"][1]["physical_qubits"] == 1226
    assert result.totals["input_tokens"] == 3000 and result.totals["cached_tokens"] == 1200
    # 600 fresh + 400 cached at 0.1 + 100 out at 2, per 1M, three calls
    assert result.totals["cost_usd"] == pytest.approx(3 * 840 / 1e6)
    assert len(read_log(guard.log_path)) == 3

    records = trace(result.trace_path)
    kinds = [r["type"] for r in records]
    assert kinds == ["meta", "message", "message", "llm_call", "tool_call", "llm_call",
                     "tool_call", "tool_call", "llm_call", "auto_verify", "final"]  # fmt: skip
    meta, final = records[0], records[-1]
    assert meta["model"] == "fake" and meta["max_steps"] == 12 and "qiskit" in meta["versions"]
    assert records[1]["message"]["content"].startswith("You estimate")
    assert records[4]["result"]["circuit_id"] == "c1"
    assert all("latency_s" in r for r in records if r["type"] in ("llm_call", "tool_call"))
    assert final["verification"]["passed"] and final["totals"] == result.totals


def test_step_limit_stops_the_run(guard, tmp_path):
    client = FakeClient(reply("", call("build_circuit", {"code": "qft"})))
    result = go(client, guard, tmp_path, max_steps=3)
    assert (result.stop_reason, result.steps) == ("step_limit", 3)
    assert len(client.sent) == 3
    assert result.answer is None and result.verification is None
    assert trace(result.trace_path)[-1]["stop_reason"] == "step_limit"


def test_malformed_final_answer_is_sent_back_then_a_fixed_one_is_verified(guard, tmp_path):
    fenced = f"```json\n{ANSWER}\n```"
    client = FakeClient(*ESTIMATE, reply("Surface needs 140,015 qubits."), reply(fenced))
    result = go(client, guard, tmp_path)
    assert (result.stop_reason, result.steps) == ("answered", 4)
    feedback = client.sent[3][-1]
    assert feedback["role"] == "user" and "could not be checked" in feedback["content"]
    assert "not JSON" in feedback["content"]
    assert result.verification["passed"]


def test_a_final_answer_that_never_parses_ends_at_the_step_limit(guard, tmp_path):
    result = go(FakeClient(reply("no idea")), guard, tmp_path, max_steps=2)
    assert (result.stop_reason, result.answer) == ("step_limit", None)


@pytest.mark.parametrize(
    "estimates, reason",
    [
        ([{"result_id": "r1", "architecture": "surface"}], "one surface and one bicycle"),
        ([{"result_id": "r9", "architecture": "surface"}], "unknown result 'r9'"),
    ],
)
def test_answers_without_both_known_results_are_malformed(estimates, reason):
    toolbox = StubToolbox()
    toolbox.build_circuit("qft"), toolbox.estimate_surface("c1"), toolbox.estimate_bicycle("c1")
    answer = json.dumps({"summary": "", "estimates": estimates})
    assert reason in final_answer(answer, toolbox)[3]


def test_failed_verification_is_attached(guard, tmp_path):
    wrong = ANSWER.replace("140015", "14015")
    result = go(FakeClient(*ESTIMATE, reply(wrong)), guard, tmp_path)
    assert result.stop_reason == "answered" and not result.verification["passed"]
    assert {f["check"] for f in result.verification["failures"]} == {"fields_match"}


def test_tool_errors_go_back_to_the_agent(guard, tmp_path):
    client = FakeClient(
        reply("", call("build_circuit", {"code": "bad"})),
        reply("", call("build_circuit", "{not json", 1), call("no_such_tool", {}, 2)),
        *ESTIMATE,
        reply(ANSWER),
    )
    result = go(client, guard, tmp_path)
    assert result.verification["passed"]
    errors = [json.loads(m["content"])["error"] for m in client.sent[2] if m["role"] == "tool"]
    assert errors == [
        "SandboxError: NameError: name 'QFT' is not defined",
        errors[1],
        "ValueError: unknown tool 'no_such_tool'",
    ]
    assert errors[1].startswith("arguments are not valid JSON")
    assert client.sent[2][-1]["tool_call_id"] == "call_no_such_tool_2"


def test_a_refused_call_still_closes_the_trace(guard, tmp_path):
    with pytest.raises(Exception, match="exceeds the remaining"):
        go(FakeClient(reply(ANSWER)), guard, tmp_path, max_tokens=10**9)
    final = trace(tmp_path / "t1.jsonl")[-1]
    assert final["type"] == "final" and final["stop_reason"].startswith("error: BudgetError")


def test_numbers_from_the_task_are_known_to_the_automatic_verify(guard, tmp_path):
    text = ANSWER.replace("Surface needs", "For the 8-bit case, surface needs")
    client = FakeClient(*ESTIMATE, reply(text))
    result = run("Compare an 8-bit adder", client, guard, "fake", "t1", StubToolbox(),
                 runs_dir=tmp_path)  # fmt: skip
    assert result.verification == {"passed": True, "failures": []}


WRONG = ANSWER.replace("140015", "14015")  # fields_match fails


def auto_verifies(result):
    return [r for r in trace(result.trace_path) if r["type"] == "auto_verify"]


def test_a_verify_pass_on_the_first_try_is_recorded_and_not_retried(guard, tmp_path):
    client = FakeClient(*ESTIMATE, reply(ANSWER))
    result = go(client, guard, tmp_path)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (True, False)
    assert [(r["attempt"], r["verification"]["passed"]) for r in auto_verifies(result)] == [
        (1, True)
    ]
    assert len(client.sent) == 3
    final = trace(result.trace_path)[-1]
    assert (final["verify_passed_first_try"], final["verify_passed_after_retry"]) == (True, False)


def test_a_failed_verify_is_sent_back_once_and_a_revision_that_passes_is_recorded(guard, tmp_path):
    client = FakeClient(*ESTIMATE, reply(WRONG), reply(ANSWER))
    result = go(client, guard, tmp_path)
    assert (result.stop_reason, result.steps) == ("answered", 4)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (False, True)
    assert result.verification == {"passed": True, "failures": []}
    feedback = client.sent[3][-1]
    assert feedback["role"] == "user" and "failed verification" in feedback["content"]
    assert "fields_match" in feedback["content"]
    assert [(r["attempt"], r["verification"]["passed"]) for r in auto_verifies(result)] == [
        (1, False),
        (2, True),
    ]
    final = trace(result.trace_path)[-1]
    assert (final["verify_passed_first_try"], final["verify_passed_after_retry"]) == (False, True)


def test_the_retry_happens_once(guard, tmp_path):
    client = FakeClient(*ESTIMATE, reply(WRONG))
    result = go(client, guard, tmp_path)
    assert (result.stop_reason, result.steps) == ("answered", 4)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (False, False)
    assert not result.verification["passed"]
    assert len(auto_verifies(result)) == 2 and len(client.sent) == 4


def test_no_retry_when_the_step_limit_leaves_no_step(guard, tmp_path):
    client = FakeClient(*ESTIMATE, reply(WRONG))
    result = go(client, guard, tmp_path, max_steps=3)
    assert (result.stop_reason, result.steps) == ("answered", 3)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (False, False)
    assert len(client.sent) == 3 and len(auto_verifies(result)) == 1


def test_a_malformed_reply_between_the_attempts_does_not_use_up_the_retry(guard, tmp_path):
    client = FakeClient(*ESTIMATE, reply(WRONG), reply("oops"), reply(ANSWER))
    result = go(client, guard, tmp_path)
    assert (result.stop_reason, result.steps) == ("answered", 5)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (False, True)


def test_without_an_answer_neither_flag_is_set(guard, tmp_path):
    result = go(FakeClient(reply("no idea")), guard, tmp_path, max_steps=2)
    assert (result.verify_passed_first_try, result.verify_passed_after_retry) == (False, False)
    assert auto_verifies(result) == []
