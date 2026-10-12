# qre-agent

You describe a quantum algorithm in plain English. An LLM agent writes the circuit and gets fault tolerant resource estimates on two architectures: the surface code (Microsoft's QDK estimator) and IBM's bicycle codes (their bicycle compiler). A deterministic verifier checks that every number in the answer came from a tool.

**Live demo:** https://qre-demo-766408496665.us-central1.run.app
The first load can take a few seconds if nobody used it recently, and it runs on a small budget so it may stop for the day.

### Phase 1: surface vs two-gross

Physical qubit-seconds at p = 1e-3, surface ÷ two-gross (above 1 means bicycle wins). Only sizes where two-gross meets the error budget.

| circuits | kind | surface ÷ two-gross |
|---|---|---|
| QFT, QPE (n = 4 to 16) | rotation heavy | 2.9 to 7.4x |
| TFIM (n = 4 to 32) | rotation heavy | 2.9 to 4.7x |
| Grover (n = 4 to 64) | Toffoli only | 0.74 to 0.97x |
| adder (n = 4 to 64) | Toffoli only | 0.45 to 0.65x |

Bicycle uses far fewer qubits but runs much longer. At p = 1e-4 the surface code beats two-gross at every size, and the plain gross code only wins on QFT with n = 4 (the other sizes it fails the error budget or loses). That comparison is mostly about factory choice (see Limitations).

### Phase 2: which model runs the agent

40 tasks, 3 passes per model. Mean ± sd across passes.

| model | correct | cost per run |
|---|---|---|
| gemini-2.5-flash | 82% ± 6 | $0.0044 |
| deepseek-v3.2 | 84% ± 3 | $0.0080 |
| haiku-4.5 | 81% ± 1 | $0.0290 |
| sonnet-5 | 97% ± 1 | $0.0464 |

### Phase 3: making it hold up

- **Faults.** With 30% of LLM calls failing on purpose, Gemini got 20% of tasks right without retries and fallback, 75% with them (one pass each).
- **Routing.** Start on Gemini, send to Sonnet when Gemini writes its own circuit or verify fails. In simulation that is 94% correct at half of Sonnet's cost ($0.023 vs $0.046 per task). One live pass got 95%. The rule was designed on the same data, so the simulation flatters it.
- **Prompt injection.** Delimiters plus a "don't follow instructions in tool output" rule did nothing on Gemini (9/30 attacks followed, vs 8/30 with no defense). A regex filter got it to 1/30, but I wrote the regex and the attacks together, so that number is optimistic.
- **CI eval gate.** PRs that touch the agent run 10 tasks on Gemini in CI and fail if they break.

### Run it locally

Needs Docker and an OpenRouter key in `.env` (`OPENROUTER_API_KEY=...`). The image builds IBM's compiler inside.

```
docker build -t qre-demo .
docker run --rm -p 8080:8080 --tmpfs /sandbox-tmp:size=64m --env-file .env qre-demo
```

Then open http://localhost:8080. Phase 1 alone needs no key: set up Python and the compiler as in [Running it](#running-it), then `python scripts/compare.py results/comparison/config.yaml`.

### Docs

- [docs/agent-design.md](docs/agent-design.md): tools, verifier, evals, sandbox, CI gate
- [docs/bicycle-interface.md](docs/bicycle-interface.md), [docs/qdk-interface.md](docs/qdk-interface.md): how each estimator is driven
- [docs/demo-api.md](docs/demo-api.md), [docs/demo-image.md](docs/demo-image.md), [docs/deploy.md](docs/deploy.md): the demo

## What I found

**Bicycle wins on rotations, loses on Toffolis.** On QFT, QPE and TFIM the two-gross code needs 2.9 to 7.4x fewer qubit-seconds than the surface code at p = 1e-3. On Grover it is slightly worse (1.03 to 1.35x) and on adders 1.5 to 2.2x worse. It uses 23 to 800x fewer qubits but runs 50 to 140x longer, because it has one T factory. IBM's Qiskit-to-PBC parser had two bugs (the rotation angle has the wrong sign and scale, and rotations land on the wrong qubits), so I wrote my own converter ([details](docs/bicycle-interface.md#2-parser-bugs-and-how-we-correct-them)). These runtimes depend on a timestep length I had to assume (66.7 ns). The rotation heavy wins survive unless the real timestep is about 3x longer.

**Sonnet is clearly best, the cheap models are a tie.** Sonnet 97%, the other three 81 to 84%, and those three are within noise of each other. Everyone does fine on standard tasks (93 to 100%). The gap is in tasks where the agent writes its own circuit: Sonnet 87%, the others 43 to 60%.

**Verify passes more often than answers are correct.** This was the main surprise. Verify only checks that the answer's numbers match tool outputs. It can't tell that the agent built the wrong circuit or misread the error rate. 16 Gemini runs, 23 Haiku, 14 DeepSeek and 4 Sonnet passed verify and were still wrong (57 of 480). So verify alone is a bad signal for when to escalate. That's why the routing rule also escalates on "Gemini wrote its own circuit".

**Prompt level injection defenses didn't help on Gemini.** Wrapping tool output in delimiters and telling the model not to follow instructions in it: 9 of 30 attacks followed, against 8 of 30 with nothing. That's no change. The regex filter is what dropped it to 1 of 30. But the same session wrote both the attacks and the regex, so it was shaped to catch them. On the 2 attacks the regex missed, the full guardrail was followed 1 of 6 times, same as no defense. All of this is Gemini only. Sonnet, which the demo escalates to, wasn't tested.

**Ambiguous tasks pass about half the time on Gemini.** 8 of 15 in the eval, 4 of 14 in later runs. The other models do much better here (DeepSeek and Haiku 13/15, Sonnet 15/15). Gemini's two failure modes: it asks the user for n and loops until the step limit, or it uses the default code and doesn't say which. Only one ambiguous task is in the CI gate because of this.

**Bicycle tables differ per build.** IBM's compiler breaks cost ties in `HashMap` order when it generates its measurement tables. So every build makes different tables, and bicycle timesteps move by about 1%. Qubits, T counts and pass/fail don't change. I record each table's sha256 with every result and in the demo's `/health`. I sent a fix upstream ([PR #28](https://github.com/qiskit-community/bicycle-architecture-compiler/pull/28), switches to `BTreeMap`). It's still open.

**Reliability layer works, the fallback is untested on quality.** Without retries, Gemini's success falls from 75% at 0% faults to 20% at 30% faults. With retries and a Haiku fallback it stays at 75 to 78%. Only 5 runs ever needed the fallback, too few to say if switching models mid run hurts.

**The eval found real bugs.** Gemini rejects numeric enums in tool schemas. The sandbox crashed on a π in the agent's code (Windows code page). Rotations at exactly ±π/2 made the surface estimator raise. All fixed, and 27 runs were rerun.

**Limits of the evals.** One eval run per model, 3 passes. The spreads are noise over 3 samples, not confidence intervals. I revised the grader (v1 to v2) after reading the failures, so v2 is partly fitted to these runs. It moved Haiku from 72% to 81%; the old numbers are in [results/eval/summary.md](results/eval/summary.md). The routing policy was also designed on these runs.

Built with a lot of help from Claude Code. The design, review and decisions are mine.

## Pipeline

```mermaid
flowchart LR
    C[Qiskit circuit] --> P["prepare()<br/>fixed-basis transpile,<br/>angle rules"]
    P --> S["surface<br/>qdk.qre, T count pinned<br/>to gridsynth"]
    P --> B1["bicycle<br/>our PBC converter"]
    B1 --> B2[bicycle_compiler]
    B2 --> B3[bicycle_numerics]
    S --> X[comparison table and plots]
    B3 --> X
```

Both sides get the same prepared circuit, the same error budget (1e-3, of which a third goes to rotation synthesis) and the same synthesis precision. Details and every decision are in [docs/](docs/).

## Running it

Setup (Windows, Python 3.11):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e . --no-deps
set QDK_PYTHON_TELEMETRY=none
```

The last line turns off QDK's telemetry: by default the `qdk` package, imported by every run, sends usage data to Microsoft's Application Insights in release builds. CI and the demo image set it too (in PowerShell: `$env:QDK_PYTHON_TELEMETRY = "none"`; to keep it, put it in your user environment variables).

The bicycle compiler is IBM's, and I call it by path without editing it. I used commit `bed7e8a` of [qiskit-community/bicycle-architecture-compiler](https://github.com/qiskit-community/bicycle-architecture-compiler):

```
cargo build --release -F bicycle_compiler/rsgridsynth
```

Then either clone and build it in a `bicycle-compiler/` folder inside this repo (where `assumptions/default.yaml` looks, and it is gitignored) or set the `QRE_COMPILER_DIR` environment variable to its `target/release` folder. Tests that need the compiler are marked `compiler` and are skipped if it isn't found. Don't run the compiler's own `scripts/*.sh`; they rebuild without rsgridsynth. The compiler needs a Clifford lookup table per code, which takes about a minute to generate; it is made on first use and kept in `cache/` (gitignored).

Regenerate everything (table, JSON, both plots):

```
python scripts/compare.py results/comparison/config.yaml
```

That takes about a minute and gives byte-identical output each time. `pytest` runs the tests.

## Results

![qubit-seconds at p = 1e-3](results/comparison/qubit_seconds_p1e-03.png)

n = 16, p = 1e-3, timestep 66.7 ns (physical qubit-seconds; qubits and runtime in brackets):

| circuit | surface | two-gross | surface ÷ two-gross |
|---|---|---|---|
| QFT | 4.7e4 (1.59M qubits, 29.6 ms) | 6.4e3 (1,994 qubits, 3,210 ms) | 7.4 |
| QPE | 5.5e4 (1.51M, 36.5 ms) | 7.4e3 (1,994, 3,720 ms) | 7.4 |
| TFIM | 6.8e3 (810k, 8.4 ms) | 2.3e3 (1,994, 1,170 ms) | 2.9 |
| adder | 108 (80.5k, 1.34 ms) | 241 (3,530, 68.4 ms) | 0.45 |
| Grover | 549 (132k, 4.16 ms) | 566 (2,762, 205 ms) | 0.97 |

Bicycle uses 23 to 800x fewer qubits but runs 50 to 140x longer, because it has one T factory and injects T gates one at a time.

## Limitations

- **The timestep length is an assumption.** The paper counts durations in "timesteps" and never gives them in nanoseconds. I use 66.7 ns, picked so that the paper's 6 timesteps per surface-code cycle matches our 400 ns cycle. Runtime is linear in it, so I report where the bicycle advantage disappears (the crossover timestep): 194 to 495 ns for QFT, QPE and TFIM, so those wins hold unless the timestep is about 3x or more longer. For Toffoli-only circuits it is 30 to 44 ns (adder) and 49 to 65 ns (Grover), so bicycle only wins there if its timestep is faster than 66.7 ns. The table also shows 50 and 100 ns.
- **The p = 1e-4 comparison is confounded by factory choice.** The paper uses an 18,600-qubit distillation factory for two-gross at 1e-4, which it calls very conservative, because there are no cultivation estimates at 1e-4. That factory is 67 to 96% of two-gross's qubits there. The surface side uses v3's own factory search. So the 1e-4 rows compare factory choices as much as architectures.
- **A constant in the bicycle numerics disagrees with the paper.** For gross at p = 1e-4, `bicycle_numerics` has a T-injection error 10x higher and a shift error 10x lower than the paper's Table 2. The main results use the code as it is. The sensitivity rows in table.md re-score gross with the paper's values; for QFT8 that turns a budget failure into a pass.
- **QFT and QPE stop at n = 20.** Qiskit's Litinski pass silently treats rotations within 2.4e-6 of a multiple of π/2 as Clifford, so above n = 20 the two estimators would see different circuits. `prepare()` raises an error instead. Going further needs an explicit approximate-QFT rule.
- **Surface is a frontier, bicycle is one point.** For the surface code I take the point on v3's qubits-vs-runtime frontier with the smallest qubit-seconds. The bicycle architecture here has one layout and one factory, so there is nothing to optimise. This favours surface a bit.
- **Both sides use gridsynth T counts.** The surface estimate is pinned to the T count that the compiler's gridsynth needs at the same precision, so the architectures pay the same per-rotation cost. v3's own synthesis model uses far fewer T gates per rotation (about 4x at these precisions). `synthesis: native` switches to that for the surface side; I haven't put native-mode results in the table.
- **Their Qiskit parser has two bugs, so I don't use it.** `scripts/qiskit_parser.py` writes the wrong rotation angle (+t·c instead of −2·t·c; I reported this on upstream issue #26) and puts rotations on the wrong qubits. `src/qre_agent/pbc.py` is my replacement and has tests for both cases.
- **Bicycle runtime numbers vary by about ±1% with the measurement table the compiler generated.** Its table search breaks ties in `HashMap` order, so every generated table differs; qubits, T counts and pass/fail don't change. The sha256 of the tables is recorded with new results (the results above predate that).
- The error totals are sums of per-instruction errors (union bound), so they are conservative. Developed on Windows; CI runs the tests on Linux (Ubuntu 24.04).

## Phase 2: the agent

The agent takes a plain-English problem, builds a circuit, estimates it on both architectures and writes an answer, in at most 12 LLM calls. It has five tools: `build_circuit` (Python for a Qiskit circuit, run in a restricted sandbox), `build_benchmark` (qft, qpe, tfim, adder or grover of size n), `estimate_surface`, `estimate_bicycle`, and `verify`. Circuits and results are passed around by id, so the model never re-types them. `verify` is deterministic: it checks that every number in the answer appears in a tool output, for the right architecture. The tools and verifier are in [docs/agent-design.md](docs/agent-design.md).

I ran four models on 40 tasks, 3 passes each (25 standard, 10 free-form where the agent has to write the circuit, 5 ambiguous where it has to state an assumption). A run is correct when the task's checks and the final `verify` both pass. Rates are mean ± standard deviation across the 3 passes, with (min–max). The grader was revised after I read the failures (v2); v1 is shown next to it. Full tables, costs and failure counts are in [results/eval/summary.md](results/eval/summary.md).

| model | correct (v2) | correct (v1) | verify first try (v2) | cost per run | latency p50 | p95 |
|---|---|---|---|---|---|---|
| gemini-2.5-flash | 82% ± 6 (75–87) | 75% ± 4 (70–78) | 93% ± 5 (88–97) | $0.0044 | 9 s | 26 s |
| deepseek-v3.2 | 84% ± 3 (82–88) | 82% ± 4 (78–85) | 86% ± 3 (82–88) | $0.0080 | 68 s | 170 s |
| haiku-4.5 | 81% ± 1 (80–82) | 72% ± 5 (68–78) | 96% ± 1 (95–98) | $0.0290 | 13 s | 26 s |
| sonnet-5 | 97% ± 1 (95–98) | 96% ± 1 (95–98) | 100% ± 0 (100–100) | $0.0464 | 19 s | 66 s |

- **Sonnet is about 15 points ahead** of the others (97% against 81 to 84%), at 1.6 to 10x the cost per run. All models get the standard tasks right (93 to 100%); the gap is in free-form circuits (87% against 43 to 60%).
- **The three cheaper models are within noise of each other under v2** (81 to 84%, spreads of 1 to 6 points). Under v1, DeepSeek is 10 points above Haiku, so that ordering depends on the grader.
- **Verify passes more often than answers are correct.** 57 of 480 runs passed `verify` and were still wrong (Gemini 16, Haiku 23, DeepSeek 14, Sonnet 4), because it checks the answer against the tool outputs, not that the circuit or settings were the right ones. So passing `verify` alone is not enough to decide when to escalate to a stronger model.
- **The eval found three bugs, now fixed.** Gemini rejects numeric enums in tool schemas and called `estimate_bicycle` with no arguments, so the schemas use string enums. `build_circuit` sent code to the sandbox in the Windows code page, so a π in the code crashed it (27 runs rerun after the fix). Rotations at ±π/2 made `estimate_surface` raise on ordinary circuits (rxx, ryy, controlled phases at π, the 4-bit Draper adder); `prepare()` now turns them into Clifford gates.

Limitations:

- **The grader checks numbers, not labels.** "14 physical qubits" for a circuit's 14 logical qubits passes, because 14 is in a tool output.
- **Assumption checks are regexes.** An ambiguous task passes if the answer states the value or says it assumed or defaulted one; it can't judge whether the assumption was sensible.
- **One eval run per model, 3 passes.** The spreads are run-to-run noise over 3 samples, not confidence intervals, and the same seed doesn't reproduce a run.
- **The grader changed after I read the failures.** Its rules came from the same runs they are scored on, which is why v1 is shown. v2 moves some models by up to 9 points (Haiku 72% to 81%) and Sonnet by 1.

## Prior work

- **Microsoft's resource estimator** ([QDK](https://github.com/microsoft/qdk)). I use its new `qdk.qre` module for the surface side. The older `qdk.qiskit.estimate` is deprecated; I only keep it as a cross-check on logical counts. See `docs/qdk-interface.md` for what I found.
- **[DeDuckProject/quantum-resource-estimator-mcp](https://github.com/DeDuckProject/quantum-resource-estimator-mcp)**, an MCP server around Microsoft's estimator with tools like `estimate_resources`, `compare_configurations` and `generate_frontier`. As far as its README shows, it wraps that one estimator. What I add is a second architecture (bicycle) on the same circuits with matched assumptions, which is where most of the work went.
- **Krol et al.** ([arXiv:2408.02587](https://arxiv.org/abs/2408.02587), Appendix D, which is in the PDF but not in the arXiv HTML version). They run one day of an industrial shift-scheduling problem through three estimators at p = 1e-3 and a 1e-3 error budget and get different answers: Bench-Q gives 348,000 physical qubits and 6.2 s; Microsoft's estimator gives 529,000 qubits and 72 ms with Qiskit input but 460,000 qubits and 50 ms with Q# input; Qualtran gives 111,000 qubits and 1.3 s. They put the Qiskit/Q# gap down to how the input is decomposed into T gates. That is why `prepare()` fixes one transpile and gives the identical circuit to both architectures. I don't compare estimators on the same architecture, so I can't say how much of such gaps is the tool and how much is the input.
- **Tour de gross** ([arXiv:2506.03094](https://arxiv.org/abs/2506.03094)) and IBM's compiler. I use their compiler and numerics and their module and factory sizes. Their own surface-code comparison uses a simple formula (2d² qubits per patch); I use v3 for the surface side instead.
