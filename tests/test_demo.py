import threading
import time

import pytest
import yaml
from test_agent import ANSWER, ESTIMATE, FakeClient, StubToolbox, reply
from test_routing import BENCH, IN_FLIGHT, KEY_LIMIT, http_402

from qre_agent import demo as demo_module
from qre_agent.cache import EstimateCache
from qre_agent.demo import PRIMARY, STRONG, Cancelled, Demo, Hooks, Session, refusal_code
from qre_agent.llm import ProviderError

INFO = {"versions": {"qdk": "1.0"}, "measurement_tables": {"gross": "abc"}}


class DemoStub(Session, StubToolbox):
    """The demo's toolbox over canned estimates."""


@pytest.fixture
def budgets(tmp_path):
    path = tmp_path / "budgets.yaml"
    price = {"input": 1.0, "cache_read": 0.1, "output": 2.0}
    path.write_text(
        yaml.safe_dump({"caps": {"demo": 5}, "prices": {PRIMARY: price, STRONG: price}})
    )
    return path


class Quiet(Hooks):
    """Hooks that keep the events and don't wait out the retries' backoff."""

    def __init__(self, cancel=None):
        self.events = []
        super().__init__(self.events.append, cancel)

    def sleep(self, seconds):
        self.check()


def make(budgets, primary, strong=None, **kw):
    return Demo(primary, strong or FakeClient(reply(ANSWER)), INFO, toolbox_cls=DemoStub,
                budgets_path=budgets, **kw)  # fmt: skip


def go(d, hooks=None):
    hooks = hooks or Quiet()
    return d.run("Compare a 4-qubit QFT", "d-1", hooks), hooks


def test_a_benchmark_task_is_answered_by_the_primary_with_a_result(budgets):
    strong = FakeClient(reply(ANSWER))
    event, hooks = go(make(budgets, FakeClient(*BENCH, reply(ANSWER)), strong))
    assert event["event"] == "result" and event["run_id"] == "d-1"
    assert event["verification"] == {"passed": True, "failures": []}
    assert (
        event["verify_passed_first_try"]
        and event["answer"]["estimates"][1]["physical_qubits"] == 1226
    )
    assert [row["result_id"] for row in event["table"]] == ["r1", "r2"]
    assert [row["architecture"] for row in event["table"]] == ["surface", "bicycle"]
    assert event["routing"]["escalated"] is False and event["routing"]["answered_by"] == PRIMARY
    assert event["steps"] == 3 and event["cost_usd"] == pytest.approx(3 * 840 / 1e6)
    assert event["versions"] == INFO["versions"] and event["measurement_tables"] == {"gross": "abc"}
    assert event["assumptions"]["physical_error_rate"] == 1e-3
    assert strong.sent == []
    assert [e["tool"] for e in hooks.events if e["event"] == "step"] == [
        "build_benchmark", "estimate_surface", "estimate_bicycle"
    ]  # fmt: skip
    assert [e["n"] for e in hooks.events] == [1, 2, 3]


def test_a_custom_circuit_escalates_and_says_so(budgets):
    event, hooks = go(make(budgets, FakeClient(*ESTIMATE), FakeClient(*ESTIMATE, reply(ANSWER))))
    assert event["routing"]["escalated"] and event["routing"]["reason"] == "first_build"
    assert event["routing"]["answered_by"] == STRONG
    assert {"event": "escalated"} in hooks.events
    assert [e["n"] for e in hooks.events if e["event"] == "step"] == [
        1,
        2,
        3,
    ]  # the switched call never ran a tool


def test_a_failed_verify_is_reported_not_hidden(budgets):
    wrong = ANSWER.replace("1,226", "1,227")
    strong = FakeClient(*BENCH, reply(wrong))
    event, _ = go(make(budgets, FakeClient(*BENCH, reply(wrong)), strong))
    assert event["event"] == "result" and event["routing"]["escalated"]
    assert event["verification"]["passed"] is False and event["verification"]["failures"]
    assert event["verify_passed_first_try"] is False


def test_a_run_without_an_answer_still_returns_what_was_estimated(budgets):
    looping = FakeClient(BENCH[0])  # builds a benchmark forever
    event, _ = go(make(budgets, looping, FakeClient(*BENCH)))
    assert event["event"] == "result" and event["answer"] is None and event["verification"] is None
    assert [r["architecture"] for r in event["table"]] == ["surface", "bicycle"]  # the last of each
    assert event["stop_reason"] == "step_limit"


def test_the_claude_fallback_is_off_and_the_guardrail_on(budgets, monkeypatch):
    seen = {}
    real_reliable, real_routed = demo_module.Reliable, demo_module.run_routed

    def reliable(*args, **kw):
        seen["fallback"] = args[1]
        return real_reliable(*args, **kw)

    def routed(*args, **kw):
        seen["kw"], seen["runs_dir"] = kw, args[-1]
        return real_routed(*args, **kw)

    monkeypatch.setattr(demo_module, "Reliable", reliable)
    monkeypatch.setattr(demo_module, "run_routed", routed)
    go(make(budgets, FakeClient(*BENCH, reply(ANSWER))))
    assert seen["fallback"] is None
    assert seen["kw"] == {"guardrail": True, "otel": False}
    assert not seen["runs_dir"].parent.exists()  # traces and the spend log are removed


