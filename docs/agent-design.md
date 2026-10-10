# Agent design: tools and verifier

Phase 2. Step 1 built the tools and the verifier; step 2 added the agent loop and an OpenRouter client. Phase 3, step 1 added tracing, a reliability layer and fault injection (see Reliability).

## Shape

The agent turns a plain-English problem into a circuit, estimates it on both architectures, and writes an answer. Before it answers, a deterministic verifier checks the result. Code: `src/qre_agent/tools.py` (tools, schemas, sandbox), `src/qre_agent/verify.py` (checks), `src/qre_agent/sandbox_runner.py` (the child process).

`Toolbox` holds one session. Circuits and results are stored under ids (`c1`, `r1`, ...), and the tools pass ids, not data. That way the LLM never re-types a circuit or a result, so it can't hand the verifier an edited result. `Toolbox.call(name, arguments)` runs a tool and always returns JSON. An exception comes back as `{"error": "..."}`, so the agent can fix its code and retry. Every output is kept in `Toolbox.outputs`, which the number check uses.

`SCHEMAS` holds one JSON Schema per tool (`name`, `description`, `parameters`). A test keeps them in line with the method signatures. Provider adapters (OpenRouter, Anthropic) still have to convert them to each API's format.

## Tools

| tool | input | output |
|---|---|---|
| `build_circuit` | `code`: Python that assigns a `QuantumCircuit` to `circuit` | `circuit_id`, qubits, clbits, depth, op counts; or the error |
| `build_benchmark` | `family` (qft, qpe, tfim, adder, grover), `n` | the same, plus `family` and `n`; the circuit is `circuits.FAMILIES[family](n)` |
| `estimate_surface` | `circuit_id`, optional `physical_error_rate`, `error_budget` | `estimate_surface`'s result without the frontier, plus `result_id`, `circuit` and `passes` (error ≤ budget) |
| `estimate_bicycle` | `circuit_id`, optional `code` (gross, two-gross), `physical_error_rate` (`"1e-3"` or `"1e-4"`, a string), `error_budget` | `estimate_bicycle`'s result, plus `result_id` and `circuit` |
| `verify` | `surface_result`, `bicycle_result` (result ids), `final_answer` (JSON string, below), optional `sweep` of `{size, surface, bicycle}` | `{"passed": bool, "failures": [{"check", "reason"}]}` |

Every `enum` in the schemas is a string enum. Gemini's function-calling schema allows `enum` on STRING only. In the first trial, the numeric enum on bicycle's `physical_error_rate` went with Gemini calling `estimate_bicycle` with `{}` in all three tasks. So the tool takes `"1e-3"` or `"1e-4"` and converts it; a number is accepted too.

**Timeouts and repeats.** `estimate_surface` and `estimate_bicycle` run in a spawned child process that is killed after `Toolbox.timeout` (180 s by default, `tools.TOOL_TIMEOUT_S`); the call then returns `{"error": "ToolTimeout: ..."}` and stores nothing. Not a thread: a thread can't be killed, and QDK keeps one interpreter context per process that panics when used from a second thread (`GlobalCallable is unsendable, but is being dropped on another thread`, hit by a first version that timed tools out with threads). A child costs about 1.7 s to start (importing qiskit and QDK), on top of the 0.3 s median estimate. `build_circuit` already runs in a child with its own 60 s limit. `build_benchmark` and `verify` run in-process with no limit: they are pure Python on small inputs. `Toolbox(timeout=None)` runs the estimates in-process; the grader's self-test uses that. Tools are safe to repeat (the agent may call one again after a timeout): the same call returns the same output under a new id, earlier ids and results are left alone, and a failed `verify`'s output adds no number that a later `verify` would accept (`tests/test_tools.py`).

`build_circuit` rejects circuits that contain `reset` or `initialize` (which adds resets), searching inside composite instructions, because the bicycle compiler can't compile resets. Its description says so, and that qubits start in |0>.

