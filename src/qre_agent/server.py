"""The demo's HTTP API: POST /estimate runs the agent and streams newline-delimited JSON events
(accepted, step, escalated, ping, then one result or error). Limits are in limits.py, a run in
demo.py. This module doesn't import qiskit, QDK or the estimators: they load in the background
after startup (Startup), so the page, /health and /limits answer while they do. See
docs/demo-api.md."""

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import limits, sandbox_broker
from .limits import IpLimiter, KeyBudget, Rejected, client_ip
from .run_control import Cancelled, Hooks, new_run_id
from .sandbox_common import SOCKET_ENV

log = logging.getLogger("qre_agent.demo")
MAX_BODY = 8192
MAX_PROBLEM = 1500
PING_S = 10
SALT = secrets.token_hex(8)  # per process: the logs can tell addresses apart, not name them
LOAD_WAIT_S = 60  # how long a run waits for the estimators to finish loading after a cold start


PAGE = Path(__file__).with_name("static") / "index.html"


def load_page(path=PAGE):
    """(html, headers) for GET /. The page has one inline script and one inline style, so the
    Content-Security-Policy allows exactly those two by hash, plus fetches to this origin: nothing
    else can load or run, which also holds if text from an answer ever got into the page."""
    html = path.read_text(encoding="utf-8")

    def source(tag):
        found = re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL)
        if len(found) != 1:
            raise RuntimeError(f"{path} must have exactly one inline <{tag}>, found {len(found)}")
        digest = hashlib.sha256(found[0].encode("utf-8")).digest()
        return "'sha256-" + base64.b64encode(digest).decode() + "'"

    policy = (
        f"default-src 'none'; script-src {source('script')}; style-src {source('style')}; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return html, {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache",
    }


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


def _exit_on_failed_load():
    """A server that can't load its estimators can't run anything: exit, as a failed startup did
    before loading moved to the background, so the host restarts the container."""
    logging.shutdown()
    os._exit(1)


class Startup:
    """The demo and its versions. Given (tests) they are ready at once. Given as `load`, a function
    returning (demo, versions), they are built in a background thread, so the server answers the
    page, /health and /limits while qiskit, QDK and the estimators import and the measurement
    tables are hashed. A run waits for them (`wait`). If `load` fails, `on_error` is called."""

    def __init__(self, demo=None, versions=None, load=None, on_error=_exit_on_failed_load):
        self.demo, self.versions, self.error, self.on_error = demo, versions or {}, None, on_error
        self.done = threading.Event()
        if load is None:
            self.done.set()
        else:
            threading.Thread(target=self._load, args=(load,), name="qre-load", daemon=True).start()

    def _load(self, load):
        start = time.monotonic()
        try:
            self.demo, self.versions = load()
        except Exception as e:  # noqa: BLE001 (logged, then the process exits)
            self.error = e
            log.error("loading the estimators failed: %s: %s", type(e).__name__, e)
            self.done.set()
            self.on_error()
            return
        log.info("estimators loaded in %.1f s", time.monotonic() - start)
        self.done.set()

    @property
    def ready(self):
        return self.done.is_set() and self.error is None

    async def wait(self, timeout=None):
        """The demo, once loaded; Rejected(503, "starting") if it isn't within `timeout` seconds."""
        if not self.done.is_set():
            await asyncio.to_thread(self.done.wait, LOAD_WAIT_S if timeout is None else timeout)
        if not self.ready:
            message = "The demo is still starting; try again in a few seconds."
            raise Rejected(503, "starting", message, 10)
        return self.demo


def create_app(demo, budget, limiter=None, slots=None, hops=1, versions=None, load=None):
    """`demo` runs a task (demo.Demo); `budget` is a limits.KeyBudget. Instead of `demo` and
    `versions`, `load` builds them in the background (Startup). `hops`: how many entries from the
    right of X-Forwarded-For are our own proxies' (see docs/demo-api.md)."""
    limiter, slots = limiter or IpLimiter(), slots or Slots()
    startup = load if isinstance(load, Startup) else Startup(demo, versions, load)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Rejected)
    async def rejected(request, e):
        headers = {"Retry-After": str(max(1, math.ceil(e.retry_after)))} if e.retry_after else {}
        body = {"error": {"code": e.code, "message": e.message}}
        return JSONResponse(body, status_code=e.status, headers=headers)

    page, page_headers = load_page()

    # HEAD too, for `curl -I` and uptime monitors: the server sends the headers, not the body.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index():  # no budget check, no limit: it is a static file
        return HTMLResponse(page, headers=page_headers)

    def address(request):
        peer = request.client.host if request.client else None
        return client_ip(request.headers.get("x-forwarded-for"), peer, hops)

    @app.post("/estimate")
    async def estimate(request: Request):
        ip = address(request)
        problem = await read_problem(request)
        await asyncio.to_thread(budget.check)
        demo = await startup.wait()  # only after a cold start, and before a place is taken
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

    # /health is the public path: Cloud Run reserves some paths ending in `z`, so /healthz never
    # reaches the container there (docs/deploy.md). /healthz stays for use inside the container.
    @app.api_route("/health", methods=["GET", "HEAD"])
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def health():  # no LLM call, no key check; `ready`: the estimators have loaded
        state = {"ok": True, "ready": startup.ready, "accepting_runs": not budget.exhausted}
        return state | startup.versions

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


def check_sandbox(environ=os.environ, wait_s=10.0):
    """Logs how build_circuit will run. RuntimeError if a broker is configured and doesn't answer,
    or if QRE_SANDBOX_REQUIRED=1 and there is none: the image sets it, so a missing broker is a
    startup failure, not a silent fallback to running the code as the server's own user."""
    mode, message = sandbox_broker.describe(environ)
    if environ.get("QRE_SANDBOX_REQUIRED") == "1" and mode != "broker":
        raise RuntimeError(f"{message} (QRE_SANDBOX_REQUIRED=1 needs the broker)")
    if mode == "broker":
        sandbox_broker.check(environ[SOCKET_ENV], wait_s)
    (log.info if mode == "broker" else log.warning)(message)


def load_demo():
    """(demo, versions): the part of startup that imports qiskit, QDK and the estimators, runs the
    compiler for its version and hashes the measurement tables. Run in the background (Startup)."""
    from .assumptions import load_assumptions
    from .cache import EstimateCache
    from .demo import Demo, versions_of
    from .llm import OpenRouter
    from .tools import SCHEMAS

    demo = Demo(
        OpenRouter(SCHEMAS),
        OpenRouter(SCHEMAS, cache=True),  # Anthropic prompt caching on the strong model
        versions_of(load_assumptions()),
        EstimateCache(Path(os.environ.get("QRE_CACHE_DIR", "/tmp/qre/estimates"))),
    )
    return demo, demo.info


def build():
    """(budget, proxy hops) from the environment, with the startup checks that stay before the
    server answers: RuntimeError, naming the variable, if the OpenRouter key is missing, and the
    sandbox check (check_sandbox). No Anthropic key is read: the Claude fallback is off."""
    from .spend import api_key

    key = api_key("OPENROUTER_API_KEY")
    check_sandbox()
    budget = KeyBudget(lambda: limits.fetch_key_info(key))
    return budget, int(os.environ.get("QRE_PROXY_HOPS", "1"))


def create_default_app():
    budget, hops = build()
    return create_app(None, budget, hops=hops, load=load_demo)


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
