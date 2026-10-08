# Agent design: tools and verifier

Phase 2, step 1. The tools and the verifier exist and are tested. No LLM is wired in yet.

## Shape

The agent turns a plain-English problem into a circuit, estimates it on both architectures, and writes an answer. Before it answers, a deterministic verifier checks the result. Code: `src/qre_agent/tools.py` (tools, schemas, sandbox), `src/qre_agent/verify.py` (checks), `src/qre_agent/sandbox_runner.py` (the child process).

`Toolbox` holds one session. Circuits and results are stored under ids (`c1`, `r1`, ...), and the tools pass ids, not data. That way the LLM never re-types a circuit or a result, so it can't hand the verifier an edited result. `Toolbox.call(name, arguments)` runs a tool and always returns JSON. An exception comes back as `{"error": "..."}`, so the agent can fix its code and retry. Every output is kept in `Toolbox.outputs`, which the number check uses.

`SCHEMAS` holds one JSON Schema per tool (`name`, `description`, `parameters`). A test keeps them in line with the method signatures. Provider adapters (OpenRouter, Anthropic) still have to convert them to each API's format.

## Tools

| tool | input | output |
|---|---|---|
| `build_circuit` | `code`: Python that assigns a `QuantumCircuit` to `circuit` | `circuit_id`, qubits, clbits, depth, op counts; or the error |
| `estimate_surface` | `circuit_id`, optional `physical_error_rate`, `error_budget` | `estimate_surface`'s result without the frontier, plus `result_id`, `circuit` and `passes` (error ≤ budget) |
| `estimate_bicycle` | `circuit_id`, optional `code` (gross, two-gross), `physical_error_rate` (1e-3 or 1e-4), `error_budget` | `estimate_bicycle`'s result, plus `result_id` and `circuit` |
| `verify` | `surface_result`, `bicycle_result` (result ids), `final_answer` (JSON string, below), optional `sweep` of `{size, surface, bicycle}` | `{"passed": bool, "failures": [{"check", "reason"}]}` |

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
- Audit hooks only see what raises an event. `ctypes` can call C functions directly, and the hook doesn't block it.
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

The second layer, `numbers_match`, checks the prose. Every number written in `summary` must equal some numeric value in the tool outputs, at the precision it's written. "19.5" matches 19.509, "1347" matches 1346.72, and "1346" doesn't. The check reads commas (`140,015`), e-notation (`4.64e-04`), `× 10^k` and `10^k`. It doesn't read digits inside words like `r1` or `two-gross`. Sweep sizes count as known numbers too.
- Consequence: ratios ("3x fewer"), percentages and differences fail unless a tool produced them. So the agent has to quote tool numbers, or a tool has to compute the comparison.
- Weakness: a number written with low precision is a weak check. "1e-3" matches anything from 0.0005 to 0.0015.
- Unicode superscripts (10⁻³) aren't parsed, so they fail. That's a false alarm, which is the safe direction.

**d. `grows_with_size`.** For a sweep, physical-qubit-seconds strictly increases with size on each architecture, and sizes must be distinct. Raw qubit counts aren't checked, because they can dip when v3 picks a different factory. In `results/comparison`, surface adder and Grover at p = 1e-3 dip (adder: 82,110 qubits at n = 4, 76,209 at n = 8), while qubit-seconds grow for every family and architecture.

## Still to build

- An agent loop: system prompt, a provider adapter (OpenRouter and Anthropic) behind `spend.Guard`, and tool-call dispatch through `Toolbox.call`.
- A rule for what happens when `verify` fails: let the agent revise a limited number of times, then report the failure instead of answering.
- A comparison tool that computes ratios and differences, so answers can state them and still pass check (c).
- A check that both sides used the same physical error rate and error budget. Today (a) checks the circuit only.
- Surfacing `passes: false` (error over budget) in the answer. The verifier doesn't require it yet.
- A container sandbox for untrusted input.
- Evals: a problem set with reference answers, repeated runs, spread across seeds and models.
- Recording each session (tool calls, outputs, versions, spend) under `runs/`.
