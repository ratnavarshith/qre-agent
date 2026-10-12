"""The server answers before the estimators have loaded (server.Startup): the page, /health and
/limits at once, a run once loading is done."""

import json
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient
from test_server import RESULT, FakeDemo, open_budget

from qre_agent import server
from qre_agent.server import Startup, create_app

VERSIONS = {"versions": {"qdk": "1.0"}, "measurement_tables": {"gross": "abc"}}


def blocked_load():
    """A load that finishes only when `release` is set; `started` is set when it begins."""
    started, release = threading.Event(), threading.Event()

    def load():
        started.set()
        release.wait(10)
        return FakeDemo(), VERSIONS

    return load, started, release


def test_importing_the_server_and_the_broker_does_not_import_qiskit_or_qdk():
    code = (
        "import sys, qre_agent.server, qre_agent.sandbox_broker\n"
        "print(sorted(m for m in ('qiskit', 'qdk', 'qre_agent.tools', 'qre_agent.demo') if m in sys.modules))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "[]"


def test_the_page_health_and_limits_answer_while_loading_and_a_run_waits_for_it():
    load, started, release = blocked_load()
    client = TestClient(create_app(None, open_budget(), load=load))
    assert started.wait(5)
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"ok": True, "ready": False, "accepting_runs": True}
    assert client.get("/limits").json()["accepting_runs"] is True

    got = {}
    run = threading.Thread(
        target=lambda: got.update(r=client.post("/estimate", json={"problem": "p"}))
    )
    run.start()
    time.sleep(0.3)
    assert run.is_alive()  # waiting for the load, not refused
    release.set()
    run.join(10)
    events = [json.loads(line) for line in got["r"].text.splitlines()]
    assert got["r"].status_code == 200 and events[-1] == RESULT
    assert (
        client.get("/health").json()
        == {"ok": True, "ready": True, "accepting_runs": True} | VERSIONS
    )


def events(response):
    return [json.loads(line) for line in response.text.splitlines()]


def test_a_run_waits_for_the_load_inside_its_own_time_limit_with_pings(monkeypatch):
    """No separate, shorter limit for the load: a run that arrives during it says it is loading,
    keeps the connection alive, and runs once the load is done."""
    monkeypatch.setattr(server, "PING_S", 0.05)
    load, started, release = blocked_load()
    client = TestClient(create_app(None, open_budget(), load=load))
    assert started.wait(5)
    threading.Timer(0.4, release.set).start()
    got = events(client.post("/estimate", json={"problem": "p"}))
    kinds = [e["event"] for e in got]
    assert kinds[:2] == ["accepted", "loading"] and "ping" in kinds and got[-1] == RESULT


def test_a_load_slower_than_the_run_limit_ends_in_starting_and_frees_the_places(monkeypatch):
    monkeypatch.setattr(server.limits, "RUN_S", 0.3)
    load, _, release = blocked_load()
    limiter, slots = server.IpLimiter(), server.Slots()
    client = TestClient(create_app(None, open_budget(), limiter=limiter, slots=slots, load=load))
    got = events(client.post("/estimate", json={"problem": "p"}))
    assert [e["event"] for e in got][:2] == ["accepted", "loading"]
    assert got[-1]["event"] == "error" and got[-1]["code"] == "starting"
    assert "didn't finish within" in got[-1]["message"]
    assert limiter.active["testclient"] == 0 and not slots.sem.locked()
    release.set()


def test_the_load_and_the_run_share_one_time_limit(monkeypatch):
    """Waiting for the load uses up the run's time: the whole request stays within the host's
    request timeout (queue wait + RUN_S < 360 s)."""
    monkeypatch.setattr(server.limits, "RUN_S", 1.0)
    load, _, release = blocked_load()
    threading.Timer(0.7, release.set).start()

    def slow(hooks):
        time.sleep(0.6)  # 0.7 s of loading + 0.6 s of run > the 1 s limit
        return RESULT

    startup = Startup(load=lambda: (load()[0] and FakeDemo(body=slow), VERSIONS))
    client = TestClient(create_app(None, open_budget(), load=startup))
    got = events(client.post("/estimate", json={"problem": "p"}))
    assert got[-1]["event"] == "error" and got[-1]["code"] == "timeout"


def test_a_load_that_fails_calls_on_error_and_runs_end_in_starting():
    failed = threading.Event()

    def load():
        raise FileNotFoundError("no measurement tables")

    startup = Startup(load=load, on_error=failed.set)
    assert failed.wait(5) and not startup.ready
    client = TestClient(create_app(None, open_budget(), load=startup))
    assert client.get("/health").json()["ready"] is False
    got = events(client.post("/estimate", json={"problem": "p"}))
    assert got[-1]["code"] == "starting" and "failed" in got[-1]["message"]


def test_given_a_demo_it_is_ready_at_once():
    startup = Startup(FakeDemo(), VERSIONS)
    assert startup.ready and startup.versions == VERSIONS
