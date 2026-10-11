# Demo API

Phase 4, PR 2. Code: `src/qre_agent/server.py` (HTTP), `demo.py` (one run), `limits.py` (limits and the key's budget). Start it with `python -m qre_agent.server` (port from `PORT`, default 8080); the extra `demo` in `pyproject.toml` has FastAPI and uvicorn. The plan and the reasons for the design are in the Phase 4 plan (not committed).

The demo has its own OpenRouter key with a hard $5 limit, in `OPENROUTER_API_KEY`. Startup fails with `cannot start the demo: OPENROUTER_API_KEY is missing or malformed ...` without it (the value is never printed). No Anthropic key is read: the Claude fallback of `reliability.Reliable` is never built, so a provider that keeps failing after its retries fails that leg (the primary's failure sends the task to the strong model, as policy f does for any primary error). Every run is policy f (gemini-2.5-flash, then claude-sonnet-5 on the strong model's prompt cache), the full injection guardrail, the estimate cache (`QRE_CACHE_DIR`, default `/tmp/qre/estimates`), 12 steps and 4096 tokens per call.

## `POST /estimate`

Body: `{"problem": "<1 to 1500 characters, trimmed>"}`, at most 8 KB, no other fields. The response is `application/x-ndjson`: one JSON event per line, kept open until the run ends.

| event | when |
|---|---|
| `{"event": "accepted", "run_id", "queue_position"}` | first; 0 starts at once, 1 is next in line |
| `{"event": "step", "n", "tool"}` | before each tool call (the name only, no arguments); `n` counts across both legs |
| `{"event": "escalated"}` | policy f restarted the task on the strong model |
| `{"event": "ping"}` | every 10 s of silence, so proxies keep the connection |
| `{"event": "result", ...}` | last, on success |
| `{"event": "error", "code", "message"}` | last, on a failure after the stream began |

`result` has: `run_id`; `answer` (the agent's final JSON, or null); `table` (the cited estimate results as the tools returned them, surface then bicycle; when there is no answer, the last result of each architecture); `verification` (`{"passed", "failures"}`, null without an answer; a failed verify is returned, not hidden); `verify_passed_first_try`; `stop_reason`; `routing` (`policy`, `escalated`, `reason`, `answered_by`, both legs' models and costs, `cache_hits`, `cache_misses`); `assumptions`; `steps`; `cost_usd` (ours, both legs); `latency_s`; `versions` and `measurement_tables` (the sha256 of each bicycle table).

### Errors

Before the stream starts: an HTTP status and `{"error": {"code", "message"}}`, with `Retry-After` where waiting helps.

| status | code | when |
|---|---|---|
| 413 | `body_too_large` | body over 8 KB |
| 422 | `invalid_request` | not JSON, `problem` missing, empty, over 1500 characters, or another field |
| 429 | `ip_busy` | this address already has a run in progress |
| 429 | `ip_hourly_limit` / `ip_daily_limit` | 4 runs in the last hour / 10 in the last 24 h |
| 429 | `busy` | 2 runs going and 6 waiting |
| 503 | `daily_cap` | the key's `usage_daily` reached $0.75; until 00:00 UTC |
| 503 | `budget_exhausted` | the key has under $0.40 left, or a call came back out of credit; until the process restarts |
| 503 | `budget_unknown` | the key's budget can't be read, or the key has no limit |

After it started (`{"event": "error", ...}`): `timeout` (240 s), `busy` (no place in 60 s), `budget_exhausted` (OpenRouter 402/403 out of credit: see `llm.status_kind`), `provider_busy` (in-flight budget still full after the retries), `run_cap` (the run would pass $0.35), `provider_error` (both legs failed after retries), `internal` (no detail is sent; the log has the exception type).

## Limits

| | |
|---|---|
| a run | 240 s from its start, both legs |
| at once / waiting | 2 / 6; a run waits at most 60 s for a place |
| estimates | one at a time across runs (each needs up to ~0.9 GB: the estimate child plus the compiler's 0.5 GB); 90 s each |
| per address | 1 at a time, 4 an hour, 10 in 24 h (rolling), counted when a run is accepted; in memory, so a restart resets them |
| per run | $0.35, as `Guard("demo", run_cap=0.35)` with a fresh spend log; the guard refuses a call whose worst case would pass it |
| per day | stop accepting at $0.75 `usage_daily` on the key |
| key | stop for good below $0.40 left |

The daily cap and the stop level come from `GET https://openrouter.ai/api/v1/key` (`limit_remaining`, `effective_budget.remaining_usd`, `usage_daily`), asked at most once every 30 s, so they survive a restart and count what other uses of the key spent. Runs already going can pass the daily cap; the per-run cap bounds that. `GET /limits` returns `accepting_runs`, the `reason` if not, and this address's `runs_left`. `GET /healthz` returns the versions and `accepting_runs` without any outside call. There is no docs page.

## A client that leaves

A disconnect cancels the run: it stops at its next LLM call, tool call or retry wait (a call already sent finishes, up to its 180 s timeout; an estimate child is killed by its own timeout). The run's place, the address's concurrency slot and the estimate lock are given back when its thread has really stopped, so the limits can't be exceeded by leaving and coming back. A timeout cancels the same way.

## The client address

Limits are per address. `X-Forwarded-For` is a list the client can start with any value; each proxy appends the address it saw. The server uses the entry `QRE_PROXY_HOPS` (default 1) from the right, never the leftmost, and falls back to the socket peer when the header is missing or too short. IPv6 is grouped by /64.

That is only safe if the proxy in front appends the connecting address. Behind Google's load balancers it does (the header ends `<client>,<load balancer>`, so there the value would be 2). Google's Cloud Run documentation does not say what Cloud Run's own front end does. **If it doesn't append the client's address, the rightmost entry is whatever the client sent, and the per-address limits can be spoofed**: one person could use a new fake address for every run and never hit the 1-at-a-time, 4-an-hour or 10-a-day limits. Cost is still bounded by what doesn't depend on the address: the 2 running and 6 waiting slots, the $0.35 per run, the $0.75 daily cap and the key's own $5 limit. The address limits only stop one person from using the daily cap up for everyone else. Check it after the first deploy (below).

## Post-deploy checks

1. **The client address.** The log holds only a salted hash of the address, so the check goes through the per-address counter. From one machine: send one real `POST /estimate` with `X-Forwarded-For: 1.2.3.4`. Then `GET /limits` three times: with no header, with `X-Forwarded-For: 1.2.3.4`, and with `X-Forwarded-For: 5.6.7.8`. If all three show `runs_left.hour` of 3, the address is the real one and the header is safe. If only the `1.2.3.4` one shows 3 and the others show 4, the rightmost entry is client-controlled: set `QRE_PROXY_HOPS` to the right count, or put the limits behind something that sets the address (and until then, treat the per-address limits as absent).
2. **The real `/key` reply.** `limits.KeyBudget` was written from OpenRouter's documentation and tested with made-up replies. With the demo key set, call `GET /limits` and check that `accepting_runs` is true and `reason` is null (a wrong field name or shape would show as `budget_unknown`), and that the first run's cost in the log is close to the drop in the key's `limit_remaining`.
3. **The out-of-credit path.** With a revoked or empty test key, a run should end in `budget_exhausted` and every request after it should get the 503.

## Logs

One line per run: run id, a salted hash of the address and of the prompt (the salt is per process), prompt length, status, cost, latency, whether it escalated and cache hits. No prompt, address, header or key. Traces and the run's spend log are written to a temporary directory and deleted when the run ends.

## What the tests cover

`tests/test_limits.py`, `test_demo.py`, `test_server.py`: no API calls. The LLM is scripted; one test (marked `compiler`) runs the real estimators through the whole server with a scripted LLM. Not covered: a real OpenRouter call, the real `/key` response shape (written from the docs), and Cloud Run's header.
