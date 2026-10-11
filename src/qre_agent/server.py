"""The demo's HTTP API: POST /estimate runs the agent and streams newline-delimited JSON events
(accepted, step, escalated, ping, then one result or error). Limits are in limits.py, a run in
demo.py. See docs/demo-api.md."""

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import limits, sandbox_broker
from .demo import Cancelled, Demo, Hooks, new_run_id, versions_of
from .limits import IpLimiter, KeyBudget, Rejected, client_ip
from .tools import SOCKET_ENV

log = logging.getLogger("qre_agent.demo")
MAX_BODY = 8192
MAX_PROBLEM = 1500
PING_S = 10
SALT = secrets.token_hex(8)  # per process: the logs can tell addresses apart, not name them


class Problem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problem: str = Field(max_length=MAX_PROBLEM)

    @field_validator("problem")
    @classmethod
    def not_empty(cls, v):
        if not v.strip():
            raise ValueError("must not be empty")
        return v.strip()


def line(event):
    return (json.dumps(event) + "\n").encode()


def error_event(code, message):
    return {"event": "error", "code": code, "message": message}


class Slots:
    """`running` runs at once and `queued` more waiting for a place."""

    def __init__(self, running=limits.MAX_RUNNING, queued=limits.MAX_QUEUED):
        self.sem, self.queued, self.waiting = asyncio.Semaphore(running), queued, 0

    def full(self):
        return self.sem.locked() and self.waiting >= self.queued

    def position(self):
        """A new run's place in line: 0 starts at once, 1 is next."""
        return self.waiting + 1 if self.sem.locked() else 0

    async def acquire(self, timeout):
        self.waiting += 1
        try:
            await asyncio.wait_for(self.sem.acquire(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self.waiting -= 1

    def release(self):
        self.sem.release()


class Lease:
    """One run counted against an address; releasing twice is harmless."""

    def __init__(self, limiter, ip):
        self.limiter, self.ip, self.held = limiter, ip, True
        limiter.acquire(ip)

    def release(self):
        if self.held:
            self.held = False
            self.limiter.release(self.ip)


async def read_problem(request):
    """The request's problem text; Rejected(413) or Rejected(422) otherwise."""
    too_big = Rejected(413, "body_too_large", f"The body is over {MAX_BODY} bytes.")
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise too_big
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise too_big
    try:
        return Problem.model_validate_json(body).problem
    except ValidationError as e:
        why = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors())
        message = f'Send {{"problem": text}}, 1 to {MAX_PROBLEM} characters ({why}).'
        raise Rejected(422, "invalid_request", message) from None


def work(demo, budget, problem, run_id, hooks, record):
    """Runs in the run's thread: ends by emitting one terminal event, unless cancelled."""
    try:
        event = demo.run(problem, run_id, hooks)
    except Cancelled:
        return
    except Exception as e:  # noqa: BLE001 (the client gets no traceback; the log gets the type)
        log.error("run %s failed: %s", run_id, type(e).__name__)
        event = error_event("internal", "The run failed unexpectedly.")
    if event.get("code") == "budget_exhausted":
        budget.mark_exhausted()
    record(event)
    hooks.emit(event)


def create_app(demo, budget, limiter=None, slots=None, hops=1, versions=None):
    """`demo` runs a task (demo.Demo); `budget` is a limits.KeyBudget. `hops`: how many entries
    from the right of X-Forwarded-For are our own proxies' (see docs/demo-api.md)."""
    limiter, slots = limiter or IpLimiter(), slots or Slots()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Rejected)
    async def rejected(request, e):
        headers = {"Retry-After": str(max(1, math.ceil(e.retry_after)))} if e.retry_after else {}
        body = {"error": {"code": e.code, "message": e.message}}
        return JSONResponse(body, status_code=e.status, headers=headers)

    def address(request):
        peer = request.client.host if request.client else None
        return client_ip(request.headers.get("x-forwarded-for"), peer, hops)

    @app.post("/estimate")
    async def estimate(request: Request):
        ip = address(request)
        problem = await read_problem(request)
        await asyncio.to_thread(budget.check)
        if slots.full():
            raise Rejected(429, "busy", "The demo is busy; try again in a minute.", 30)
        lease = Lease(limiter, ip)
        # a body that is never read (the client left first) never reaches stream()'s finally
        asyncio.get_running_loop().call_later(
            limits.RUN_S + limits.QUEUE_WAIT_S + 60, lease.release
        )
        run_id = new_run_id()
        return StreamingResponse(
            stream(demo, budget, lease, slots, problem, run_id, ip),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/limits")
    async def limits_(request: Request):
        try:
            await asyncio.to_thread(budget.check)
            reason = None
        except Rejected as e:
            reason = e.code
        left = limiter.remaining(address(request))
        return {"accepting_runs": reason is None, "reason": reason, "runs_left": left}

    @app.get("/healthz")
    async def healthz():  # no LLM call, no key check
        return {"ok": True, "accepting_runs": not budget.exhausted} | (versions or {})

    return app


async def stream(demo, budget, lease, slots, problem, run_id, ip):
    """The NDJSON body of one run. A client that leaves cancels the run; its place and its
    address's slot are given back once the run's thread has really stopped."""
    loop = asyncio.get_running_loop()
    queue, cancel = asyncio.Queue(), threading.Event()
    hooks = Hooks(lambda e: loop.call_soon_threadsafe(queue.put_nowait, e), cancel)
    acquired, task, outcome = False, None, {}
    try:
        yield line({"event": "accepted", "run_id": run_id, "queue_position": slots.position()})
        acquired = await slots.acquire(limits.QUEUE_WAIT_S)
        if not acquired:
            outcome = error_event("busy", "No place came free in time.")
            yield line(outcome)
            return
        task = asyncio.ensure_future(
            asyncio.to_thread(work, demo, budget, problem, run_id, hooks, outcome.update)
        )
        task.add_done_callback(lambda _: (slots.release(), lease.release()))
        deadline = loop.time() + limits.RUN_S
        while True:
            left = deadline - loop.time()
            if left <= 0:
                cancel.set()
                outcome = error_event("timeout", f"The run took over {limits.RUN_S} s.")
                yield line(outcome)
                return
            try:
                event = await asyncio.wait_for(queue.get(), min(PING_S, left))
            except TimeoutError:
                if loop.time() < deadline:
                    yield line({"event": "ping"})
                continue
            yield line(event)
            if event["event"] in ("result", "error"):
                return
    finally:
        cancel.set()  # a normal end, a timeout or a client that left
        if task is None:
            if acquired:
                slots.release()
            lease.release()
        log_run(ip, problem, run_id, outcome)


def log_run(ip, problem, run_id, outcome):
    """One line per run: no prompt, no address, no key."""

    def digest(s):
        return hashlib.sha256((SALT + s).encode()).hexdigest()[:10]

    routing = outcome.get("routing") or {}
    status = outcome.get("code") or outcome.get("event") or "cancelled"
    log.info(
        "run %s ip=%s prompt_len=%d prompt=%s status=%s cost_usd=%s latency_s=%s escalated=%s "
        "cache_hits=%s",
        run_id, digest(ip), len(problem), digest(problem), status, outcome.get("cost_usd"),
        outcome.get("latency_s"), routing.get("escalated"), routing.get("cache_hits"),
    )  # fmt: skip


def check_sandbox(environ=os.environ):
    """Logs how build_circuit will run. RuntimeError if a broker is configured and doesn't answer,
    or if QRE_SANDBOX_REQUIRED=1 and there is none: the image sets it, so a missing broker is a
    startup failure, not a silent fallback to running the code as the server's own user."""
    mode, message = sandbox_broker.describe(environ)
    if environ.get("QRE_SANDBOX_REQUIRED") == "1" and mode != "broker":
        raise RuntimeError(f"{message} (QRE_SANDBOX_REQUIRED=1 needs the broker)")
    if mode == "broker":
        sandbox_broker.check(environ[SOCKET_ENV])
    (log.info if mode == "broker" else log.warning)(message)


def build():
    """(demo, budget, proxy hops) from the environment; RuntimeError, naming the variable, if the
    OpenRouter key is missing. No Anthropic key is read: the Claude fallback is off."""
    from .assumptions import load_assumptions
    from .cache import EstimateCache
    from .llm import OpenRouter
    from .spend import api_key
    from .tools import SCHEMAS

    key = api_key("OPENROUTER_API_KEY")
    check_sandbox()
    demo = Demo(
        OpenRouter(SCHEMAS),
        OpenRouter(SCHEMAS, cache=True),  # Anthropic prompt caching on the strong model
        versions_of(load_assumptions()),
        EstimateCache(Path(os.environ.get("QRE_CACHE_DIR", "/tmp/qre/estimates"))),
    )
    budget = KeyBudget(lambda: limits.fetch_key_info(key))
    return demo, budget, int(os.environ.get("QRE_PROXY_HOPS", "1"))


def create_default_app():
    demo, budget, hops = build()
    return create_app(demo, budget, hops=hops, versions=demo.info)


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        app = create_default_app()
    except RuntimeError as e:
        raise SystemExit(f"cannot start the demo: {e}") from None
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
