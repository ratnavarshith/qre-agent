import asyncio
import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from qre_agent import limits, server
from qre_agent.demo import Cancelled
from qre_agent.limits import IpLimiter, KeyBudget, Rejected
from qre_agent.server import Lease, Slots, create_app, stream

RESULT = {"event": "result", "run_id": "d-1", "cost_usd": 0.004, "latency_s": 9.0,
          "routing": {"escalated": False, "cache_hits": 1}}  # fmt: skip
PROMPT = "Estimate a secret 16-qubit QFT circuit"


class FakeDemo:
    """Emits a step, then returns `event`; or runs `body(hooks)`."""

    def __init__(self, event=RESULT, body=None):
        self.event, self.body, self.calls = event, body, []
        self.info = {"versions": {"qdk": "1.0"}}

    def run(self, problem, run_id, hooks):
        self.calls.append(problem)
        if self.body:
            return self.body(hooks)
        hooks.emit({"event": "step", "n": 1, "tool": "build_benchmark"})
        if isinstance(self.event, Exception):
            raise self.event
        return self.event


def open_budget(**data):
    return KeyBudget(lambda: {"limit": 5, "limit_remaining": 4.0, "usage_daily": 0.1} | data)


def events(response):
    return [json.loads(x) for x in response.text.splitlines()]


def post(client, problem=PROMPT, ip="9.9.9.9", **kw):
    return client.post(
        "/estimate", json={"problem": problem}, headers={"x-forwarded-for": ip}, **kw
    )


@pytest.fixture
def make():
    def make(demo=None, budget=None, **kw):
        demo = demo or FakeDemo()
        app = create_app(demo, budget or open_budget(), **kw)
        return TestClient(app), demo

    return make


