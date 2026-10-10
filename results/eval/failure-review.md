## Failure review (read by hand)

I read at least one run from each of the 57 (model, category, task) groups of the 97 runs that failed under v1, and checked each pattern against all runs by script. Each finding below and what was done about it:

| # | finding | runs | done |
|---|---|---|---|
| 1 | The QFT-without-swaps check took one qubit order only; every failing circuit on those two tasks was the QFT written with qubit 0 as the most significant bit | 11 (Gemini 4, Haiku 4, DeepSeek 2, Sonnet 1) | grader v2; 10 regraded correct, 1 DeepSeek run hit the step limit and stays a failure |
| 2 | `build_circuit` crashed on non-ASCII code (π, ⟩), a harness bug | 27 (DeepSeek 18, Haiku 9) | fixed (8e394d1) and rerun; a second path (an agent's `print` of π) fixed in 0894b8d after it hit one rerun, which still ended correct |
| 3 | "made-up numbers" for real values: float-noise digits (3), "3.721 seconds" (1), the constant "2 iterations" from the tool description (1) | 5 (Gemini 4, DeepSeek 1) | grader v2 covers the first four; the "2 iterations" run still fails (no rule for numbers from tool descriptions) |
| 4 | "wrong assumptions" for stating the value without the word "assumed" | 9 (Haiku 6, DeepSeek 2, Gemini 1) | grader v2; all regraded correct |
| 5 | "unit/format" for two DeepSeek runs that never ran a tool and invented the numbers, and for a Gemini run that asked the user for the size | 3 | grader v2 relabels them made-up numbers and gave up |
| 6 | `ff-adder-interleaved-4`: 0/12; 5 runs (all of Sonnet's, two of Gemini's) used the benchmark adder, the old system prompt's rule; the rest wrote a wrong adder or none | 12 | stands; system prompt changed for future runs |

**Labels I checked and found right:**
- `wrong circuit` on the Grover tasks: the oracle marks the wrong state, or several.
- `wrong circuit` on the adders (wrong sums), the Draper adder (not a basis state), Heisenberg and the controlled-Rz ladder.
- `wrong assumptions` for Haiku running at 1e-4 while writing "0.1%" (all three runs of `std-qft8-1e3-gross`), for DeepSeek mixing error rates between the two architectures, and for naming "bicycle" without the code.
- `gave up` for Gemini asking the user for the missing size.
- `missed budget fail` for DeepSeek reporting an error of 0.0012987 without saying it is over budget. In its other such run the summary is cut off mid-sentence and continues in Chinese ("we found a code error, let me fix it").
- The remaining `unit/format` for prose before the answer JSON.

**Still open:**
- **Numbers taken from the tool descriptions** (the Grover iteration count) aren't known to the verifier.
- **The grader can't see labels:** "14 physical qubits" for the circuit's 14 logical qubits passes.
- **Reruns are a fresh sample, not a replay:** one Haiku run that was correct despite the bug came back wrong on the rerun (`ff-heisenberg-4` run 2), and the same seed doesn't reproduce outputs.