Defaults come from `assumptions/default.yaml`. The wrappers add `circuit: {circuit_id, num_qubits}` to each result. Check (a) needs it, because the estimators' own `logical_qubits` include each layout's overhead and differ by design: for a 4-qubit QFT, surface says 15 and bicycle says 12.

The final answer is structured JSON:

```json
{"summary": "Surface needs 140,015 physical qubits, two-gross 1,226.",
 "estimates": [
   {"result_id": "r1", "architecture": "surface", "physical_qubits": 140015,
    "runtime_ns": 1.93e6, "physical_qubit_seconds": 271},
   {"result_id": "r2", "architecture": "bicycle", "physical_qubits": 1226,
    "runtime_ns": 7.53e7, "physical_qubit_seconds": 92.3}]}
```

Each estimate cites the result it reports and uses that result's field names and units. Numbers can be rounded. The verifier parses them as `Decimal`, so it knows the precision written: `271` is precise to 1, and `271.0` claims precision to 0.1.

## Sandbox (`build_circuit`)

The agent's code runs in a separate `python -I` process with these layers:

1. **Static check, before anything runs.** Imports must come from the allowlist: qiskit, numpy, math, cmath, fractions, itertools, functools. Relative imports are rejected. So are names and attributes that start with `__`, and `eval`, `exec`, `compile`, `open`, `input`, `breakpoint`, `globals`, `locals`, `vars`, `getattr`, `setattr` and `delattr`.
2. **No network.** Before the code runs, the runner replaces `socket.socket.__init__`, `socket.getaddrinfo` and `socket.create_connection` with functions that raise.
3. **Minimal environment.** Only `PATH`, `SYSTEMROOT`, `TEMP` and `TMP` are passed through, so the child never sees API keys. It runs in a fresh temporary directory.
4. **Timeout.** 60 s by default, and the process is killed when it runs out.
5. **Audit hook** (`sys.addaudithook`, installed just before the agent's code runs):
   - `open` is allowed anywhere inside the run's temporary directory.
   - Read-only `open` is allowed inside the Python install and site-packages (`sys.prefix`, `sys.base_prefix`).
   - Every other `open` is blocked, the repo's `.env` included. That covers both `open()` and `os.open`, because both raise the same event.
   - Process events are blocked: `subprocess.Popen`, `os.system`, `os.exec`, `os.spawn`, `os.posix_spawn`, `os.startfile`, `os.fork`.
   - Filesystem changes are blocked: `os.remove`, `os.rename`, `os.mkdir`, `os.chmod` and similar.
   - Bytecode writing is turned off so imports don't trip the hook.

The circuit comes back as QPY.

**This is still a guardrail against mistakes, not a security boundary.** Python can't be sandboxed in-process.
- Code can reach `os` through attributes of allowed modules: `qiskit.utils.parallel.os` passes the static check. The audit hook then stops it from opening files outside the run directory or starting processes, and tests cover exactly that escape.
- Audit hooks only see what raises an event. `ctypes` is reachable through allowed modules (`numpy.ctypeslib.ctypes`), so the hook blocks every `ctypes.*` event. This Python (3.11.9, Windows) raises `ctypes.dlopen`, `ctypes.dlsym`, `ctypes.string_at`, `ctypes.addressof`, `ctypes.create_string_buffer`, `ctypes.get_errno` and `ctypes.set_errno`; it doesn't raise `ctypes.call_function`, so the block works at the library load and symbol lookup, not at the call. What gets through:
  - Calling a C function pointer that something already holds, since no event fires at the call.
  - `ctypes.memmove`, `ctypes.cast` and `ctypes.c_int` also raise no event. `id(obj)` is the object's address in CPython, so an escape can read or write the interpreter's memory without any blocked event, including memory the hook uses.
- The socket patch only covers the `socket` module's Python layer.

That's acceptable for code our own LLM writes from a problem statement. It isn't acceptable for untrusted users' code. For that, use a container (e.g. `docker run --network none`, read-only filesystem, CPU and memory limits). That's on the to-build list.

## Verifier checks

All checks are deterministic. Each returns `None` or a reason. `verify` runs them all and reports every failure.

**a. `same_circuit`.** Both results have the same `circuit` (id and qubit count), `t_count` and `rotation_count`. Both sides already share `prepare()`, but surface counts from v3's trace and bicycle from the Litinski-transformed PBC ops. So agreement here is a real cross-check, not a tautology.

**b. `physical_bounds`.** This runs on each result:
- physical-qubit-seconds is finite and > 0
- it equals qubits × runtime (relative tolerance 1e-9)
- circuit qubits ≤ logical qubits < physical qubits
- at most 10⁹ physical qubits and at most ten years of runtime

The upper bounds are sanity limits, not physics. Raise them if a real problem needs more.

**c. `fields_match`, then `numbers_match`.** Two layers.

The first layer, `fields_match`, checks the answer field by field:
- Every estimate's `result_id` must be one of the results being verified.
- Its `architecture` must match that result.
- Its `physical_qubits`, `runtime_ns` and `physical_qubit_seconds` must equal that result's fields at the precision written.
- Both `surface_result` and `bicycle_result` must be reported.

This catches surface and bicycle numbers swapped between architectures, which the second layer would miss because both numbers exist somewhere in the tool outputs. An answer that isn't JSON in this shape fails as `answer_format`.

The second layer, `numbers_match`, checks the prose. Every number written in `summary` must equal some numeric value in the tool outputs, at the precision it's written. "19.5" matches 19.509, "1347" matches 1346.72, and "1346" doesn't. The check reads commas (`140,015`), e-notation (`4.64e-04`), `× 10^k` and `10^k`. It doesn't read digits inside words like `r1` or `two-gross`. Sweep sizes count as known numbers too, and so does every number written in the task prompt ("8-bit"), since the user gave it.
- Time units: a number with `ms`, `µs` (or `μs`, `us`) or `s` right after it (at most one space) also matches a tool value converted from ns to that unit, at the precision written: "1.9344 ms" matches 1,934,400 ns and "0.0019344 s" does too, but "1.9344 s", "1.9344, ms" and a bare "1.9344" don't. Only the prose is converted. The `runtime_ns` field in `estimates` stays in ns (`fields_match`). A number with a unit matches only the converted value, never the raw ns one, so "1,934,400 ms" fails.
- Consequence: ratios ("3x fewer"), percentages and differences fail unless a tool produced them. So the agent has to quote tool numbers, or a tool has to compute the comparison.
- Since grader v2: units can be written out (`seconds`, `milliseconds`, `microseconds`, `nanoseconds`, singular or plural; `ns` explicitly), and a number written with more digits than a double holds matches within a relative 1e-12 (`0.00064718583333333343` for `0.0006471858333333334`). In the four-model eval v1 failed four answers on these: three copied a tool value with float-noise digits, one wrote "3.721 seconds".
- Weakness: a number written with low precision is a weak check. "1e-3" matches anything from 0.0005 to 0.0015.
- Unicode superscripts (10⁻³) aren't parsed, so they fail. That's a false alarm, which is the safe direction.

**d. `grows_with_size`.** For a sweep, physical-qubit-seconds strictly increases with size on each architecture, and sizes must be distinct. Raw qubit counts aren't checked, because they can dip when v3 picks a different factory. In `results/comparison`, surface adder and Grover at p = 1e-3 dip (adder: 82,110 qubits at n = 4, 76,209 at n = 8), while qubit-seconds grow for every family and architecture.

## Agent loop

`src/qre_agent/agent.py` runs one task: a system prompt (tools, default assumptions, the rule to state any assumption the user didn't give, the final-answer format), then up to 12 LLM calls (`max_tokens` 4096 each) through `spend.Guard`. Tool calls go through `Toolbox.call`, so a tool error goes back to the agent as `{"error": ...}`; so do tool arguments that aren't JSON.

A reply without tool calls is the final answer. A ```` ```json ```` fence around it is dropped. If it doesn't parse, or doesn't cite exactly one surface and one bicycle result the session produced, the reason goes back to the agent and the loop continues (within the step limit). Otherwise `verify` runs on the cited results and its result is attached. If it fails, the failures go back to the agent once (`VERIFY_RETRIES`), which may call tools again and must reply with a corrected JSON object; the second result is final. There is no retry when the failing answer came on the last step. A malformed reply between the two attempts doesn't use up the retry.

The trace has an `auto_verify` record per attempt, and the `final` record and the `Run` carry two flags for scoring: `verify_passed_first_try`, and `verify_passed_after_retry` (the first verify failed and the revision passed). Both are false when there was no answer or both attempts failed. Strict scoring counts the first flag; lenient scoring counts either. The trial report has both per task.

Each run writes `runs/<run_id>.jsonl`: a `meta` record (task, model, limits, seed, assumptions, versions, hardware), every message, every LLM call (message, tokens input/cached/output/reasoning, our cost, OpenRouter's reported cost, latency), every tool call (arguments, result, latency) and a `final` record (stop reason, answer, verification, totals). The `final` record is written even when a call raises.

`src/qre_agent/llm.py` is the OpenRouter client. It sends the tool schemas with every call and asks for usage accounting. The guard logs OpenRouter's reported cost next to the `budgets.yaml` estimate in `runs/spend.jsonl` and warns when they differ by more than 20% of ours. The guard's worst case counts the tool schemas too.

## Task types and grading

The eval grader sorts tasks into two types.

**Benchmark tasks** name one of the families in `circuits.py`: QFT, phase estimation, transverse-field Ising model, ripple-carry adder, Grover search. The system prompt tells the agent to use `build_benchmark` for them, so the circuit is ours and the estimates are deterministic. Since the four-model eval, it says to use it only when the task's circuit is exactly the benchmark (same algorithm, variant and qubit layout) and to write anything else with `build_circuit`. Under the old rule ("whenever the task names one of these families"), 5 of the 12 runs of `ff-adder-interleaved-4`, an adder on another layout, used the benchmark adder (all of Sonnet's, two of Gemini's; the other runs wrote a wrong adder or none, 0/12 correct); that failure stands, because it was a real error under the old prompt. Evals run with the new prompt aren't directly comparable with that one on free-form tasks. A run is correct when:
- it called `build_benchmark` with the right family and size;
- the results the answer cites equal the reference row in `results/comparison` (same p, budget and bicycle code) exactly: physical qubits, runtime and qubit-seconds;
- `verify` passes;
- every assumption the user didn't give is stated (the trial script checks the physical error rate with a regex; a human reads the rest).

**Custom-circuit tasks** describe a circuit that isn't a benchmark, or ask the agent to write it. The agent's circuit can legitimately differ from any reference (the trial's QFT without swaps, a different adder layout), so its numbers aren't compared to a fixed row. A run is correct when `verify` passes and, for n ≤ 8, the circuit passes `semantics.check` (`src/qre_agent/semantics.py`):
- **qft**: its unitary equals the QFT, with or without the final swaps, up to global phase.
- **adder**: on six basis inputs (0+0, 1+1, max+1, max+max and two seeded random pairs), simulation gives a + b. The circuit is simulated classically when it reduces to X/CX/CCX/SWAP, else with a statevector (at most 20 qubits). The layout comes from registers named `a`, `b`, `cout` when present (qiskit's adders), else a = qubits 0..n-1, b = n..2n-1 and the sum in b then qubit 2n; a task can give the layout explicitly.
- **grover**: the instruction named `oracle` maps |x>|0> to ±|x>|0> with exactly one minus sign (on the marked state, if the task names one). A whole Grover circuit can't be split into oracle and diffusion automatically, so a custom Grover task must ask for the oracle as an instruction named `oracle`.
- **qpe, tfim**: no semantics; the T count and rotation count (as the surface estimate counts them) must each be within 10% of the benchmark circuit's.

These checks need the task's family and size, so they belong to the grader, not to `verify`. They don't scale past small n: the QFT check builds a 2^n × 2^n unitary.

## Evals

Phase 2, step 3. Tasks: `evals/tasks.yaml`. Grader: `src/qre_agent/eval.py`. Runner: `scripts/run_eval.py`, driven by a config (`evals/eval.yaml` for the full eval, `evals/pilot.yaml` for the pilot) that is copied next to the results.

**Tasks.** 40, each with an id, type, difficulty (easy, medium, hard, or ambiguous), prompt and the parameters the answer must use (`params`):
- 25 standard: a benchmark family and size, p = 1e-3 or 1e-4, gross or two-gross, in varied phrasing ("2^16 items", "0.01% physical error rate", "[[144,12,12]]"). The expected numbers aren't written in the task file. The grader looks up the `results/comparison/results.json` rows for (family, n, p) on surface and on the code, and compares physical qubits, runtime, qubit-seconds and T count at relative tolerance 1e-6. Six tasks have a bicycle estimate over the error budget, so the summary has to say it fails.
- 10 free-form: circuits that aren't in `circuits.py` (QFT without swaps, Grover with an oracle gate named `oracle`, ripple-carry adders on given layouts, a Draper adder, GHZ plus rotations, a Heisenberg Trotter chain, a controlled-Rz ladder). The agent's circuit is checked against a reference in `evals/references/<id>.py`, with whichever of these the task lists: `semantics.qft`/`adder`/`grover` with the task's arguments (`qft` can require no swaps, and since grader v2 takes either qubit order), `same_state` (output state from |0...0> equals the reference's up to global phase) and `counts_close_to` (T and rotation counts within 10% of the reference's). The numbers aren't compared to a fixed row.
- 5 ambiguous: the prompt leaves out the error rate, the size or the bicycle code (`assume`). The summary must state the value it used (grader v1 also required the word "assumed" or "default"; v2 doesn't), and the numbers must equal the results.json row for that value. A valid choice other than the default is graded correct.

The error budget is never given and must stay at the 1e-3 default.

**Grading.** A run is correct when it has an answer, the task's checks pass and the final verify passed (on the first try or after the retry). Each failure gets a category. A run is counted under its first category in this order:

| category | when |
|---|---|
| api error | the provider failed (not the agent; left out of the rates) |
| budget/step limit | the guard refused a call, or no answer within the step limit while still calling tools |
| gave up | no answer, and the last reply was prose without tool calls (asking the user for a value included) |
| wrong circuit | wrong benchmark family or size, or the free-form circuit fails its checks |
| tool misuse | `build_circuit` for a named benchmark; verify's same_circuit or physical_bounds failed |
| wrong assumptions | p, code or budget differ from the task, surface and bicycle used different p, or an ambiguous task's assumption isn't stated |
| missed budget fail | a cited result is over the error budget and the summary doesn't say so |
| made-up numbers | verify's numbers_match or fields_match failed, or (v2) the last reply was answer JSON but no estimate ever ran |
| unit/format | answer JSON (a quoted `"estimates"` key) never parsed, or a field is off by a power of 1000 (ms written as ns) |
| reference mismatch | right circuit and settings but numbers differ from results.json: the toolchain changed, not the agent |

The stated-assumption checks are regexes. An error rate counts as stated only right after "error rate" or "p" (or right before "error rate"), so "an error budget of 0.001" doesn't state p = 1e-3. "two-gross" doesn't state "gross".

**Grader versions.** Records carry `grader`. v1 graded the four-model eval as it ran (`runs.jsonl`). v2 (`GRADER_VERSION = 2`) comes from reading its failures: the QFT in either qubit order, time units written out and float-noise digits in the verifier, a stated value counts as the assumption, and the two no-answer labels above. `run_eval.py --regrade DIR` grades stored runs again from their traces with no LLM calls (`eval.replay`): circuits are rebuilt from the logged build calls, results come from the log, and each automatic verify is redone on that step's answer with the toolbox as it was then; if the new rules pass a first attempt the old ones failed, the run counts as stopping there. The result goes to `runs-v2.jsonl` and `summary-v2.md`.

**Grader self-test.** `run_eval.py --self-test` (and `tests/test_eval.py`, marked `compiler`) builds every task's reference answer with the real tools, as an agent would, and grades it, along with corrupted versions: a ratio in the prose, the runtime in ms, the wrong size, p or code, the corrupted free-form circuit (`<id>_wrong.py`), the budget failure left unsaid, and for ambiguous tasks the assumption left out or misstated, plus the other valid assumption. The reference and the alternative must pass, and each corruption must fail with its expected category. The table goes to `results/eval/grader-selftest.md`.

**Output.** `results/eval/<model>/<date>[-name]/`: `config.yaml`, `meta.json` (versions, hardware, task-file hash, git commit and a dirty flag for changed tracked files; untracked files don't count, since results are written to untracked directories; the config copy ends with the same two values as a comment), `runs.jsonl` (one graded line per run: correct, verify first try and after retry, category and reasons, tool errors, steps, tokens, our cost and OpenRouter's, wall latency) and `summary.md`. The summary gives rates as mean ± sample standard deviation (min–max) across repeats, per type and difficulty, plus cost, tokens, latency, failure counts by category and per-task results. Repeat i uses seed `seed + i`, which OpenRouter passes to providers that support it. `--summarize DIR` rewrites the summary from `runs.jsonl`. `--estimate` prints the worst case (every run uses every step at the guard's worst case) and an expected cost (the mean cost of earlier runs of that model in `runs/`), without calling the API. Spend goes to phase `eval`.

**Models and caching.** `evals/eval-<model>.yaml` runs the 40 tasks × 3 on one model; the four are run in the order gemini-2.5-flash, deepseek-v3.2, claude-haiku-4.5, claude-sonnet-5. With `cache: true`, `llm.OpenRouter` puts `cache_control` on the system prompt and the last tool definition of calls to `anthropic/` models (Anthropic's cache prefix is tools, then system, then messages, so this caches both). Gemini and DeepSeek cache implicitly. Measured on one prompt (tools + system prompt + a short task): Sonnet 5 wrote 2,780 tokens on the first call and read all 2,780 on the second; Haiku 4.5 cached nothing, because its minimum cacheable prompt is 4,096 tokens and ours is 2,361. Cache writes cost 1.25× the input price (OpenRouter billing), so `budgets.yaml` has a `cache_write` price, `cost()` bills written tokens at it, and the guard's worst case prices the whole prompt as a write.

**API errors.** The eval runs without the reliability layer (below), so a provider failure ends the run with no answer and is graded `api error`. `run_eval.py --rerun-errors DIR` reruns each of DIR's api-error runs once (same task, repeat and seed), replaces it in `runs.jsonl` (the record gets `rerun_of`, the first attempt's run id and stop reason), keeps the original file as `runs-first-attempt.jsonl`, and rewrites `summary.md`, which says how many runs were api errors on the first attempt and how many after the rerun. The first-attempt count is the no-retry baseline for Phase 3, which adds retries. `--rerun DIR --runs TASK:REPEAT ... --reason TEXT` reruns named runs the same way, for a harness bug fixed after the eval ran: the reason goes into `rerun_of` and into the list of rerun events in `meta.json`, and such reruns don't count as api errors.

**Rate limits.** OpenRouter limits new accounts to 20 requests a minute per Anthropic model; an unpaced Haiku eval got HTTP 429 on 4 of its first 10 runs and stopped (kept as `2026-10-09-aborted-429`). `max_rpm` in a config makes `llm.Pacer` keep calls at least 60 / max_rpm seconds apart, shared by all runs of the eval. It is not a retry: a failed call still fails. The wait is recorded per run as `paced_wait_s` and subtracted from `latency_s` and `llm_s`, so latencies are comparable with unpaced models (the per-call latencies in the traces still include it).

**Stop rules.** `stop` in the config: the run stops, writes what it has and exits with code 2 when the cost so far passes `cost_factor` (2) times the expected cost of the whole eval, or when more than `api_error_rate` (10%) of the runs are api errors once `min_runs` (10) are done. Expected cost is the mean token use of the 15 earlier Gemini runs (trial and pilot), with input tokens scaled by each model's prompt-token ratio (tokenizers differ: the same prompt is 1,377 tokens on Gemini and 2,868 on Sonnet 5) and, for Sonnet, the cached prefix read on every step. It assumes the other models take about as many steps and write about as many tokens as Gemini; reasoning models may not.

**Limitations.**
- The grader checks numbers, not labels: "14 physical qubits" for the circuit's 14 logical qubits passes, because 14 appears in a tool output.
- Stated assumptions are found by regex. A phrasing outside the patterns counts as unstated (a false failure); it is never counted as stated by mistake, but the tests only cover the phrasings seen so far.
- Free-form circuits are checked at small n against a reference (semantics, output state, or T and rotation counts within 10%); a circuit that passes can still differ from the reference in ways those checks don't see.

## Reliability

Phase 3, step 1. Code: `src/qre_agent/tracing.py`, `reliability.py`, `claude.py`, `faults.py`; experiment runner `scripts/run_faults.py`.

**Tracing.** Each `agent.run` also writes an OpenTelemetry trace to `runs/otel/<run_id>.jsonl`, one span per line in the SDK's JSON, from a local exporter (no collector, no hosted service). One trace per run: an `agent_run` root span (run id, model, phase; at the end stop reason, steps, whether verify passed, cost), an `llm_call` span per LLM call and a `tool_call` span per tool call.
- LLM spans: `gen_ai.request.model`, `gen_ai.response.model` (the fallback's, after a fallback), `gen_ai.usage.input_tokens` / `cached_tokens` / `output_tokens`, `cost_usd`, `latency_s`, `retries`, `fallback`, `faults`, and `error.type` when the call raised (a ProviderError's kind, else the exception's name). Each failed attempt is an `llm_retry` event, each injected fault a `fault_injected` event.
- Tool spans: `tool.name`, `tool.call_id`, `latency_s`, and `error.type` (the exception name in the tool's `{"error": ...}`, or `JSONDecodeError` for arguments that aren't JSON). Tools have no retries.

Each run has its own tracer provider, not the global one. `runs/<run_id>.jsonl` keeps its records; `llm_call` records gain `model`, `retries`, `fallback` and `faults`. `run(..., otel=False)` turns the OpenTelemetry trace off.

**Errors.** Both clients raise `llm.ProviderError` with a `kind`: `timeout`, `rate_limit` (429), `server` (5xx, Anthropic's 529 included) and `missing_usage` (a reply with no usage) and `malformed_json` (a reply with a tool call whose arguments aren't JSON) are retried; `client` (other 4xx) and `connection` (unreachable) are not. `retry_after` is the provider's Retry-After in seconds (a number or an HTTP date). An OpenRouter error body (HTTP 200 with `error`) takes its kind from its `code`, and is `server` without one. Every LLM call has a 180 s timeout: urllib's per-socket-operation timeout for OpenRouter, so a slowly trickling reply can take longer (one eval call took 301 s); the SDK's request timeout for Anthropic.

**Retries and fallback** (`reliability.Reliable`, switched by a config's `reliability: {enabled: true, ...}` through `reliability.build`). It has the guard's interface, so the agent loop is unchanged.
- A retryable failure is retried up to 3 times; the retry sends the same request again. Before retry k (0, 1, 2) it sleeps the longer of a full-jitter backoff, uniform in [0, min(30 s, 1 s · 2^k)] from a seeded RNG, and the provider's Retry-After, which is always waited in full.
- When the retries are used up, the same messages go to the fallback with the same rule: Anthropic's Messages API (official SDK, its own retries off), `claude-haiku-4-5`, `ANTHROPIC_API_KEY`, spend phase `phase3`. The next call starts on the primary again. Other errors are raised at once, with no fallback.
- Every attempt goes through a `spend.Guard`, so each one is budget-checked and a failed one is logged at its worst case, as before. The run's cost is priced at the model that answered.
- The reply carries `model`, `retries` (attempts sent again to the same provider), `fallback` and `faults`; a final failure re-raises the last error with the same set on it.
- A retried LLM call never re-runs a tool: tools run only after a reply arrives.

`claude.Claude` takes the agent's OpenAI-style messages, so it can take over mid-run: system messages become `system`, tool calls `tool_use` blocks, tool results `tool_result` blocks, and consecutive user content one user turn. Tool-call ids with characters Anthropic rejects are rewritten, the same way in the matching result; arguments that aren't JSON become `{}` (their error is already the result). OpenRouter's `reasoning_details` are dropped. Input tokens count all prompt tokens, cache reads and writes included, as for OpenRouter. `budgets.yaml` prices it at Anthropic's list price ($1 / $5 per 1M, cache read $0.10, write $1.25). No prompt caching: Haiku 4.5's minimum cacheable prompt (4,096 tokens) is longer than ours.

**Malformed tool-call JSON is retried.** A reply whose tool call has arguments that aren't JSON was a successful, billed call, so each one is in the spend log; the layer treats it like a failure and resends the same request, with the same backoff, retry budget and fallback. If the last attempt on the last provider is still malformed, that reply is returned and the agent handles it as before: it sends `arguments are not valid JSON` back as the tool's result and goes on, at the cost of a step. Without the reliability layer that is the only path: every malformed reply loses a step (`tests/test_faults.py` checks both).

**Fault injection** (`faults.Injected`, also with the guard's interface, between `Reliable` and the guard). Before each call, with probability `rate`, it injects one fault drawn uniformly from `kinds`: a timeout, a 429 with a 1 s Retry-After, a 5xx (each raised as `InjectedFault`, a ProviderError), or a free reply whose `build_circuit` call has truncated JSON arguments (the reliability layer retries it like the others). An injected fault never reaches the guard or the provider: no tokens, no cost, no spend-log record. An injected timeout raises at once rather than waiting 180 s. Draws come from `random.Random(seed)`, so a run is reproducible. Without the reliability layer the first injected error ends the run (graded `api error`), and an injected malformed reply costs the run a step.

**Experiment** (`evals/phase3-faults.yaml`, not run yet). The 40 tasks on gemini-2.5-flash at injection rates 0, 10, 20 and 30% per LLM call, reliability off and on, one pass each: 320 runs, task by task over the eight cells, so a stop leaves the cells balanced.
- Faults are drawn per task and rate from seed `0-<task>-<rate>`, the same draws for off and on (until a retry draws again); the fallback is injected at the same rate from its own seed.
- `scripts/run_faults.py CONFIG` writes `results/phase3/faults/<date>/` (config, meta.json, runs.jsonl, summary.md). Per cell the summary has the success rate with a 95% Wilson interval (one pass, so no spread across seeds), api errors, total cost and cost per run, p50/p95 run latency (wall time, nearest rank), and retries, fallbacks and injected faults per run. Unlike the eval, runs ended by an API error count as failures.
- `--estimate` prints the expected and worst-case cost with no API calls: expected is the eval's per-run estimate, raised for the extra call each injected malformed reply causes, plus the calls that fall back ((rate × 3/4)^4 of calls, priced on Haiku); worst is every step at the guard's worst case on both providers. `--summarize DIR` rewrites the summary. The run stops above 2× the expected cost, and the guard refuses any call over the phase3 cap ($12).

## Still to build

- A rule for what happens when `verify` fails: let the agent revise a limited number of times, then report the failure instead of answering.
- A final answer that reports more than one bicycle code (gross and two-gross) can't be verified: `verify` takes one result per architecture.
- A comparison tool that computes ratios and differences, so answers can state them and still pass check (c).
- A check that both sides used the same physical error rate and error budget. Today (a) checks the circuit only.
- Surfacing `passes: false` (error over budget) in the answer. The verifier doesn't require it yet.
- A container sandbox for untrusted input.
- Evals on more models, and the full 40-task run (see Evals).