def test_a_run_streams_accepted_a_step_and_the_result(make):
    client, demo = make()
    r = post(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    got = events(r)
    assert [e["event"] for e in got] == ["accepted", "step", "result"]
    assert got[0]["queue_position"] == 0 and got[0]["run_id"].startswith("d-")
    assert got[-1] == RESULT and demo.calls == [PROMPT]


@pytest.mark.parametrize(
    "body",
    [
        {"problem": ""},
        {"problem": "   "},
        {"problem": "x" * 1501},
        {},
        {"problem": 3},
        {"problem": "ok", "model": "anthropic/claude-sonnet-5"},
    ],
)
def test_a_bad_body_is_a_422_with_the_error_envelope(make, body):
    client, demo = make()
    r = client.post("/estimate", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert demo.calls == []


def test_not_json_is_a_422_and_the_longest_problem_is_accepted(make):
    client, _ = make()
    assert client.post("/estimate", content=b"nope").status_code == 422
    assert post(client, "x" * 1500).status_code == 200


def test_an_oversized_body_is_a_413(make):
    client, demo = make()
    r = client.post("/estimate", content=b'{"problem": "' + b"x" * 9000 + b'"}')
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"
    assert demo.calls == []


def test_the_problem_is_trimmed(make):
    client, demo = make()
    post(client, "  hello  ")
    assert demo.calls == ["hello"]


def test_budget_errors_are_503s_with_their_codes_and_no_run(make):
    cases = [
        (open_budget(limit_remaining=0.3), "budget_exhausted", False),
        (open_budget(usage_daily=0.8), "daily_cap", True),
        (
            KeyBudget(lambda: (_ for _ in ()).throw(RuntimeError("HTTP 500"))),
            "budget_unknown",
            True,
        ),
    ]
    for budget, code, has_retry in cases:
        client, demo = make(budget=budget)
        r = post(client)
        assert (r.status_code, r.json()["error"]["code"]) == (503, code)
        assert ("retry-after" in r.headers) == has_retry
        assert demo.calls == []


def test_a_run_that_comes_back_out_of_credit_stops_the_demo(make):
    error = {"event": "error", "code": "budget_exhausted", "message": "budget exhausted: HTTP 402"}
    client, _ = make(FakeDemo(error))
    assert events(post(client))[-1] == error
    r = post(client, ip="8.8.8.8")
    assert (r.status_code, r.json()["error"]["code"]) == (503, "budget_exhausted")
    assert client.get("/healthz").json()["accepting_runs"] is False


def test_per_address_limits_use_the_rightmost_forwarded_entry(make):
    client, _ = make(limiter=IpLimiter(hour=2))
    # the client writes the first entry; each request below is the same address behind it
    for spoof in ("1.1.1.1", "2.2.2.2"):
        assert post(client, ip=f"{spoof}, 7.7.7.7").status_code == 200
    r = post(client, ip="3.3.3.3, 7.7.7.7")
    assert (r.status_code, r.json()["error"]["code"]) == (429, "ip_hourly_limit")
    assert int(r.headers["retry-after"]) > 0
    assert post(client, ip="3.3.3.3, 6.6.6.6").status_code == 200  # another address


def test_without_a_forwarded_header_the_peer_is_the_address(make):
    client, _ = make(limiter=IpLimiter(hour=1))
    assert client.post("/estimate", json={"problem": "a"}).status_code == 200
    assert client.post("/estimate", json={"problem": "a"}).status_code == 429


def test_a_full_queue_is_a_429_busy_and_does_not_count_against_the_address(make):
    limiter = IpLimiter()
    client, demo = make(limiter=limiter, slots=Slots(running=0, queued=0))
    r = post(client)
    assert (r.status_code, r.json()["error"]["code"]) == (429, "busy")
    assert r.headers["retry-after"] == "30" and demo.calls == []
    assert limiter.remaining("9.9.9.9") == {"hour": 4, "day": 10}


def test_an_unexpected_failure_gives_no_traceback_and_gives_back_the_slot(make, caplog):
    limiter = IpLimiter()
    client, _ = make(FakeDemo(RuntimeError("sk-or-v1-secret")), limiter=limiter)
    with caplog.at_level(logging.INFO):
        r = post(client)
    last = events(r)[-1]
    assert last["code"] == "internal" and "sk-or" not in r.text
    assert "sk-or" not in caplog.text and "RuntimeError" in caplog.text
    assert post(client).status_code == 200  # the address is free again


def test_logs_have_no_prompt_and_no_address(make, caplog):
    client, _ = make()
    with caplog.at_level(logging.INFO, logger="qre_agent.demo"):
        post(client, ip="9.9.9.9")
    text = caplog.text
    assert "secret" not in text and "9.9.9.9" not in text
    assert (
        f"prompt_len={len(PROMPT)}" in text and "status=result" in text and "cost_usd=0.004" in text
    )


def test_limits_and_healthz(make):
    client, _ = make(versions={"versions": {"qdk": "1.0"}})
    post(client)
    assert client.get("/limits", headers={"x-forwarded-for": "9.9.9.9"}).json() == {
        "accepting_runs": True, "reason": None, "runs_left": {"hour": 3, "day": 9},
    }  # fmt: skip
    assert client.get("/healthz").json() == {
        "ok": True,
        "accepting_runs": True,
        "versions": {"qdk": "1.0"},
    }
    client, _ = make(budget=open_budget(usage_daily=0.9))
    assert client.get("/limits").json()["reason"] == "daily_cap"


def test_there_is_no_docs_page(make):
    client, _ = make()
    assert [client.get(p).status_code for p in ("/docs", "/openapi.json", "/redoc")] == [404] * 3


def test_startup_fails_clearly_without_the_openrouter_key(monkeypatch, capsys):
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "0" * 40)
    with pytest.raises(SystemExit) as e:
        server.main()
    assert "cannot start the demo" in str(e.value) and "OPENROUTER_API_KEY" in str(e.value)
    assert "sk-ant" not in str(e.value)


def test_a_malformed_key_fails_without_printing_it(monkeypatch):
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-key-0123456789")
    with pytest.raises(SystemExit) as e:
        server.main()
    assert "OPENROUTER_API_KEY" in str(e.value) and "0123456789" not in str(e.value)


# The stream itself: queueing, pings, the time limit, and a client that leaves.


def drive(coro):
    return asyncio.run(coro)


async def collect(agen, n=None):
    out = []
    async for chunk in agen:
        out.append(json.loads(chunk))
        if n and len(out) == n:
            break
    return out


def parts(demo, limiter=None, slots=None, ip="1.1.1.1"):
    limiter, slots = limiter or IpLimiter(), slots or Slots()
    return demo, open_budget(), Lease(limiter, ip), slots, limiter


async def settle(predicate, seconds=5):
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("did not settle")


def test_a_client_that_leaves_cancels_the_run_and_frees_its_places_when_it_stops():
    stopped = threading.Event()
    seen = {}

    def body(hooks):
        seen["hooks"] = hooks
        hooks.emit({"event": "step", "n": 1, "tool": "build_benchmark"})
        hooks.cancel.wait(10)
        stopped.set()
        raise Cancelled

    async def main():
        demo, budget, lease, slots, limiter = parts(FakeDemo(body=body))
        agen = stream(demo, budget, lease, slots, "p", "d-1", "1.1.1.1")
        got = await collect(agen, n=2)  # accepted, then the first step
        assert [e["event"] for e in got] == ["accepted", "step"]
        await agen.aclose()  # the client went away
        assert seen["hooks"].cancel.is_set()
        await settle(stopped.is_set)
        await settle(lambda: not lease.held)
        assert limiter.active["1.1.1.1"] == 0
        await settle(lambda: not slots.sem.locked())

    drive(main())


def test_a_run_over_the_time_limit_gets_a_timeout_event_and_is_cancelled(monkeypatch):
    monkeypatch.setattr(limits, "RUN_S", 0.2)
    stopped = threading.Event()

    def body(hooks):
        hooks.cancel.wait(10)
        stopped.set()
        raise Cancelled

    async def main():
        demo, budget, lease, slots, _ = parts(FakeDemo(body=body))
        got = await collect(stream(demo, budget, lease, slots, "p", "d-1", "1.1.1.1"))
        assert [e["event"] for e in got] == ["accepted", "error"]
        assert got[-1]["code"] == "timeout"
        await settle(stopped.is_set)
        await settle(lambda: not lease.held)

    drive(main())


def test_a_quiet_run_is_kept_alive_with_pings(monkeypatch):
    monkeypatch.setattr(server, "PING_S", 0.05)
    release = threading.Event()

    def body(hooks):
        release.wait(5)
        return RESULT

    async def main():
        demo, budget, lease, slots, _ = parts(FakeDemo(body=body))
        agen = stream(demo, budget, lease, slots, "p", "d-1", "1.1.1.1")
        got = await collect(agen, n=3)
        assert [e["event"] for e in got] == ["accepted", "ping", "ping"]
        release.set()
        assert (await collect(agen))[-1] == RESULT

    drive(main())


def test_two_run_at_once_and_the_third_waits_in_line():
    gate = threading.Event()

    def body(hooks):
        gate.wait(5)
        return RESULT

    async def main():
        slots, limiter, demo = Slots(), IpLimiter(), FakeDemo(body=body)
        runs = []
        for i in range(3):
            lease = Lease(limiter, f"10.0.0.{i}")
            agen = stream(demo, open_budget(), lease, slots, "p", f"d-{i}", f"10.0.0.{i}")
            first = (await collect(agen, n=1))[0]
            runs.append((first, asyncio.ensure_future(collect(agen))))
            await asyncio.sleep(0.05)
        assert [first["queue_position"] for first, _ in runs] == [0, 0, 1]
        assert slots.waiting == 1 and len(demo.calls) == 2  # the third has not started
        gate.set()
        for _, task in runs:
            assert (await task)[-1] == RESULT
        assert len(demo.calls) == 3

    drive(main())


def test_the_queue_position_counts_the_runs_waiting_ahead():
    async def main():
        slots = Slots(running=1, queued=6)
        assert slots.position() == 0
        await slots.acquire(1)
        assert slots.position() == 1 and not slots.full()
        waiting = [asyncio.ensure_future(slots.acquire(5)) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert slots.position() == 4
        for _ in range(3):
            slots.release()
            await asyncio.sleep(0.01)
        assert [await w for w in waiting] == [True] * 3

    drive(main())


def test_the_queue_is_full_at_six_waiting():
    async def main():
        slots = Slots(running=2, queued=6)
        await slots.acquire(1)
        await slots.acquire(1)
        waiting = [asyncio.ensure_future(slots.acquire(5)) for _ in range(6)]
        await asyncio.sleep(0.05)
        assert slots.full()
        for _ in range(8):
            slots.release()
        await asyncio.gather(*waiting)
        assert not slots.full()

    drive(main())


def test_a_run_that_waits_too_long_for_a_place_gets_a_busy_error(monkeypatch):
    monkeypatch.setattr(limits, "QUEUE_WAIT_S", 0.05)

    async def main():
        slots = Slots(running=1, queued=6)
        await slots.acquire(1)  # taken, never released
        demo, budget, lease, _, _ = parts(FakeDemo(), slots=slots)
        got = await collect(stream(demo, budget, lease, slots, "p", "d-1", "1.1.1.1"))
        assert [e["event"] for e in got] == ["accepted", "error"] and got[-1]["code"] == "busy"
        assert not lease.held and demo.calls == []

    drive(main())


def test_a_lease_counts_once_and_releases_once():
    ips = IpLimiter()
    lease = Lease(ips, "1.1.1.1")
    with pytest.raises(Rejected):
        Lease(ips, "1.1.1.1")
    lease.release()
    lease.release()
    assert ips.active["1.1.1.1"] == 0
    Lease(ips, "1.1.1.1")


@pytest.mark.compiler
def test_a_real_run_end_to_end_with_a_scripted_llm_and_the_real_estimators(tmp_path):
    """The server, the demo, the real toolbox and both real estimators; only the LLM is scripted.
    The answer is written from the tool results, as the agent would."""
    import yaml
    from test_agent import call, reply

    from qre_agent.assumptions import load_assumptions
    from qre_agent.cache import EstimateCache
    from qre_agent.demo import PRIMARY, STRONG, Demo, versions_of

    class Scripted:
        def __init__(self):
            self.n = 0

        def complete(self, model, messages, max_tokens):
            self.n += 1
            if self.n == 1:
                return reply("", call("build_benchmark", {"family": "qft", "n": 4}))
            if self.n == 2:
                return reply(
                    "",
                    call("estimate_surface", {"circuit_id": "c1"}, 0),
                    call("estimate_bicycle", {"circuit_id": "c1"}, 1),
                )
            # the guardrail wraps each tool output in data delimiters
            wrapped = [m["content"] for m in messages if m["role"] == "tool"]
            tools = [json.loads(c[c.index("{") : c.rindex("}") + 1]) for c in wrapped]
            r = {t["architecture"]: t for t in tools if "architecture" in t}
            est = [
                {
                    "result_id": r[a]["result_id"],
                    "architecture": a,
                    "physical_qubits": r[a]["physical_qubits"],
                    "runtime_ns": r[a]["runtime_ns"],
                    "physical_qubit_seconds": r[a]["physical_qubit_seconds"],
                }
                for a in ("surface", "bicycle")
            ]
            summary = f"Surface needs {r['surface']['physical_qubits']} physical qubits."
            return reply(json.dumps({"summary": summary, "estimates": est}))

    budgets = tmp_path / "budgets.yaml"
    price = {"input": 1.0, "output": 2.0}
    budgets.write_text(
        yaml.safe_dump({"caps": {"demo": 5}, "prices": {PRIMARY: price, STRONG: price}})
    )
    demo = Demo(Scripted(), Scripted(), versions_of(load_assumptions()), EstimateCache(tmp_path / "c"),
                budgets_path=budgets)  # fmt: skip
    client = TestClient(create_app(demo, open_budget()))
    first = events(post(client, "4-qubit QFT"))
    result = first[-1]
    assert result["event"] == "result" and result["verification"]["passed"]
    assert [row["architecture"] for row in result["table"]] == ["surface", "bicycle"]
    assert result["routing"]["escalated"] is False and result["versions"]["qiskit"]
    assert set(result["measurement_tables"]) >= {"gross", "two-gross"}
    # the same task again hits the estimate cache for both estimates
    demo.clients = (Scripted(), Scripted())
    assert events(post(client, "4-qubit QFT"))[-1]["routing"]["cache_hits"] == 2
