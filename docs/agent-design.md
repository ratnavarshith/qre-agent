# Agent design: tools and verifier

Phase 2. Step 1 built the tools and the verifier; step 2 added the agent loop and an OpenRouter client.

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

**Benchmark tasks** name one of the families in `circuits.py`: QFT, phase estimation, transverse-field Ising model, ripple-carry adder, Grover search. The system prompt tells the agent to use `build_benchmark` for them, so the circuit is ours and the estimates are deterministic. A run is correct when:
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
- 10 free-form: circuits that aren't in `circuits.py` (QFT without swaps, Grover with an oracle gate named `oracle`, ripple-carry adders on given layouts, a Draper adder, GHZ plus rotations, a Heisenberg Trotter chain, a controlled-Rz ladder). The agent's circuit is checked against a reference in `evals/references/<id>.py`, with whichever of these the task lists: `semantics.qft`/`adder`/`grover` with the task's arguments (`qft` can require no swaps), `same_state` (output state from |0...0> equals the reference's up to global phase) and `counts_close_to` (T and rotation counts within 10% of the reference's). The numbers aren't compared to a fixed row.
- 5 ambiguous: the prompt leaves out the error rate, the size or the bicycle code (`assume`). The summary must say it assumed something and state the value it used, and the numbers must equal the results.json row for that value. A valid choice other than the default is graded correct.

The error budget is never given and must stay at the 1e-3 default.

**Grading.** A run is correct when it has an answer, the task's checks pass and the final verify passed (on the first try or after the retry). Each failure gets a category. A run is counted under its first category in this order:

| category | when |
|---|---|
| api error | the provider failed (not the agent; left out of the rates) |
| budget/step limit | the guard refused a call, or no answer within the step limit while still calling tools |
| gave up | no answer, and the last reply was prose without tool calls |
| wrong circuit | wrong benchmark family or size, or the free-form circuit fails its checks |
| tool misuse | `build_circuit` for a named benchmark; verify's same_circuit or physical_bounds failed |
| wrong assumptions | p, code or budget differ from the task, surface and bicycle used different p, or an ambiguous task's assumption isn't stated |
| missed budget fail | a cited result is over the error budget and the summary doesn't say so |
| made-up numbers | verify's numbers_match or fields_match failed |
| unit/format | the answer never parsed, or a field is off by a power of 1000 (ms written as ns) |
| reference mismatch | right circuit and settings but numbers differ from results.json: the toolchain changed, not the agent |

The stated-assumption checks are regexes. An error rate counts as stated only right after "error rate" or "p" (or right before "error rate"), so "an error budget of 0.001" doesn't state p = 1e-3. "two-gross" doesn't state "gross".

**Grader self-test.** `run_eval.py --self-test` (and `tests/test_eval.py`, marked `compiler`) builds every task's reference answer with the real tools, as an agent would, and grades it, along with corrupted versions: a ratio in the prose, the runtime in ms, the wrong size, p or code, the corrupted free-form circuit (`<id>_wrong.py`), the budget failure left unsaid, and for ambiguous tasks the assumption left out or misstated, plus the other valid assumption. The reference and the alternative must pass, and each corruption must fail with its expected category. The table goes to `results/eval/grader-selftest.md`.

**Output.** `results/eval/<model>/<date>[-name]/`: `config.yaml`, `meta.json` (versions, hardware, task-file hash), `runs.jsonl` (one graded line per run: correct, verify first try and after retry, category and reasons, tool errors, steps, tokens, our cost and OpenRouter's, wall latency) and `summary.md`. The summary gives rates as mean ± sample standard deviation (min–max) across repeats, per type and difficulty, plus cost, tokens, latency, failure counts by category and per-task results. Repeat i uses seed `seed + i`, which OpenRouter passes to providers that support it. `--summarize DIR` rewrites the summary from `runs.jsonl`. `--estimate` prints the worst case (every run uses every step at the guard's worst case) and an expected cost (the mean cost of earlier runs of that model in `runs/`), without calling the API. Spend goes to phase `eval`.

**Limitations.**
- The grader checks numbers, not labels: "14 physical qubits" for the circuit's 14 logical qubits passes, because 14 appears in a tool output.
- Stated assumptions are found by regex. A phrasing outside the patterns counts as unstated (a false failure); it is never counted as stated by mistake, but the tests only cover the phrasings seen so far.
- Free-form circuits are checked at small n against a reference (semantics, output state, or T and rotation counts within 10%); a circuit that passes can still differ from the reference in ways those checks don't see.

## Still to build

- An Anthropic client (OpenRouter is done).
- A rule for what happens when `verify` fails: let the agent revise a limited number of times, then report the failure instead of answering.
- A final answer that reports more than one bicycle code (gross and two-gross) can't be verified: `verify` takes one result per architecture.
- A comparison tool that computes ratios and differences, so answers can state them and still pass check (c).
- A check that both sides used the same physical error rate and error budget. Today (a) checks the circuit only.
- Surfacing `passes: false` (error over budget) in the answer. The verifier doesn't require it yet.
- A container sandbox for untrusted input.
- Evals on more models, and the full 40-task run (see Evals).
