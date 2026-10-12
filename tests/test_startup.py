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


def test_a_run_that_waits_too_long_for_the_load_gets_a_starting_error_and_no_place(monkeypatch):
    monkeypatch.setattr(server, "LOAD_WAIT_S", 0.1)
    load, _, release = blocked_load()
    limiter = server.IpLimiter()
    client = TestClient(create_app(None, open_budget(), limiter=limiter, load=load))
    r = client.post("/estimate", json={"problem": "p"})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "starting")
    assert r.headers["retry-after"] == "10"
    assert limiter.remaining("testclient") == {"hour": 4, "day": 10}  # it didn't count
    release.set()


def test_a_load_that_fails_calls_on_error_and_runs_are_refused():
    failed = threading.Event()

    def load():
        raise FileNotFoundError("no measurement tables")

    startup = Startup(load=load, on_error=failed.set)
    assert failed.wait(5) and not startup.ready
    client = TestClient(create_app(None, open_budget(), load=startup))
    assert client.get("/health").json()["ready"] is False
    r = client.post("/estimate", json={"problem": "p"})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "starting")


def test_given_a_demo_it_is_ready_at_once():
    startup = Startup(FakeDemo(), VERSIONS)
    assert startup.ready and startup.versions == VERSIONS