def test_the_estimate_cache_is_used_across_runs(budgets, tmp_path, monkeypatch):
    from test_agent import BICYCLE, SURFACE

    from qre_agent import bicycle, surface
    from qre_agent import cache as estimate_cache

    monkeypatch.setattr(surface, "estimate_surface", lambda c, a: SURFACE)
    monkeypatch.setattr(bicycle, "estimate_bicycle", lambda c, a: BICYCLE)
    monkeypatch.setattr(estimate_cache, "measurement_table", lambda a, code: code)
    monkeypatch.setattr(estimate_cache, "table_sha256", lambda path: "table")
    cache = EstimateCache(tmp_path / "estimates")

    class Real(demo_module.Session, demo_module.Toolbox):
        pass

    def run_once():
        d = Demo(FakeClient(*BENCH, reply(ANSWER)), FakeClient(), INFO, cache, Real,
                 budgets_path=budgets, estimate_s=None)  # fmt: skip
        return go(d)[0]

    assert run_once()["routing"]["cache_hits"] == 0
    assert run_once()["routing"]["cache_hits"] == 2


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        (KEY_LIMIT, "budget_exhausted"),
        (None, "budget_exhausted"),
        (IN_FLIGHT, "provider_busy"),
    ],
)
def test_a_402_ends_the_run_with_its_own_code_and_no_escalation(
    budgets, monkeypatch, metadata, code
):
    strong = FakeClient(*BENCH, reply(ANSWER))
    event, _ = go(make(budgets, http_402(monkeypatch, metadata), strong))
    assert event["event"] == "error" and event["code"] == code
    assert strong.sent == []


def test_the_run_cap_stops_the_run(budgets):
    event, _ = go(make(budgets, FakeClient(*BENCH, reply(ANSWER)), run_cap=1e-6))
    assert event["event"] == "error" and event["code"] == "run_cap"


def test_both_legs_failing_is_a_provider_error(budgets):
    class Down:
        def complete(self, model, messages, max_tokens):
            raise ProviderError("client", "bad request", 400)

    event, _ = go(make(budgets, Down(), Down()))
    assert event["event"] == "error" and event["code"] == "provider_error"


def test_refusal_codes():
    assert refusal_code("budget exhausted: OpenRouter HTTP 402") == "budget_exhausted"
    assert refusal_code("in-flight budget full: HTTP 402") == "provider_busy"
    assert refusal_code("phase demo: worst case $1 exceeds the remaining $0.2") == "run_cap"


def test_a_cancelled_run_makes_no_call(budgets):
    primary = FakeClient(*BENCH, reply(ANSWER))
    hooks = Quiet()
    hooks.cancel.set()
    with pytest.raises(Cancelled):
        make(budgets, primary).run("x", "d-1", hooks)
    assert primary.sent == []


def test_cancelling_during_the_primary_leg_does_not_escalate(budgets):
    hooks, strong = Quiet(), FakeClient(*BENCH, reply(ANSWER))

    class Leaves(FakeClient):
        def complete(self, model, messages, max_tokens):
            r = super().complete(model, messages, max_tokens)
            if len(self.sent) == 2:
                hooks.cancel.set()  # the client leaves after the second call
            return r

    with pytest.raises(Cancelled):
        make(budgets, Leaves(*BENCH, reply(ANSWER)), strong).run("x", "d-1", hooks)
    assert strong.sent == [] and {"event": "escalated"} not in hooks.events


def test_a_cancel_cuts_a_retry_wait_short():
    hooks = Hooks(lambda e: None)
    threading.Timer(0.05, hooks.cancel.set).start()
    start = time.monotonic()
    with pytest.raises(Cancelled):
        hooks.sleep(30)
    assert time.monotonic() - start < 5


def test_estimates_run_one_at_a_time_across_sessions():
    state = {"now": 0, "max": 0}

    class Slow(Session, StubToolbox):
        def estimate_surface(self, circuit_id, **_):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            state["now"] -= 1
            return super().estimate_surface(circuit_id)

    def session():
        t = Slow()
        t.hooks = Hooks(lambda e: None)
        t.call("build_circuit", {"code": "x"})
        t.call("estimate_surface", {"circuit_id": "c1"})

    threads = [threading.Thread(target=session) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 1


def test_a_session_waiting_for_the_estimate_slot_can_be_cancelled():
    t = DemoStub()
    t.hooks = Hooks(lambda e: None)
    t.call("build_circuit", {"code": "x"})
    demo_module.ESTIMATE_LOCK.acquire()
    try:
        threading.Timer(0.05, t.hooks.cancel.set).start()
        with pytest.raises(Cancelled):
            t.call("estimate_surface", {"circuit_id": "c1"})
    finally:
        demo_module.ESTIMATE_LOCK.release()
