# Eval: google/gemini-2.5-flash, 2026-10-10 13:31:36 -0500

40 tasks × 1 repeats = 40 runs; seeds [0] (repeat i uses seed 0 + i). Task file `evals/tasks.yaml` (sha256 230e2860eddc). Versions: qdk 1.32.3, qiskit 2.5.2, bicycle_compiler 0.2.1, bicycle_compiler_commit bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb, rsgridsynth 0.2.2, cargo_lock_sha256 605a42f1b3414fe57ef0b3620b8bef1553480759228a94bd69f7799bb53e0101. Git commit 7623f6a91a42b0863d459a62e1298facdf5e56ce, dirty: False. Hardware: Intel64 Family 6 Model 191 Stepping 2, GenuineIntel, Windows-10-10.0.26200-SP0, Python 3.11.9.

Rates are mean ± sample standard deviation across repeats (min–max), each repeat being one pass over the tasks. Correct = the task's checks and verify both pass. Verify (any) = passed on the first try or after the one retry. Runs that hit an API error are left out of the rates (0 here).

API errors (no retries): 0 of 40 runs on the first attempt, 0 after rerunning them once.

| group | tasks | correct | verify first try | verify (any) | steps | tool errors |
|---|---|---|---|---|---|---|
| all | 40 | 95% | 98% | 100% | 5.3 ± 2.4 | 0.0 ± 0.0 |
| standard | 25 | 100% | 96% | 100% | 4.6 ± 0.8 | 0.0 ± 0.0 |
| free-form | 10 | 100% | 100% | 100% | 5.6 ± 1.0 | 0.0 ± 0.0 |
| ambiguous | 5 | 60% | 100% | 100% | 8.2 ± 5.8 | 0.0 ± 0.0 |
| easy | 8 | 100% | 88% | 100% | 4.8 ± 1.0 | 0.0 ± 0.0 |
| medium | 15 | 100% | 100% | 100% | 5.1 ± 1.1 | 0.0 ± 0.0 |
| hard | 12 | 100% | 100% | 100% | 4.7 ± 0.8 | 0.0 ± 0.0 |

## Cost and time

Per run: mean ± sd over all runs. Per repeat: one pass over the tasks.

| | value |
|---|---|
| total cost (ours) | $1.0507 |
| total cost (OpenRouter) | $0.9679 |
| cost per repeat | $1.0507 |
| cost per run | $0.02627 ± 0.05502 |
| input tokens per run | 16004 ± 15805 |
| cached tokens per run | 4622 ± 6422 |
| cache-written tokens per run | 72 ± 455 |
| output tokens per run | 1642 ± 2609 |
| latency per run (s) | 22.1 ± 27.9 |
| of which LLM (s) | 17.3 ± 27.6 |
| of which tools (s) | 4.5 ± 0.4 |

## Failures

Each failed run counted once, under its first category in this order: api error, budget/step limit, gave up, wrong circuit, tool misuse, wrong assumptions, missed budget fail, made-up numbers, unit/format, reference mismatch.

| category | standard | free-form | ambiguous | total |
|---|---|---|---|---|
| wrong assumptions | 0 | 0 | 2 | 2 |

## Per task

| task | type | difficulty | correct | failures |
|---|---|---|---|---|
| std-qft4-1e3-twogross | standard | easy | 1/1 |  |
| std-qft4-1e4-gross | standard | medium | 1/1 |  |
| std-qft16-1e4-twogross | standard | medium | 1/1 |  |
| std-qft20-1e3-twogross | standard | hard | 1/1 |  |
| std-qft8-1e3-gross | standard | hard | 1/1 |  |
| std-qpe4-1e3-twogross | standard | easy | 1/1 |  |
| std-qpe8-1e4-twogross | standard | medium | 1/1 |  |
| std-qpe16-1e3-twogross | standard | medium | 1/1 |  |
| std-qpe4-1e4-gross | standard | hard | 1/1 |  |
| std-adder8-1e3-twogross | standard | easy | 1/1 |  |
| std-adder4-1e4-gross | standard | easy | 1/1 |  |
| std-adder16-1e4-gross | standard | medium | 1/1 |  |
| std-adder32-1e3-twogross | standard | medium | 1/1 |  |
| std-adder64-1e4-twogross | standard | hard | 1/1 |  |
| std-adder16-1e3-gross | standard | medium | 1/1 |  |
| std-tfim4-1e3-twogross | standard | easy | 1/1 |  |
| std-tfim8-1e4-twogross | standard | medium | 1/1 |  |
| std-tfim16-1e3-twogross | standard | medium | 1/1 |  |
| std-tfim32-1e3-twogross | standard | medium | 1/1 |  |
| std-tfim64-1e3-twogross | standard | hard | 1/1 |  |
| std-grover4-1e3-twogross | standard | easy | 1/1 |  |
| std-grover8-1e4-gross | standard | medium | 1/1 |  |
| std-grover16-1e3-twogross | standard | hard | 1/1 |  |
| std-grover32-1e4-gross | standard | hard | 1/1 |  |
| std-grover64-1e3-twogross | standard | hard | 1/1 |  |
| ff-qft-noswap-5 | free-form | easy | 1/1 |  |
| ff-qft-noswap-6 | free-form | medium | 1/1 |  |
| ff-grover-4 | free-form | medium | 1/1 |  |
| ff-grover-5 | free-form | hard | 1/1 |  |
| ff-adder-interleaved-4 | free-form | hard | 1/1 |  |
| ff-adder-layout-3 | free-form | medium | 1/1 |  |
| ff-ghz-rz-5 | free-form | easy | 1/1 |  |
| ff-draper-adder-4 | free-form | hard | 1/1 |  |
| ff-heisenberg-4 | free-form | hard | 1/1 |  |
| ff-crz-ladder-4 | free-form | medium | 1/1 |  |
| amb-grover8-p-code | ambiguous | ambiguous | 0/1 | r0 wrong assumptions: the summary doesn't state the code used (two-gross) |
| amb-qft-n | ambiguous | ambiguous | 1/1 |  |
| amb-adder4-code | ambiguous | ambiguous | 0/1 | r0 wrong assumptions: the summary doesn't state the code used (two-gross) |
| amb-tfim8-p | ambiguous | ambiguous | 1/1 |  |
| amb-qpe-n-p | ambiguous | ambiguous | 1/1 |  |

## Routing (policy f)

One pass of 40 tasks, so no spread across passes; api errors count as failures. Cost is ours (`budgets.yaml` prices) and covers both legs of an escalated task.

| | this run | simulation, policy f (mean of 3 passes, range) |
|---|---|---|
| correct | 95% | 94% (92%–98%) |
| cost per task | $0.0263 | $0.0232 ($0.0221–$0.0250) |
| escalated | 28% | 27% (25%–28%) |

Escalations by reason: first_build 9, no_answer 1, primary_error 1. Cost before the switch $0.1406, after $0.9101 (total over the pass).

| group | tasks | correct | cost per task | latency p50 | max |
|---|---|---|---|---|---|
| answered by the primary | 29 | 93% | $0.0040 | 10 s | 18 s |
| escalated | 11 | 100% | $0.0849 | 41 s | 150 s |
| escalated: first_build | 9 | 100% | $0.0887 | 27 s | 150 s |
| escalated: primary_error | 1 | 100% | $0.0795 | 59 s | 59 s |
| escalated: no_answer | 1 | 100% | $0.0563 | 41 s | 41 s |

Latency per task (both legs, pacer wait left out): p50 11 s, p95 64 s, max 150 s.

Estimate cache: 0 hits, 80 misses.
